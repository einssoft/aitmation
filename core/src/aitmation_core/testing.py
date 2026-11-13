"""The proving run: executes a solution's sidecar tests (ADR-0035 §6-§9).

One runner for the workshop, the shop's curation gate and Colabbot
layer 1. It uses the ordinary engine and runtime - there is no second
execution path - under a named configuration (``test`` by default):
recorded model answers, stubbed tools, pinned clock, logged mails.

What is compared is state, not conversation (τ-bench, ADR-0035 §6): the
case status, the outcomes the steps took, the path through the process,
the gates it waited at, the structured fields a step produced. A case
with ``repeat = k`` passes only when all k runs pass (pass^k).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import aimn
from .apps import App, AppError, load_app
from .config import Settings
from .definition import ProcessDefinition, StepKind
from .model import ModelClient, ModelError
from .recordings import RecordingClient
from .runtime import Runtime
from .solution import BUILTIN_CONFIGURATIONS, Configuration, SidecarCase, sample_inside
from .template import _substitute

MAX_TICKS = 60


@dataclass
class CaseResult:
    case_id: str
    subject_kind: str
    subject_id: str
    description: str
    passed: bool
    attempts: int
    failures: list[str] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)
    duration_ms: int = 0


@dataclass
class Coverage:
    steps_total: int
    steps_visited: list[str]
    outcomes_total: int
    outcomes_visited: list[str]

    @property
    def step_ratio(self) -> float:
        return len(self.steps_visited) / self.steps_total if self.steps_total else 1.0


@dataclass
class TestReport:
    solution_id: str
    solution_version: str
    configuration: str
    results: list[CaseResult] = field(default_factory=list)
    coverage: dict[str, Coverage] = field(default_factory=dict)
    missing_recordings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and all(r.passed for r in self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ok"] = self.ok
        data["coverage"] = {
            pid: {**asdict(cov), "step_ratio": round(cov.step_ratio, 3)}
            for pid, cov in self.coverage.items()
        }
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def run_solution_tests(
    path: str | Path,
    *,
    configuration: str = "test",
    record: bool = False,
    inner_client: ModelClient | None = None,
    only: str | None = None,
    repeat: int | None = None,
    core_definitions: dict[str, ProcessDefinition] | None = None,
    core_templates_dir: Path | None = None,
    dependencies: dict[str, Path] | None = None,
) -> TestReport:
    """Run every sidecar case of the solution at ``path``.

    ``dependencies`` maps solution ids to their directories, for a
    solution that declares ``[dependencies]``; by default its siblings
    (directories next to ``path`` carrying a ``solution.toml``) serve."""
    started = time.monotonic()
    path = Path(path)
    dependencies = dict(dependencies or _sibling_solutions(path))
    try:
        provided = _load_provided(path, dependencies, core_definitions)
        app = load_app(path, core_definitions=core_definitions, provided=provided)
    except AppError as exc:
        return TestReport(path.name, "?", configuration, errors=[str(exc)])
    report = TestReport(app.id, app.version, configuration)
    cases = [c for c in app.sidecars if only is None or c.id == only]
    if only is not None and not cases:
        report.errors.append(f"no case {only!r} in solution {app.id}")
    run_config = _configuration(app, configuration)
    recordings_dir = path / "tests" / "recordings"
    visited_by_process: dict[str, set[str]] = {}
    outcomes_by_process: dict[str, set[str]] = {}

    for case in cases:
        # A case may name its own configuration (ADR-0035 §6); the run's
        # configuration applies to cases that do not.
        config = _configuration(app, case.config) if case.config != "test" else run_config
        runs = max(1, repeat or case.repeat)
        attempt = 0
        failures: list[str] = []
        visited: list[str] = []
        outcomes: dict[str, str] = {}
        case_started = time.monotonic()
        for attempt in range(1, runs + 1):
            client = _client(config, recordings_dir, record, inner_client)
            try:
                if case.subject_kind == "process":
                    run_failures, visited, outcomes, chain_trace = _run_process_case(
                        app, path, case, config, client, core_templates_dir, dependencies
                    )
                    # Coverage counts every process the chain touched - a
                    # called or chained sub-process is proven by the same run.
                    for pid, (seen_steps, seen_outcomes) in chain_trace.items():
                        visited_by_process.setdefault(pid, set()).update(seen_steps)
                        outcomes_by_process.setdefault(pid, set()).update(
                            f"{sid}:{o}" for sid, o in seen_outcomes.items()
                        )
                elif case.subject_kind in ("ai", "skill"):
                    run_failures = _run_briefing_case(app, path, case, client)
                elif case.subject_kind == "decision":
                    run_failures = _run_decision_case(app, case)
                else:
                    run_failures = [f"unsupported subject kind {case.subject_kind!r}"]
            except Exception as exc:
                run_failures = [f"{type(exc).__name__}: {exc}"]
            if isinstance(client, RecordingClient) and client.missing:
                report.missing_recordings.extend(
                    key for key in client.missing if key not in report.missing_recordings
                )
            if run_failures:
                failures.extend(f"run {attempt}/{runs}: {f}" for f in run_failures)
                break
        report.results.append(
            CaseResult(
                case_id=case.id,
                subject_kind=case.subject_kind,
                subject_id=case.subject_id,
                description=case.description,
                passed=not failures,
                attempts=attempt,
                failures=failures,
                visited=visited,
                outcomes=outcomes,
                duration_ms=int((time.monotonic() - case_started) * 1000),
            )
        )

    for pid, definition in app.templates.items():
        total_outcomes = sum(len(step.next) for step in definition.steps.values())
        report.coverage[pid] = Coverage(
            steps_total=len(definition.steps),
            steps_visited=sorted(visited_by_process.get(pid, set())),
            outcomes_total=total_outcomes,
            outcomes_visited=sorted(outcomes_by_process.get(pid, set())),
        )
    report.duration_ms = int((time.monotonic() - started) * 1000)
    return report


def _configuration(app: App, name: str) -> Configuration:
    base = BUILTIN_CONFIGURATIONS.get(name) or Configuration(name=name)
    return app.configurations.get(name, base)


def _client(
    config: Configuration, recordings_dir: Path, record: bool, inner: ModelClient | None
) -> ModelClient | None:
    if config.replays_models:
        if record:
            if inner is None:
                raise ModelError("--record needs a configured model (registry + key)")
            return RecordingClient(recordings_dir, mode="record", inner=inner)
        return RecordingClient(recordings_dir, mode="replay")
    return inner


# -- process cases -------------------------------------------------------------


def _run_process_case(
    app: App,
    solution_path: Path,
    case: SidecarCase,
    config: Configuration,
    client: ModelClient | None,
    core_templates_dir: Path | None,
    dependencies: dict[str, Path] | None = None,
) -> tuple[list[str], list[str], dict[str, str], dict[str, tuple[set[str], dict[str, str]]]]:
    failures: list[str] = []
    chain_trace: dict[str, tuple[set[str], dict[str, str]]] = {}
    definition = app.templates[case.subject_id]
    workdir = Path(tempfile.mkdtemp(prefix="aitmation-test-"))
    try:
        apps_dir = workdir / "apps"
        apps_dir.mkdir()
        _link_solution(solution_path, apps_dir / app.id)
        _link_dependencies(app, apps_dir, dependencies or {})
        settings = Settings(
            data_dir=workdir / "data",
            templates_dir=core_templates_dir or _default_core_templates(),
            apps_dir=apps_dir,
            configuration=config.name,
        )
        clock = None
        if config.now:
            pinned = datetime.fromisoformat(config.now).astimezone(UTC)
            clock = lambda: pinned  # noqa: E731 - the pinned clock of this run only
        runtime = Runtime(settings, model_client=client, clock=clock)
        try:
            payload = _trigger_payload(case, solution_path, runtime)
            case_id = runtime.engine.start_case(definition.id, payload, actor_id="test-runner")
            _drive(runtime, case_id, case)
            visited, outcomes = _trace(runtime, case_id)
            chain_trace = _trace_chain(runtime, case_id)
            failures = _compare_process(runtime, case_id, case, visited, outcomes, definition)
        finally:
            runtime.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return failures, visited, outcomes, chain_trace


def _trace_chain(runtime: Runtime, case_id: str) -> dict[str, tuple[set[str], dict[str, str]]]:
    """Visited steps and outcomes of every case in the chain, per process."""
    out: dict[str, tuple[set[str], dict[str, str]]] = {}
    rows = runtime.conn.execute(
        "SELECT c.template_id AS template_id, s.step_id AS step_id, s.outcome AS outcome"
        " FROM steps s JOIN cases c ON c.id = s.case_id"
        " WHERE c.chain_id = ? OR c.id = ? ORDER BY s.started_at",
        (case_id, case_id),
    ).fetchall()
    for row in rows:
        seen, outcomes = out.setdefault(row["template_id"], (set(), {}))
        seen.add(row["step_id"])
        if row["outcome"]:
            outcomes[row["step_id"]] = row["outcome"]
    return out


def _default_core_templates() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "templates" / "document-intake.bpmn"
        if candidate.is_file():
            return candidate.parent
    return here.parent / "templates"


def _link_solution(source: Path, target: Path) -> None:
    try:
        target.symlink_to(source.resolve(), target_is_directory=True)
    except OSError:
        shutil.copytree(source, target)


def _sibling_solutions(path: Path) -> dict[str, Path]:
    """Solutions next to ``path`` by id, the default pool for dependencies."""
    out: dict[str, Path] = {}
    try:
        siblings = sorted(p for p in path.parent.iterdir() if p.is_dir() and p != path)
    except OSError:
        return out
    for sibling in siblings:
        manifest = sibling / "solution.toml"
        if manifest.is_file():
            try:
                data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                continue
            if isinstance(data.get("id"), str):
                out[data["id"]] = sibling
    return out


def _load_provided(
    path: Path, pool: dict[str, Path], core_definitions: dict[str, ProcessDefinition] | None
) -> dict[str, App]:
    """Load the declared dependencies of the solution at ``path`` (and
    theirs), dependencies first, so its references resolve as installed."""
    out: dict[str, App] = {}

    def deps_of(solution_path: Path) -> list[str]:
        try:
            data = tomllib.loads((solution_path / "solution.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return []
        raw = data.get("dependencies")
        return [str(k) for k in raw] if isinstance(raw, dict) else []

    def visit(dep: str, trail: tuple[str, ...]) -> None:
        if dep in out or dep in trail or dep not in pool:
            return
        for inner in deps_of(pool[dep]):
            visit(inner, (*trail, dep))
        out[dep] = load_app(pool[dep], core_definitions=core_definitions, provided=out)

    for dep in deps_of(path):
        visit(dep, ())
    return out


def _link_dependencies(app: App, apps_dir: Path, pool: dict[str, Path]) -> None:
    """Link the dependency closure of ``app`` into the temporary apps dir,
    so the throw-away runtime can load it (ADR-0035 §4)."""
    todo = list(app.manifest.dependencies)
    linked: set[str] = {app.id}
    while todo:
        dep = todo.pop()
        if dep in linked:
            continue
        linked.add(dep)
        source = pool.get(dep)
        if source is None:
            continue  # the runtime reports the missing dependency
        _link_solution(source, apps_dir / dep)
        manifest = source / "solution.toml"
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        deps = data.get("dependencies")
        if isinstance(deps, dict):
            todo.extend(str(k) for k in deps)


def _trigger_payload(case: SidecarCase, solution_path: Path, runtime: Runtime) -> dict:
    given = case.given
    if "trigger" in given:
        return dict(given["trigger"])
    if "file" in given:
        if not sample_inside(solution_path, str(given["file"])):
            raise ValueError(f"given.file {given['file']!r} is not a sample of the solution")
        sample = solution_path / str(given["file"])
        stored = runtime.settings.files_dir / sample.name
        stored.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sample, stored)
        name = sample.name
    else:
        stored = runtime.settings.files_dir / f"{case.id}.txt"
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_text(str(given["text"]), encoding="utf-8")
        name = stored.name
    payload = {
        "source": "test",
        "file": str(stored),
        "original_name": name,
        "language": given.get("language", "de"),
    }
    for key in ("from", "to", "subject", "sender", "recipient"):
        if key in given:
            payload[key] = given[key]
    # Every other ``given`` key is a trigger field too (the contract's
    # inputs - title, organisation, ...), as a manual start would carry it.
    for key, value in given.items():
        if key not in ("file", "text", "trigger", "language") and key not in payload:
            payload[key] = value
    return payload


def _drive(runtime: Runtime, case_id: str, case: SidecarCase) -> None:
    """Tick until nothing moves: agent steps and actions run, timers fire
    at once, gates are decided as the case declares - an undeclared gate
    is where the run stops (and the expectation says whether that is
    right)."""
    decided: set[str] = set()
    for _ in range(MAX_TICKS):
        moved = runtime.tick()
        for row in runtime.engine.due_timers(datetime.max.replace(tzinfo=UTC)):
            runtime.engine.fire_timer(row["case_id"], row["step_id"], row_id=row["id"])
            moved += 1
        chain = {
            r["id"]
            for r in runtime.conn.execute(
                "SELECT id FROM cases WHERE chain_id = ? OR id = ?", (case_id, case_id)
            ).fetchall()
        }
        for gate in runtime.open_gates():
            key = f"{gate['case_id']}:{gate['step_id']}"
            outcome = case.decisions.get(gate["step_id"])
            if gate["case_id"] not in chain or outcome is None or key in decided:
                continue
            runtime.engine.decide_gate(
                gate["case_id"],
                gate["step_id"],
                outcome,
                actor_id="test-runner",
                comment="decided by sidecar case",
            )
            decided.add(key)
            moved += 1
        if not moved:
            break


def _trace(runtime: Runtime, case_id: str) -> tuple[list[str], dict[str, str]]:
    rows = runtime.conn.execute(
        "SELECT step_id, kind, status, outcome FROM steps WHERE case_id = ? ORDER BY started_at",
        (case_id,),
    ).fetchall()
    visited = [row["step_id"] for row in rows]
    outcomes = {row["step_id"]: row["outcome"] for row in rows if row["outcome"]}
    return visited, outcomes


def _compare_process(
    runtime: Runtime,
    case_id: str,
    case: SidecarCase,
    visited: list[str],
    outcomes: dict[str, str],
    definition: ProcessDefinition,
) -> list[str]:
    failures: list[str] = []
    expect = case.expect
    row = runtime.conn.execute("SELECT status FROM cases WHERE id = ?", (case_id,)).fetchone()
    status = row["status"] if row else "unknown"
    if "status" in expect and expect["status"] != status:
        failures.append(f"status: expected {expect['status']!r}, got {status!r}")
    if "outcome" in expect:
        last = None
        for sid in visited:
            if definition.steps[sid].kind is not StepKind.TRIGGER and sid in outcomes:
                last = outcomes[sid]
        if last != expect["outcome"]:
            failures.append(f"outcome: expected {expect['outcome']!r}, got {last!r}")
    for sid, wanted in (expect.get("outcomes") or {}).items():
        got = outcomes.get(sid)
        if got != wanted:
            failures.append(f"outcomes.{sid}: expected {wanted!r}, got {got!r}")
    if "path" in expect and not _subsequence(list(expect["path"]), visited):
        failures.append(f"path: expected {list(expect['path'])} within {visited}")
    if "gates" in expect:
        gates = [sid for sid in visited if definition.steps[sid].kind is StepKind.HUMAN_GATE]
        if sorted(expect["gates"]) != sorted(gates):
            failures.append(f"gates: expected {sorted(expect['gates'])}, got {sorted(gates)}")
    if "waiting_at" in expect:
        current = runtime.conn.execute(
            "SELECT current_step_id FROM cases WHERE id = ?", (case_id,)
        ).fetchone()["current_step_id"]
        if current != expect["waiting_at"]:
            failures.append(f"waiting_at: expected {expect['waiting_at']!r}, got {current!r}")
    for sid, fields in (expect.get("results") or {}).items():
        result_row = runtime.conn.execute(
            "SELECT result FROM steps WHERE case_id = ? AND step_id = ? ORDER BY started_at DESC",
            (case_id, sid),
        ).fetchone()
        result = json.loads(result_row["result"]) if result_row and result_row["result"] else {}
        failures.extend(f"results.{sid}.{f}" for f in _subset_mismatch(fields, result))
    if "chain" in expect:
        successors = [
            r["template_id"]
            for r in runtime.conn.execute(
                "SELECT template_id FROM cases WHERE chain_id = ? AND id != ? ORDER BY created_at",
                (case_id, case_id),
            ).fetchall()
        ]
        if list(expect["chain"]) != successors:
            failures.append(f"chain: expected {list(expect['chain'])}, got {successors}")
    if "audit" in expect:
        events = [
            r["event"]
            for r in runtime.conn.execute(
                "SELECT event FROM audit_log WHERE case_id = ? ORDER BY seq", (case_id,)
            ).fetchall()
        ]
        for event in expect["audit"]:
            if event not in events:
                failures.append(f"audit: event {event!r} missing in {events}")
    return failures


def _subsequence(wanted: list, actual: list) -> bool:
    index = 0
    for item in actual:
        if index < len(wanted) and item == wanted[index]:
            index += 1
    return index == len(wanted)


def _subset_mismatch(wanted: dict, actual: dict) -> list[str]:
    out: list[str] = []
    if not isinstance(actual, dict):
        return [f"{k}: no structured result" for k in wanted]
    for key, value in wanted.items():
        got = actual.get(key)
        if isinstance(value, dict) and isinstance(got, dict):
            out.extend(f"{key}.{inner}" for inner in _subset_mismatch(value, got))
        elif got != value:
            out.append(f"{key}: expected {value!r}, got {got!r}")
    return out


# -- briefing (ai / skill) cases ---------------------------------------------


def _run_briefing_case(
    app: App, solution_path: Path, case: SidecarCase, client: ModelClient | None
) -> list[str]:
    table = app.briefings if case.subject_kind == "ai" else app.skills
    briefing = table[case.subject_id]
    # The briefing is proven as a process would see it: constants
    # substituted, so the prompt hash matches the recordings of the runs.
    config = _substitute(
        briefing.to_config(app.skills), None, f"ai/{case.subject_id}", case.subject_id,
        app.constants,
    )
    if client is None:
        return ["no model client: the configuration does not replay and none was given"]
    if "file" in case.given:
        if not sample_inside(solution_path, str(case.given["file"])):
            raise ValueError(f"given.file {case.given['file']!r} is not a sample of the solution")
        from .document import extract_text

        text = extract_text(str(solution_path / str(case.given["file"]))).text
        name = Path(str(case.given["file"])).name
    else:
        text = str(case.given["text"])
        name = f"{case.id}.txt"
    payload: dict = {"prior_steps": {}, "trigger": {"source": "test", "original_name": name}}
    if config.get("read_document") or (config.get("context") or {}).get("document"):
        payload["documents"] = [{"name": name, "method": "text", "text": text}]
    else:
        payload["text"] = text
    for key in ("records", "knowledge"):
        if key in case.given:
            payload[key] = case.given[key]
    try:
        response = client.complete_structured(
            task=str(config.get("task", "")), payload=payload, schema_hint=config.get("output")
        )
    except ModelError as exc:
        return [str(exc)]
    failures: list[str] = []
    threshold = config.get("confidence_threshold")
    outcome = config.get("success_outcome", "done")
    if threshold is not None and response.confidence < float(threshold):
        outcome = "uncertain"
    if "error" in response.data and response.confidence == 0.0:
        outcome = "failed"
    if "outcome" in case.expect and case.expect["outcome"] != outcome:
        failures.append(f"outcome: expected {case.expect['outcome']!r}, got {outcome!r}")
    if "fields" in case.expect:
        failures.extend(
            f"fields.{f}" for f in _subset_mismatch(case.expect["fields"], response.data)
        )
    if "criteria" in case.expect:
        failures.extend(_judge(app, client, case, response.data))
    return failures


def _judge(app: App, client: ModelClient, case: SidecarCase, answer: dict) -> list[str]:
    """Free-text criteria are judged by the ``judge`` model role - a
    recorded answer under ``test`` like any other (ADR-0035 §6)."""
    criteria = case.expect["criteria"]
    if isinstance(criteria, str):
        criteria = [criteria]
    briefing = aimn.Briefing(
        id="judge",
        role={"en": "You are a strict reviewer of another model's structured answer."},
        instructions={
            "en": "For every criterion decide whether the answer satisfies it. Answer with"
            ' {"verdicts": [{"criterion": "...", "satisfied": true|false, "reason": "..."}]}.'
        },
        model_role="judge",
        output={"verdicts": "one verdict per criterion"},
    )
    config = briefing.to_config()
    try:
        response = client.complete_structured(
            task=config["task"],
            payload={"criteria": list(criteria), "answer": answer},
            schema_hint=config["output"],
        )
    except ModelError as exc:
        return [f"judge: {exc}"]
    failures: list[str] = []
    verdicts = response.data.get("verdicts") if isinstance(response.data, dict) else None
    if not isinstance(verdicts, list):
        return ["judge: no verdicts in the answer"]
    for verdict in verdicts:
        if isinstance(verdict, dict) and not verdict.get("satisfied", False):
            failures.append(
                f"criterion {verdict.get('criterion')!r} not satisfied: {verdict.get('reason', '')}"
            )
    return failures


# -- decision cases ------------------------------------------------------------


def _run_decision_case(app: App, case: SidecarCase) -> list[str]:
    from .dmn import DmnError, evaluate

    decision = app.decisions[case.subject_id]
    try:
        outputs = evaluate(decision, dict(case.given["inputs"]))
    except DmnError as exc:
        return [str(exc)]
    return [f"outputs.{f}" for f in _subset_mismatch(dict(case.expect["outputs"]), outputs)]


# -- reporting ------------------------------------------------------------------


def format_report(report: TestReport) -> str:
    lines = [
        f"{report.solution_id} {report.solution_version} under '{report.configuration}':"
        f" {report.passed}/{len(report.results)} passed"
        + ("" if report.ok else " - FAILED")
        + f" ({report.duration_ms} ms)"
    ]
    for error in report.errors:
        lines.append(f"  error: {error}")
    for result in report.results:
        mark = "ok  " if result.passed else "FAIL"
        suffix = f" (pass^{result.attempts})" if result.attempts > 1 else ""
        lines.append(
            f"  {mark} {result.subject_kind}/{result.subject_id} :: {result.case_id}{suffix}"
        )
        for failure in result.failures:
            lines.append(f"         {failure}")
    if report.coverage:
        lines.append("  coverage:")
        for pid, cov in report.coverage.items():
            lines.append(
                f"    {pid}: steps {len(cov.steps_visited)}/{cov.steps_total},"
                f" outcomes {len(cov.outcomes_visited)}/{cov.outcomes_total}"
            )
    if report.missing_recordings:
        lines.append("  missing recordings (run with --record from the workshop):")
        lines.extend(f"    {key}" for key in report.missing_recordings)
    return "\n".join(lines)
