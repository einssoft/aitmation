"""Parallel branches and multi-instance steps (ADR-0037): the definition
checks, the multi-token engine, the BPMN profile, the proving run."""

from pathlib import Path

import pytest

from aitmation_core.apps import load_app
from aitmation_core.bpmn import BpmnError, parse_bpmn, to_bpmn
from aitmation_core.config import Settings
from aitmation_core.definition import (
    DefinitionError,
    ProcessDefinition,
    StepDefinition,
    StepKind,
)
from aitmation_core.engine import Engine, EngineError
from aitmation_core.runtime import Runtime
from aitmation_core.template import load_templates
from aitmation_core.testing import format_report, run_solution_tests

CORE_TEMPLATES = Path(__file__).resolve().parents[2] / "core" / "templates"


def _step(sid, kind, nxt, config=None, forks=None):
    return StepDefinition(id=sid, kind=kind, next=nxt, config=config or {}, forks=forks or {})


def _parallel(internal_action="export", gate_in_branch=False):
    """start -> prepare -(done)-> [a, b] -> join -> finish -> end."""
    b_next = {"done": "join"}
    steps = {
        "start": _step("start", StepKind.TRIGGER, {"fired": "prepare"}),
        "prepare": _step(
            "prepare", StepKind.AGENT_STEP, {"done": "a", "failed": "a"},
            {"task": "t"}, forks={"done": ("a", "b")},
        ),
        "a": _step("a", StepKind.AGENT_STEP, {"done": "join", "failed": "join"}, {"task": "a"}),
        "b": _step("b", StepKind.HUMAN_GATE if gate_in_branch else StepKind.AGENT_STEP,
                   {"approved": "join", "rejected": "join"} if gate_in_branch else b_next,
                   {} if gate_in_branch else {"task": "b"}),
        "join": _step("join", StepKind.SYSTEM_ACTION, {"done": "finish"}, {"action": "join"}),
        "finish": _step("finish", StepKind.SYSTEM_ACTION, {"done": None},
                        {"action": internal_action}),
    }
    return ProcessDefinition(id="par", version=1, steps=steps)


# -- definition ---------------------------------------------------------------


def test_fork_join_pairing_is_validated():
    definition = _parallel()
    definition.validate()
    assert definition.joins() == {"join": ("prepare", "done", ("a", "b"))}
    assert set(definition.branch_steps()) == {"a", "b"}

    # A branch that ends the process instead of joining.
    broken = _parallel()
    broken.steps["b"] = _step("b", StepKind.AGENT_STEP, {"done": None}, {"task": "b"})
    with pytest.raises(DefinitionError, match="leaves the process"):
        broken.validate()
    # A join without a split.
    lonely = ProcessDefinition(
        id="l", version=1,
        steps={
            "start": _step("start", StepKind.TRIGGER, {"fired": "join"}),
            "join": _step("join", StepKind.SYSTEM_ACTION, {"done": None}, {"action": "join"}),
        },
    )
    with pytest.raises(DefinitionError, match="no parallel split"):
        lonely.validate()
    # Serial-only actions may not sit in a branch.
    serial = _parallel()
    serial.steps["b"] = _step(
        "b", StepKind.SYSTEM_ACTION, {"done": "join"}, {"action": "call", "process": "x"}
    )
    with pytest.raises(DefinitionError, match="may not run inside a parallel branch"):
        serial.validate()


def test_gate_invariant_holds_per_branch():
    # External action in branch a, gate only in branch b: refused.
    bad = _parallel(gate_in_branch=True)
    bad.steps["a"] = _step("a", StepKind.SYSTEM_ACTION, {"done": "join"}, {"action": "send_mail"})
    with pytest.raises(DefinitionError, match="ADR-0007"):
        bad.validate()
    # Gate before the split covers both branches.
    ok = _parallel()
    ok.steps["start"] = _step("start", StepKind.TRIGGER, {"fired": "gate"})
    ok.steps["gate"] = _step("gate", StepKind.HUMAN_GATE, {"approved": "prepare", "rejected": None})
    ok.steps["a"] = _step("a", StepKind.SYSTEM_ACTION, {"done": "join"}, {"action": "send_mail"})
    ok.validate()


def test_for_each_declaration_is_checked():
    definition = _parallel()
    definition.steps["a"] = _step(
        "a", StepKind.AGENT_STEP, {"done": "join", "failed": "join"},
        {"task": "a", "for_each": {"over": "trigger.items", "as": "item"}},
    )
    definition.validate()
    definition.steps["a"] = _step(
        "a", StepKind.AGENT_STEP, {"done": "join"}, {"task": "a", "for_each": {"over": "x"}}
    )
    with pytest.raises(DefinitionError, match="needs 'over'"):
        definition.validate()
    definition.steps["a"] = _step(
        "a", StepKind.HUMAN_GATE, {"approved": "join"}, {"for_each": {"over": "x", "as": "i"}}
    )
    with pytest.raises(DefinitionError, match="AI tasks and actions only"):
        definition.validate()


