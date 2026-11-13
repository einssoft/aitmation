"""Solution format 2 (ADR-0035): layout, configurations, recordings, runner.

The shipped ``document-basics`` solution is the honesty test: migrated
from format 1, it validates, its sidecar cases run green under ``test``
with recorded answers, and the migration tool reproduces exactly that
from the format-1 fixture.
"""

import json
from pathlib import Path

import pytest

from aitmation_core import solution
from aitmation_core.apps import load_app, validate_app
from aitmation_core.config import Settings
from aitmation_core.migrate import migrate_solution
from aitmation_core.model import ModelError, ModelResponse
from aitmation_core.recordings import RecordingClient, canonical, recording_key
from aitmation_core.runtime import Runtime
from aitmation_core.template import load_templates
from aitmation_core.testing import format_report, run_solution_tests

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
SOLUTION = REPO / "apps" / "document-basics"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_shipped_solution_is_format_2_and_valid():
    report = validate_app(SOLUTION, core_definitions=load_templates(CORE_TEMPLATES))
    assert report.ok, [str(f) for f in report.errors]
    app = report.app
    assert app.format == 2
    assert set(app.templates) == {
        "document-read",
        "document-review",
        "document-archive",
        "follow-up",
    }
    assert "read" in app.briefings and app.briefings["read"].model_role == "default"
    assert "default" in app.manifest.model_roles
    assert app.configurations["test"].replays_models
    assert {c.subject_kind for c in app.sidecars} == {"process", "ai"}


def test_shipped_solution_passes_its_sidecar_tests_under_test_configuration():
    report = run_solution_tests(SOLUTION, core_templates_dir=CORE_TEMPLATES)
    assert report.ok, format_report(report)
    assert report.coverage["document-read"].step_ratio == 1.0
    by_id = {r.case_id: r for r in report.results}
    assert by_id["invoice-summary-has-no-anomalies"].attempts == 3  # pass^3
    assert not report.missing_recordings


def test_missing_recording_fails_loudly_instead_of_calling_out(tmp_path):
    client = RecordingClient(tmp_path, mode="replay")
    with pytest.raises(ModelError, match="no recording"):
        client.complete_structured("task", {"x": 1}, {"y": "desc"})
    assert client.missing


def test_recording_keys_ignore_run_specific_noise():
    a = {"trigger": {"file": "/tmp/run-1/files/0123456789abcdef0123456789abcdef_x.txt"}}
    b = {"trigger": {"file": "/var/other/files/fedcba9876543210fedcba9876543210_x.txt"}}
    assert canonical(a) == canonical(b) == {"trigger": {"file": "x.txt"}}
    assert recording_key("t", a, None) == recording_key("t", b, None)
    assert recording_key("t", a, None) != recording_key("t2", a, None)


class _Fake:
    clearance = "sensitive"
    local = True
    entries = ()

    def complete_structured(self, task, payload, schema_hint=None):
        return ModelResponse({"summary": "s", "anomalies": [], "confidence": 0.9}, "fake@1", 0.9)


def test_record_mode_writes_replayable_files(tmp_path):
    recorder = RecordingClient(tmp_path, mode="record", inner=_Fake())
    first = recorder.complete_structured("task", {"doc": "x"}, {"summary": "s"})
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text())
    assert stored["task"] == "task" and stored["response"]["data"]["summary"] == "s"
    replay = RecordingClient(tmp_path, mode="replay")
    again = replay.complete_structured("task", {"doc": "x"}, {"summary": "s"})
    assert again.data == first.data and again.model_ref.startswith("recording:")


def test_migration_of_the_format_1_fixture_reproduces_a_valid_solution(tmp_path):
    target = tmp_path / "document-basics"
    migrate_solution(FIXTURES / "document-basics-v1", target)
    assert (target / "solution.toml").is_file()
    assert sorted(p.name for p in (target / "processes").glob("*.bpmn")) == [
        "document-archive.bpmn",
        "document-read.bpmn",
        "document-review.bpmn",
        "follow-up.bpmn",
    ]
    briefing = (target / "ai" / "read.aimn.toml").read_text()
    assert "{param:document_type}" in briefing  # unbound, as written
    assert 'model_role = "default"' in briefing
    app = load_app(target, core_definitions=load_templates(CORE_TEMPLATES))
    assert app.format == 2
    # The bound process text equals what the TOML loader produced.
    original = load_templates(FIXTURES / "document-basics-v1" / "templates")["document-read"]
    assert (
        app.templates["document-read"]
        .steps["read"]
        .config["task"]
        .startswith(original.steps["read"].config["task"])
    )


