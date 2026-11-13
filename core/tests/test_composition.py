"""Composition (ADR-0034 §1 callActivity, ADR-0035 §4 packages and dependencies)."""

import json
from pathlib import Path

import pytest

from aitmation_core.apps import AppError, load_app, load_apps, validate_app, version_satisfies
from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.template import load_templates
from aitmation_core.testing import format_report, run_solution_tests

CORE_TEMPLATES = Path(__file__).resolve().parents[2] / "core" / "templates"
HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:aimn="https://aitmation.com/schema/aimn/1" id="d" targetNamespace="x">
"""
TAIL = "</bpmn:definitions>\n"


def _manifest(root: Path, solution_id: str, version: str = "0.1.0", deps: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "solution.toml").write_text(
        f'format = 2\nid = "{solution_id}"\nversion = "{version}"\nname = "{solution_id}"\n'
        + (f"[dependencies]\n{deps}\n" if deps else ""),
        encoding="utf-8",
    )


def _process(root: Path, pid: str, body: str, folder: str = "processes") -> None:
    (root / folder).mkdir(parents=True, exist_ok=True)
    (root / folder / f"{pid}.bpmn").write_text(
        HEAD + f'<bpmn:process id="{pid}" isExecutable="true">' + body + "</bpmn:process>" + TAIL,
        encoding="utf-8",
    )


CHILD = """
  <bpmn:startEvent id="intake"><bpmn:extensionElements><aimn:trigger source="chain"/></bpmn:extensionElements></bpmn:startEvent>
  <bpmn:userTask id="check"><bpmn:extensionElements><aimn:gate approving="approved"/></bpmn:extensionElements></bpmn:userTask>
  <bpmn:serviceTask id="archive"><bpmn:extensionElements><aimn:toolCall action="archive"/></bpmn:extensionElements></bpmn:serviceTask>
  <bpmn:endEvent id="finished"/>
  <bpmn:sequenceFlow id="f1" sourceRef="intake" targetRef="check"/>
  <bpmn:sequenceFlow id="f2" name="approved" sourceRef="check" targetRef="archive"/>
  <bpmn:sequenceFlow id="f3" name="rejected" sourceRef="check" targetRef="finished"/>
  <bpmn:sequenceFlow id="f4" name="done" sourceRef="archive" targetRef="finished"/>
"""
PARENT = """
  <bpmn:startEvent id="intake"><bpmn:extensionElements><aimn:trigger source="manual"/></bpmn:extensionElements></bpmn:startEvent>
  <bpmn:callActivity id="delegate" calledElement="child"/>
  <bpmn:serviceTask id="export"><bpmn:extensionElements><aimn:toolCall action="export"/></bpmn:extensionElements></bpmn:serviceTask>
  <bpmn:endEvent id="finished"/>
  <bpmn:sequenceFlow id="f1" sourceRef="intake" targetRef="delegate"/>
  <bpmn:sequenceFlow id="f2" name="done" sourceRef="delegate" targetRef="export"/>
  <bpmn:sequenceFlow id="f3" name="done" sourceRef="export" targetRef="finished"/>