# -- engine ----------------------------------------------------------------------


def _open(engine, case_id):
    return {
        (r["step_id"], r["branch"]): r["status"]
        for r in engine.conn.execute(
            "SELECT step_id, branch, status FROM steps WHERE case_id = ? AND finished_at IS NULL",
            (case_id,),
        )
    }


def test_split_opens_every_branch_and_join_waits_for_all(conn):
    engine = Engine(conn, {"par": _parallel(gate_in_branch=True)})
    case_id = engine.start_case("par", {"x": 1})
    engine.complete_step(case_id, "prepare", "done", result={"p": 1})
    assert _open(engine, case_id) == {
        ("a", "prepare.a"): "pending",
        ("b", "prepare.b"): "waiting",
    }
    engine.complete_step(case_id, "a", "done", result={"from": "a"})
    assert _open(engine, case_id) == {("b", "prepare.b"): "waiting", ("join", None): "waiting"}
    with pytest.raises(EngineError):
        engine.complete_step(case_id, "a", "done")  # no open row any more
    engine.decide_gate(case_id, "b", "approved", actor_id="alex")
    # Join completed itself, finish opened on the enclosing (root) branch.
    assert _open(engine, case_id) == {("finish", None): "pending"}
    events = [
        r["event"] for r in engine.conn.execute(
            "SELECT event FROM audit_log WHERE case_id = ? ORDER BY seq", (case_id,)
        )
    ]
    assert "branches_opened" in events and "branch_arrived" in events
    joined = engine.conn.execute(
        "SELECT result FROM steps WHERE case_id = ? AND step_id = 'join'", (case_id,)
    ).fetchone()["result"]
    assert '"prepare.a"' in joined and '"prepare.b"' in joined
    engine.complete_step(case_id, "finish", "done", actor_type="system", actor_id="t")
    assert engine.conn.execute(
        "SELECT status FROM cases WHERE id = ?", (case_id,)
    ).fetchone()["status"] == "completed"
    _trigger, outputs = engine.step_outputs(case_id)
    assert outputs["a"]["result"] == {"from": "a"} and outputs["b"]["outcome"] == "approved"


def test_nested_splits_join_inside_out(conn):
    definition = _parallel()
    # Branch a itself splits into a1/a2 and joins at ja before reaching join.
    definition.steps["a"] = _step(
        "a", StepKind.AGENT_STEP, {"done": "a1"}, {"task": "a"}, forks={"done": ("a1", "a2")}
    )
    definition.steps["a1"] = _step("a1", StepKind.AGENT_STEP, {"done": "ja"}, {"task": "1"})
    definition.steps["a2"] = _step("a2", StepKind.AGENT_STEP, {"done": "ja"}, {"task": "2"})
    definition.steps["ja"] = _step("ja", StepKind.SYSTEM_ACTION, {"done": "join"}, {"action": "join"})
    definition.validate()
    assert definition.joins()["ja"] == ("a", "done", ("a1", "a2"))
    engine = Engine(conn, {"par": definition})
    case_id = engine.start_case("par", {})
    engine.complete_step(case_id, "prepare", "done")
    engine.complete_step(case_id, "a", "done")
    assert set(_open(engine, case_id)) == {
        ("a1", "prepare.a/a.a1"), ("a2", "prepare.a/a.a2"), ("b", "prepare.b")
    }
    engine.complete_step(case_id, "a1", "done")
    engine.complete_step(case_id, "a2", "done")
    # Inner join done -> outer join has one arrival (branch prepare.a).
    assert _open(engine, case_id) == {("b", "prepare.b"): "pending", ("join", None): "waiting"}
    engine.complete_step(case_id, "b", "done")
    assert _open(engine, case_id) == {("finish", None): "pending"}