def _solution(tmp_path: Path, *, tool_mode: str = "stub") -> Path:
    """A tiny format-2 solution with an external tool after a gate."""
    root = tmp_path / "apps" / "demo-tool"
    (root / "processes").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "configurations").mkdir()
    (root / "solution.toml").write_text(
        'format = 2\nid = "demo-tool"\nversion = "0.1.0"\nname = "Demo"\n'
        '[requires]\ndata_class = "internal"\n',
        encoding="utf-8",
    )
    (root / "tools" / "send-mail.toml").write_text(
        'id = "send-mail"\nname = "Mail senden"\ndescription = "Sends a mail to the sender."\n'
        'effect = "external"\naction = "notify_sender"\n',
        encoding="utf-8",
    )
    (root / "configurations" / "test.toml").write_text(
        f'name = "test"\n[tools.send-mail]\nmode = "{tool_mode}"\noutcome = "done"\n'
        'result = { sent = false, note = "stubbed" }\n',
        encoding="utf-8",
    )
    (root / "processes" / "notify.bpmn").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:aimn="https://aitmation.com/schema/aimn/1" id="d" targetNamespace="x">
<bpmn:process id="notify" isExecutable="true">
  <bpmn:startEvent id="intake"><bpmn:extensionElements><aimn:trigger source="manual"/></bpmn:extensionElements></bpmn:startEvent>
  <bpmn:userTask id="approve"><bpmn:extensionElements><aimn:gate approving="approved"/></bpmn:extensionElements></bpmn:userTask>
  <bpmn:serviceTask id="mail"><bpmn:extensionElements><aimn:toolCall action="send-mail"/></bpmn:extensionElements></bpmn:serviceTask>
  <bpmn:endEvent id="finished"/>
  <bpmn:sequenceFlow id="f1" sourceRef="intake" targetRef="approve"/>
  <bpmn:sequenceFlow id="f2" name="approved" sourceRef="approve" targetRef="mail"/>
  <bpmn:sequenceFlow id="f3" name="rejected" sourceRef="approve" targetRef="finished"/>
  <bpmn:sequenceFlow id="f4" name="done" sourceRef="mail" targetRef="finished"/>
  <bpmn:sequenceFlow id="f5" name="failed" sourceRef="mail" targetRef="finished"/>