"""


def test_call_activity_waits_for_the_child_and_continues(tmp_path):
    root = tmp_path / "apps" / "demo-call"
    _manifest(root, "demo-call")
    _process(root, "child", CHILD)
    _process(root, "parent", PARENT)
    app = load_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    assert app.templates["parent"].steps["delegate"].config == {
        "action": "call",
        "process": "child",
    }
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=root.parent
    )
    runtime = Runtime(settings)
    try:
        parent = runtime.engine.start_case("parent", {"source": "manual", "subject": "x"})
        runtime.tick()  # call -> child started, parent waits
        row = runtime.conn.execute(
            "SELECT status FROM steps WHERE case_id = ? AND step_id = 'delegate'", (parent,)
        ).fetchone()
        assert row["status"] == "waiting"
        child = runtime.conn.execute(
            "SELECT id, chain_id, parent_case_id, parent_step_id FROM cases WHERE parent_case_id = ?",
            (parent,),
        ).fetchone()
        assert child["chain_id"] == parent and child["parent_step_id"] == "delegate"
        gates = runtime.open_gates()
        assert [g["step_id"] for g in gates] == ["check"]
        runtime.engine.decide_gate(child["id"], "check", "approved", actor_id="alex")
        runtime.tick()  # archive in the child -> child ends -> parent continues
        runtime.tick()  # export in the parent
        parent_row = runtime.conn.execute(
            "SELECT status FROM cases WHERE id = ?", (parent,)
        ).fetchone()
        assert parent_row["status"] == "completed"
        delegate = runtime.conn.execute(
            "SELECT outcome, result FROM steps WHERE case_id = ? AND step_id = 'delegate'",
            (parent,),
        ).fetchone()
        assert delegate["outcome"] == "done"
        assert json.loads(delegate["result"])["child_case"] == child["id"]
        events = [
            r["event"]
            for r in runtime.conn.execute(
                "SELECT event FROM audit_log WHERE case_id = ? ORDER BY seq", (parent,)
            ).fetchall()
        ]
        assert "subprocess_called" in events and events[-1] == "case_completed"
    finally:
        runtime.close()


def test_call_activity_case_runs_in_the_proving_run(tmp_path):
    root = tmp_path / "apps" / "demo-call"
    _manifest(root, "demo-call")
    _process(root, "child", CHILD)
    _process(root, "parent", PARENT)
    (root / "processes" / "parent.test.toml").write_text(
        '[[case]]\nid = "delegation-round-trip"\ngiven = { trigger = { source = "manual" } }\n'
        'decisions = { check = "approved" }\n'
        '[case.expect]\nstatus = "completed"\noutcomes = { delegate = "done", export = "done" }\n'
        'audit = ["subprocess_called"]\n',
        encoding="utf-8",
    )
    report = run_solution_tests(root, core_templates_dir=CORE_TEMPLATES)
    assert report.ok, format_report(report)


def test_unknown_call_target_is_a_validation_error(tmp_path):
    root = tmp_path / "apps" / "demo-call"
    _manifest(root, "demo-call")
    _process(root, "parent", PARENT)
    report = validate_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    assert any("calls process 'child' not in sight" in f.message for f in report.errors)


SKILL = 'id = "amounts"\ninstructions = "Extract all amounts."\n[output]\namounts = "list"\n'
AI_TASK = """id = "read"
skills = ["amounts"]
instructions = "Summarize."
read_document = true
[output]
summary = "s"
"""
AI_PROCESS = """
  <bpmn:startEvent id="intake"><bpmn:extensionElements><aimn:trigger source="chain"/></bpmn:extensionElements></bpmn:startEvent>
  <bpmn:task id="read"><bpmn:extensionElements><aimn:aiTask ref="read"/></bpmn:extensionElements></bpmn:task>
  <bpmn:endEvent id="finished"/>
  <bpmn:sequenceFlow id="f1" sourceRef="intake" targetRef="read"/>
  <bpmn:sequenceFlow id="f2" name="done" sourceRef="read" targetRef="finished"/>
  <bpmn:sequenceFlow id="f3" name="failed" sourceRef="read" targetRef="finished"/>
"""


def test_packages_see_their_parents_but_not_their_siblings(tmp_path):
    root = tmp_path / "apps" / "demo-pkg"
    _manifest(root, "demo-pkg")
    (root / "skills" / "amounts").mkdir(parents=True)
    (root / "skills" / "amounts" / "skill.toml").write_text(SKILL, encoding="utf-8")
    pkg_a = root / "packages" / "receivables"
    (pkg_a / "ai").mkdir(parents=True)
    (pkg_a / "ai" / "read.aimn.toml").write_text(AI_TASK, encoding="utf-8")
    _process(pkg_a, "read-invoice", AI_PROCESS)
    app = load_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    assert "read-invoice" in app.templates  # flattened for the engine
    assert app.packages["receivables"]["processes"] == ["read-invoice"]
    assert (
        app.templates["read-invoice"]
        .steps["read"]
        .config["task"]
        .startswith("Extract all amounts.\nSummarize.")
    )
    # A sibling package cannot see receivables' briefing.
    pkg_b = root / "packages" / "payroll"
    _process(pkg_b, "read-payslip", AI_PROCESS)
    report = validate_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    assert any(
        "references briefing 'read'" in f.message and f.where.startswith("packages/payroll/")
        for f in report.errors
    )


def test_ids_stay_unique_across_packages(tmp_path):
    root = tmp_path / "apps" / "demo-pkg"
    _manifest(root, "demo-pkg")
    for name in ("a", "b"):
        pkg = root / "packages" / name
        (pkg / "ai").mkdir(parents=True)
        (pkg / "ai" / "read.aimn.toml").write_text(
            'id = "read"\ninstructions = "x"\n', encoding="utf-8"
        )
    report = validate_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    assert any("briefing id 'read' is already defined" in f.message for f in report.errors)


def test_packages_carry_no_manifest_and_no_configurations(tmp_path):
    root = tmp_path / "apps" / "demo-pkg"
    _manifest(root, "demo-pkg")
    pkg = root / "packages" / "x"
    (pkg / "configurations").mkdir(parents=True)
    (pkg / "configurations" / "test.toml").write_text('name = "test"\n', encoding="utf-8")
    report = validate_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    assert any("belongs to the solution root" in f.message for f in report.errors)


@pytest.mark.parametrize(
    ("version", "spec", "ok"),
    [
        ("0.2.3", "^0.2", True),
        ("0.3.0", "^0.2", False),
        ("1.4.0", "^1.2", True),
        ("2.0.0", "^1.2", False),
        ("1.2.9", "~1.2.3", True),
        ("1.3.0", "~1.2.3", False),
        ("1.2.3", "1.2.3", True),
        ("1.2.4", "=1.2.3", False),
    ],
)
def test_version_ranges(version, spec, ok):
    assert version_satisfies(version, spec) is ok


def test_dependencies_resolve_chain_targets_across_solutions(tmp_path):
    apps_dir = tmp_path / "apps"
    base = apps_dir / "base"
    _manifest(base, "base", "0.2.1")
    _process(base, "child", CHILD)
    ext = apps_dir / "ext"
    _manifest(ext, "ext", "0.1.0", deps='base = "^0.2"')
    _process(
        ext,
        "starter",
        """
  <bpmn:startEvent id="intake"><bpmn:extensionElements><aimn:trigger source="manual"/></bpmn:extensionElements></bpmn:startEvent>
  <bpmn:endEvent id="handoff"><bpmn:extensionElements><aimn:chainTo process="child"/></bpmn:extensionElements></bpmn:endEvent>
  <bpmn:sequenceFlow id="f1" sourceRef="intake" targetRef="handoff"/>