def test_multi_instance_runs_once_per_element_and_aggregates(conn):
    definition = _parallel()
    definition.steps["a"] = _step(
        "a", StepKind.AGENT_STEP, {"done": "join", "failed": "join"},
        {"task": "a", "for_each": {"over": "trigger.pages", "as": "page", "collect": "summaries",
                                   "max": 3}},
    )
    engine = Engine(conn, {"par": definition})
    case_id = engine.start_case("par", {"pages": ["p1", "p2"]})
    engine.complete_step(case_id, "prepare", "done")
    rows = engine.conn.execute(
        "SELECT id, instance_index, instance_total, instance_input FROM steps"
        " WHERE case_id = ? AND step_id = 'a' ORDER BY instance_index", (case_id,)
    ).fetchall()
    assert [(r["instance_index"], r["instance_total"]) for r in rows] == [(0, 2), (1, 2)]
    assert rows[1]["instance_input"] == '{"page": "p2"}'
    with pytest.raises(EngineError, match="name the row"):
        engine.complete_step(case_id, "a", "done")
    engine.complete_step(case_id, "a", "done", result={"s": "two"}, row_id=rows[1]["id"])
    assert ("join", None) not in _open(engine, case_id)  # one instance still open
    engine.complete_step(case_id, "a", "failed", result={"error": "x"}, row_id=rows[0]["id"])
    _trigger, outputs = engine.step_outputs(case_id)
    assert outputs["a"]["outcome"] == "failed"
    assert outputs["a"]["result"] == [{"error": "x"}, {"s": "two"}]  # input order
    assert engine._handoff_payload(case_id, definition, case_id)["outputs"]["summaries"] == [
        {"error": "x"}, {"s": "two"},
    ]
    # Empty list: the step passes at once; too many: fails loudly.
    empty = engine.start_case("par", {"pages": []})
    engine.complete_step(empty, "prepare", "done")
    assert ("join", None) in _open(engine, empty)
    big = engine.start_case("par", {"pages": [1, 2, 3, 4]})
    engine.complete_step(big, "prepare", "done")
    row = engine.conn.execute(
        "SELECT outcome, result FROM steps WHERE case_id = ? AND step_id = 'a'", (big,)
    ).fetchone()
    assert row["outcome"] == "failed" and "exceed max 3" in row["result"]


# -- BPMN profile ----------------------------------------------------------------

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:aimn="https://aitmation.com/schema/aimn/1" id="d" targetNamespace="x">
<bpmn:process id="par" isExecutable="true">
"""
TAIL = "</bpmn:process></bpmn:definitions>\n"
PARALLEL_XML = HEAD + """
  <bpmn:startEvent id="intake"><bpmn:extensionElements><aimn:trigger source="manual"/></bpmn:extensionElements></bpmn:startEvent>
  <bpmn:task id="prepare"><bpmn:extensionElements><aimn:aiTask><aimn:instructions lang="en">Prepare.</aimn:instructions></aimn:aiTask></bpmn:extensionElements></bpmn:task>
  <bpmn:parallelGateway id="split"/>
  <bpmn:task id="extract"><bpmn:extensionElements><aimn:aiTask><aimn:instructions lang="en">Extract.</aimn:instructions></aimn:aiTask></bpmn:extensionElements></bpmn:task>
  <bpmn:task id="translate">
    <bpmn:multiInstanceLoopCharacteristics aimn:over="trigger.languages" aimn:as="language" aimn:collect="translations" aimn:max="4"/>
    <bpmn:extensionElements><aimn:aiTask><aimn:instructions lang="en">Translate into {language}.</aimn:instructions></aimn:aiTask></bpmn:extensionElements>
  </bpmn:task>
  <bpmn:parallelGateway id="joined"/>
  <bpmn:userTask id="review"><bpmn:extensionElements><aimn:gate approving="approved"/></bpmn:extensionElements></bpmn:userTask>
  <bpmn:endEvent id="finished"/>
  <bpmn:sequenceFlow id="f1" sourceRef="intake" targetRef="prepare"/>
  <bpmn:sequenceFlow id="f2" name="done" sourceRef="prepare" targetRef="split"/>
  <bpmn:sequenceFlow id="f2b" name="failed" sourceRef="prepare" targetRef="finished"/>
  <bpmn:sequenceFlow id="f3" sourceRef="split" targetRef="extract"/>
  <bpmn:sequenceFlow id="f4" sourceRef="split" targetRef="translate"/>
  <bpmn:sequenceFlow id="f5" name="done" sourceRef="extract" targetRef="joined"/>
  <bpmn:sequenceFlow id="f5b" name="failed" sourceRef="extract" targetRef="joined"/>
  <bpmn:sequenceFlow id="f6" name="done" sourceRef="translate" targetRef="joined"/>
  <bpmn:sequenceFlow id="f6b" name="failed" sourceRef="translate" targetRef="joined"/>
  <bpmn:sequenceFlow id="f7" sourceRef="joined" targetRef="review"/>
  <bpmn:sequenceFlow id="f8" name="approved" sourceRef="review" targetRef="finished"/>
  <bpmn:sequenceFlow id="f9" name="rejected" sourceRef="review" targetRef="finished"/>