</bpmn:process>
</bpmn:definitions>
""",
        encoding="utf-8",
    )
    (root / "processes" / "notify.test.toml").write_text(
        '[[case]]\nid = "mail-is-stubbed"\ngiven = { trigger = { source = "manual" } }\n'
        'decisions = { approve = "approved" }\n'
        '[case.expect]\nstatus = "completed"\noutcomes = { mail = "done" }\n'
        'results = { mail = { tool_mode = "stub", note = "stubbed" } }\n',
        encoding="utf-8",
    )
    return root


def test_tool_declaration_binds_action_and_effect(tmp_path):
    root = _solution(tmp_path)
    app = load_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    mail = app.templates["notify"].steps["mail"].config
    assert mail == {
        "action": "notify_sender", "tool": "send-mail", "effect": "external", "on_error": "failed",
    }


def test_tool_without_error_path_is_rejected(tmp_path):
    root = _solution(tmp_path)
    xml = (root / "processes" / "notify.bpmn").read_text()
    (root / "processes" / "notify.bpmn").write_text(
        xml.replace(
            '<bpmn:sequenceFlow id="f5" name="failed" sourceRef="mail" targetRef="finished"/>', ""
        )
    )
    report = validate_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    assert any("error path" in str(f) for f in report.errors)


def test_sidecar_runs_the_tool_as_stub_under_test(tmp_path):
    root = _solution(tmp_path)
    report = run_solution_tests(root, core_templates_dir=CORE_TEMPLATES)
    assert report.ok, format_report(report)
    assert report.results[0].outcomes["mail"] == "done"


def test_shadow_configuration_logs_external_actions(tmp_path):
    root = _solution(tmp_path)
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=root.parent,
        configuration="shadow",
    )
    runtime = Runtime(settings)
    try:
        assert runtime.configuration.name == "shadow"
        case_id = runtime.engine.start_case("notify", {"source": "manual"})
        runtime.engine.decide_gate(case_id, "approve", "approved", actor_id="alex")
        runtime.tick()
        row = runtime.conn.execute(
            "SELECT outcome, result FROM steps WHERE case_id = ? AND step_id = 'mail'", (case_id,)
        ).fetchone()
        assert row["outcome"] == "done"
        result = json.loads(row["result"])
        assert result["would_have_run"] is True and result["configuration"] == "shadow"
        audit = runtime.conn.execute(
            "SELECT details FROM audit_log WHERE case_id = ? AND event = 'step_completed'"
            " ORDER BY seq DESC",
            (case_id,),
        ).fetchone()
        assert json.loads(audit["details"])["tool_mode"] == "log"
    finally:
        runtime.close()


def test_standard_configuration_runs_unknown_external_action_to_failed(tmp_path):
    root = _solution(tmp_path)
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=root.parent
    )
    runtime = Runtime(settings)
    try:
        case_id = runtime.engine.start_case("notify", {"source": "manual"})
        runtime.engine.decide_gate(case_id, "approve", "approved", actor_id="alex")
        runtime.tick()
        row = runtime.conn.execute(
            "SELECT outcome FROM steps WHERE case_id = ? AND step_id = 'mail'", (case_id,)
        ).fetchone()
        assert row["outcome"] == "failed"  # no connector layer yet: visible, not silent
    finally:
        runtime.close()


def test_model_roles_are_bound_against_the_registry(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=REPO / "apps"
    )
    runtime = Runtime(settings)
    try:
        bindings = runtime.model_role_bindings()
        assert "document-basics" in bindings
        default = bindings["document-basics"]["default"]
        assert default["model"] is not None  # the built-in registry has released entries
        assert default["max_data_class"] == "internal"
    finally:
        runtime.close()


def test_configuration_file_may_only_refine_reserved_names():
    text = 'name = "test"\n[tools."*"]\nmode = "log"\n'
    cfg = solution.parse_configuration(text, "configurations/test.toml", "test")
    assert cfg.replays_models and cfg.default_tool_mode == "log" and cfg.notifications == "log"
    with pytest.raises(solution.SolutionError, match="mode must be one of"):
        solution.parse_configuration('name = "x"\n[tools.a]\nmode = "fire"\n', "c", "x")


def test_sidecar_validation_catches_wrong_expectations(tmp_path):
    root = _solution(tmp_path)
    (root / "processes" / "notify.test.toml").write_text(
        '[[case]]\nid = "bad"\ngiven = { trigger = {} }\n'
        '[case.expect]\noutcomes = { mail = "sent" }\npath = ["nowhere"]\n',
        encoding="utf-8",
    )
    report = validate_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    messages = [f.message for f in report.errors]
    assert any("no outcome of 'mail'" in m for m in messages)
    assert any("expect.path" in m for m in messages)


def test_constants_substitute_into_processes_and_briefings(tmp_path):
    """``{const:table.key}`` from constants/ resolves at load time, in
    briefings (hence in the prompt hash) and in step config; an unknown
    constant is a load error, a table is not a value."""
    from aitmation_core.apps import validate_app
    from aitmation_core.template import load_templates

    root = tmp_path / "apps" / "demo-const"
    for folder in ("processes", "ai", "constants"):
        (root / folder).mkdir(parents=True)
    (root / "solution.toml").write_text(
        'format = 2\nid = "demo-const"\nversion = "0.1.0"\nname = "Demo"\n'
        "[model_roles.default]\nmax_data_class = \"internal\"\n",
        encoding="utf-8",
    )
    (root / "constants" / "company.toml").write_text(
        '[company]\ncurrency = "CHF"\nname = "Muster AG"\n', encoding="utf-8"
    )
    (root / "ai" / "read.aimn.toml").write_text(
        'id = "read"\nmodel_role = "default"\n'
        '[instructions]\nen = "Report the total in {const:company.currency} for {const:company.name}."\n'
        '[output]\ntotal = "amount in {const:company.currency}"\n',
        encoding="utf-8",
    )
    process = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:aimn="https://aitmation.com/schema/aimn/1" id="d" targetNamespace="x">
  <bpmn:process id="reading" isExecutable="true">
    <bpmn:startEvent id="intake"><bpmn:extensionElements><aimn:trigger source="manual"/></bpmn:extensionElements></bpmn:startEvent>
    <bpmn:task id="read"><bpmn:extensionElements><aimn:aiTask ref="read"/></bpmn:extensionElements></bpmn:task>
    <bpmn:endEvent id="finished"/>
    <bpmn:sequenceFlow id="f1" sourceRef="intake" targetRef="read"/>
    <bpmn:sequenceFlow id="f2" name="done" sourceRef="read" targetRef="finished"/>
    <bpmn:sequenceFlow id="f3" name="failed" sourceRef="read" targetRef="finished"/>
  </bpmn:process>
</bpmn:definitions>
"""
    (root / "processes" / "reading.bpmn").write_text(process, encoding="utf-8")
    core = load_templates(Path(__file__).resolve().parents[2] / "core" / "templates")
    report = validate_app(root, core_definitions=core)
    assert report.ok, [str(f) for f in report.errors]
    config = report.app.templates["reading"].steps["read"].config
    assert "Report the total in CHF for Muster AG." in config["task"]
    assert config["output"]["total"] == "amount in CHF"
    assert "{const:" not in config["task"]

    (root / "ai" / "read.aimn.toml").write_text(
        'id = "read"\nmodel_role = "default"\n[instructions]\nen = "{const:company.vat}"\n',
        encoding="utf-8",
    )
    report = validate_app(root, core_definitions=core)
    assert not report.ok and any("unknown constant" in str(f) for f in report.errors)
    (root / "ai" / "read.aimn.toml").write_text(
        'id = "read"\nmodel_role = "default"\n[instructions]\nen = "{const:company}"\n',
        encoding="utf-8",
    )
    report = validate_app(root, core_definitions=core)
    assert not report.ok and any("is a table" in str(f) for f in report.errors)