""",
    )
    core = load_templates(CORE_TEMPLATES)
    single = validate_app(ext, core_definitions=core)
    assert single.ok and any("a dependency must provide" in f.message for f in single.warnings)
    apps = load_apps(apps_dir, core_definitions=core)
    assert set(apps) == {"base", "ext"}
    # Wrong range: install fails loudly.
    _manifest(ext, "ext", "0.1.0", deps='base = "^0.3"')
    _process(
        ext,
        "starter",
        '<bpmn:startEvent id="intake"/><bpmn:endEvent id="e"/><bpmn:sequenceFlow id="f" sourceRef="intake" targetRef="e"/>',
    )
    with pytest.raises(AppError, match="needs base"):
        load_apps(apps_dir, core_definitions=core)


def test_dependency_cycles_and_missing_dependencies_are_rejected(tmp_path):
    apps_dir = tmp_path / "apps"
    a = apps_dir / "a"
    b = apps_dir / "b"
    _manifest(a, "a", deps='b = "^0.1"')
    _manifest(b, "b", deps='a = "^0.1"')
    with pytest.raises(AppError, match="dependency cycle"):
        load_apps(apps_dir, core_definitions=load_templates(CORE_TEMPLATES))
    _manifest(b, "b", deps='zzz = "^0.1"')
    _manifest(a, "a")
    with pytest.raises(AppError, match="not installed"):
        load_apps(apps_dir, core_definitions=load_templates(CORE_TEMPLATES))


CALLS_INTAKE = """
  <bpmn:startEvent id="intake"><bpmn:extensionElements><aimn:trigger source="manual"/></bpmn:extensionElements></bpmn:startEvent>
  <bpmn:callActivity id="delegate" calledElement="document-intake"/>
  <bpmn:serviceTask id="export"><bpmn:extensionElements><aimn:toolCall action="export"/></bpmn:extensionElements></bpmn:serviceTask>
  <bpmn:endEvent id="finished"/>
  <bpmn:sequenceFlow id="f1" sourceRef="intake" targetRef="delegate"/>
  <bpmn:sequenceFlow id="f2" name="done" sourceRef="delegate" targetRef="export"/>
  <bpmn:sequenceFlow id="f3" name="done" sourceRef="export" targetRef="finished"/>