""" + TAIL


def test_bpmn_parallel_gateways_and_multi_instance_round_trip():
    definition = parse_bpmn(PARALLEL_XML, "par.bpmn")
    assert definition.steps["prepare"].forks == {"done": ("extract", "translate")}
    assert definition.steps["joined"].is_join and definition.steps["joined"].next == {"done": "review"}
    assert definition.steps["translate"].for_each == {
        "over": "trigger.languages", "as": "language", "collect": "translations", "max": 4,
    }
    xml, _ = to_bpmn(definition, inline_briefings=True)
    again = parse_bpmn(xml, "again.bpmn")
    assert again.steps["prepare"].forks == definition.steps["prepare"].forks
    assert again.steps["translate"].for_each == definition.steps["translate"].for_each
    assert again.steps["joined"].is_join
    assert "<bpmn:parallelGateway" in xml and "multiInstanceLoopCharacteristics" in xml
    assert 'bpmnElement="prepare_done_split"' in xml  # drawn


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (('<bpmn:sequenceFlow id="f3" sourceRef="split"', '<bpmn:sequenceFlow id="f3" name="x" sourceRef="split"'), "must be unnamed"),
        (('<bpmn:sequenceFlow id="f6" name="done" sourceRef="translate" targetRef="joined"/>', '<bpmn:sequenceFlow id="f6" name="done" sourceRef="translate" targetRef="finished"/>'), "leaves the process"),
        (('aimn:as="language" ', ""), "needs aimn:over and aimn:as"),
        (('<bpmn:task id="translate">', '<bpmn:task id="translate" isSequential="x">'), None),
    ],
)
def test_bpmn_parallel_profile_rejects(mutation, message):
    old, new = mutation
    text = PARALLEL_XML.replace(old, new)
    if message is None:
        parse_bpmn(text, "ok.bpmn")  # unrelated attribute: still fine
        return
    with pytest.raises(BpmnError, match=message):
        parse_bpmn(text, "bad.bpmn")


# -- proving run ------------------------------------------------------------------


def test_parallel_process_runs_in_the_proving_run(tmp_path):
    root = tmp_path / "apps" / "demo-par"
    (root / "processes").mkdir(parents=True)
    (root / "solution.toml").write_text(
        'format = 2\nid = "demo-par"\nversion = "0.1.0"\nname = "Par"\n', encoding="utf-8"
    )
    (root / "processes" / "par.bpmn").write_text(PARALLEL_XML, encoding="utf-8")
    (root / "processes" / "par.test.toml").write_text(
        '[[case]]\nid = "two-languages"\n'
        'given = { trigger = { source = "manual", languages = ["fr", "it"] } }\n'
        'config = "standard"\n'
        'decisions = { review = "approved" }\n'
        '[case.expect]\nstatus = "completed"\n'
        'outcomes = { prepare = "done", extract = "done", translate = "done", joined = "done", review = "approved" }\n'
        'audit = ["branches_opened", "instances_completed"]\n',
        encoding="utf-8",
    )
    app = load_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    assert app.templates["par"].joins()
    report = run_solution_tests(root, configuration="standard", core_templates_dir=CORE_TEMPLATES)
    assert report.ok, format_report(report)


def test_runtime_feeds_instances_their_element(tmp_path):
    root = tmp_path / "apps" / "demo-par"
    (root / "processes").mkdir(parents=True)
    (root / "solution.toml").write_text(
        'format = 2\nid = "demo-par"\nversion = "0.1.0"\nname = "Par"\n', encoding="utf-8"
    )
    (root / "processes" / "par.bpmn").write_text(PARALLEL_XML, encoding="utf-8")
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=root.parent)
    runtime = Runtime(settings)  # no model: stubs, which keep the structure visible
    try:
        case_id = runtime.engine.start_case("par", {"source": "manual", "languages": ["fr", "it", "de"]})
        for _ in range(4):
            runtime.tick()
        payload = runtime._agent_payload(
            case_id, runtime.engine.definitions["par"].steps["translate"].config, None,
            runtime.conn.execute(
                "SELECT * FROM steps WHERE case_id = ? AND step_id = 'translate' AND instance_index = 1",
                (case_id,),
            ).fetchone(),
        )
        assert payload["language"] == "it" and payload["instance"] == {"index": 1, "total": 3}
        gate = next(g for g in runtime.open_gates() if g["template_id"] == "par")
        assert gate["step_id"] == "review"  # all three instances and the extraction joined
        _trigger, outputs = runtime.engine.step_outputs(case_id)
        assert len(outputs["translate"]["result"]) == 3 and outputs["joined"]["outcome"] == "done"
    finally:
        runtime.close()