"""


def test_call_activity_waits_for_a_dispatched_chain(tmp_path):
    """A caller that calls the intake must not resume when the intake hop
    ends - only when the chain the dispatch started has ended."""
    root = tmp_path / "apps" / "demo-call-intake"
    _manifest(root, "demo-call-intake")
    _process(root, "child", CHILD)
    _process(root, "parent", CALLS_INTAKE)
    doctypes = tmp_path / "doctypes"
    doctypes.mkdir()
    (doctypes / "memo.toml").write_text(
        'id = "memo"\ntitle = "Memo"\nchain = "child"\nfeatures = ["memo"]\n',
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=root.parent,
        doctypes_dir=doctypes,
    )
    runtime = Runtime(settings)
    try:
        doc = tmp_path / "memo.txt"
        doc.write_text("memo: please read", encoding="utf-8")
        parent = runtime.engine.start_case(
            "parent", {"source": "manual", "subject": "x", "path": str(doc)}
        )
        for _ in range(3):
            runtime.tick()  # call -> intake; stub classifier sorts out -> gate
        gate = next(g for g in runtime.open_gates() if g["template_id"] == "document-intake")
        runtime.categorize(gate["case_id"], gate["step_id"], "memo", actor_id="tester")
        for _ in range(3):
            runtime.tick()  # dispatch -> child started, child waits at its gate

        def parent_step():
            return runtime.conn.execute(
                "SELECT status FROM steps WHERE case_id = ? AND step_id = 'delegate'", (parent,)
            ).fetchone()["status"]

        assert parent_step() == "waiting"
        child_gate = next(g for g in runtime.open_gates() if g["template_id"] == "child")
        runtime.decide(child_gate["case_id"], child_gate["step_id"], "approved", actor_id="t")
        for _ in range(3):
            runtime.tick()
        assert parent_step() == "completed"
        assert runtime.conn.execute(
            "SELECT status FROM cases WHERE id = ?", (parent,)
        ).fetchone()["status"] == "completed"
    finally:
        runtime.close()


def test_a_dependent_solution_is_provable_and_may_call_across(tmp_path):
    apps_dir = tmp_path / "apps"
    base = apps_dir / "base"
    _manifest(base, "base", "0.2.1")
    _process(base, "child", CHILD)
    ext = apps_dir / "ext"
    _manifest(ext, "ext", "0.1.0", deps='base = "^0.2"')
    _process(ext, "parent", PARENT)  # calls "child", which only base provides
    (ext / "processes" / "parent.test.toml").write_text(
        '[[case]]\nid = "across"\ngiven = { trigger = { source = "manual" } }\n'
        'decisions = { check = "approved" }\n'
        '[case.expect]\nstatus = "completed"\noutcomes = { delegate = "done" }\n',
        encoding="utf-8",
    )
    core = load_templates(CORE_TEMPLATES)
    single = validate_app(ext, core_definitions=core)
    assert single.ok and any("not in sight" in f.message for f in single.warnings)
    assert set(load_apps(apps_dir, core_definitions=core)) == {"base", "ext"}
    report = run_solution_tests(ext, core_templates_dir=CORE_TEMPLATES)
    assert report.ok, format_report(report)
    # Without the dependency installed the call target is missing, loudly.
    _manifest(ext, "ext", "0.1.0", deps='other = "^1.0"')
    with pytest.raises(AppError, match="not installed"):
        load_apps(apps_dir, core_definitions=core)


def test_dependencies_provide_briefings_skills_and_tools(tmp_path):
    """Over solution boundaries only through [dependencies]: the root
    elements of a dependency are in sight; alone, unresolved references
    are warnings; installed, they resolve - or fail; ids do not shadow."""
    apps_dir = tmp_path / "apps"
    base = apps_dir / "base"
    _manifest(base, "base", "0.2.1")
    (base / "skills" / "amounts").mkdir(parents=True)
    (base / "skills" / "amounts" / "skill.toml").write_text(SKILL, encoding="utf-8")
    (base / "ai").mkdir()
    (base / "ai" / "read.aimn.toml").write_text(AI_TASK, encoding="utf-8")
    (base / "tools").mkdir()
    (base / "tools" / "notify.toml").write_text(
        'id = "notify"\nname = "Notify"\ndescription = "Tells the sender."\n'
        'effect = "external"\naction = "send_mail"\non_error = "failed"\n',
        encoding="utf-8",
    )
    ext = apps_dir / "ext"
    _manifest(ext, "ext", "0.1.0", deps='base = "^0.2"')
    _process(ext, "read-invoice", AI_PROCESS)  # aimn:aiTask ref="read" from base
    core = load_templates(CORE_TEMPLATES)
    alone = validate_app(ext, core_definitions=core)
    assert alone.ok and any("references briefing 'read'" in str(f) for f in alone.warnings)
    assert "read-invoice" not in alone.app.templates  # cannot be built alone
    apps = load_apps(apps_dir, core_definitions=core)
    task = apps["ext"].templates["read-invoice"].steps["read"].config["task"]
    assert task.startswith("Extract all amounts.\nSummarize.")
    assert "read" not in apps["ext"].briefings  # provided, not owned
    # The proving run loads the dependency too (no model: the step stubs).
    (ext / "processes" / "read-invoice.test.toml").write_text(
        '[[case]]\nid = "stubbed"\ngiven = { trigger = { source = "chain" } }\n'
        '[case.expect]\nstatus = "completed"\noutcomes = { read = "done" }\n',
        encoding="utf-8",
    )
    report = run_solution_tests(ext, configuration="standard", core_templates_dir=CORE_TEMPLATES)
    assert report.ok, format_report(report)
    # Shadowing a dependency's element is an error.
    (ext / "ai").mkdir()
    (ext / "ai" / "read.aimn.toml").write_text(AI_TASK, encoding="utf-8")
    with pytest.raises(AppError, match="already defined in dependency base"):
        load_apps(apps_dir, core_definitions=core)
