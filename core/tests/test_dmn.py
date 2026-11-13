"""DMN decision tables with S-FEEL (ADR-0034 §2) and the ``decide`` action."""

import json
from pathlib import Path

import pytest

from aitmation_core.apps import load_app, validate_app
from aitmation_core.config import Settings
from aitmation_core.dmn import DmnError, evaluate, matched_rules, parse_dmn
from aitmation_core.runtime import Runtime
from aitmation_core.template import load_templates
from aitmation_core.testing import format_report, run_solution_tests

CORE_TEMPLATES = Path(__file__).resolve().parents[2] / "core" / "templates"

TRIAGE = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/" id="defs" name="triage" namespace="https://aitmation.com/solutions">
  <decision id="triage" name="Triage">
    <decisionTable id="t" hitPolicy="UNIQUE">
      <input id="i1" label="Betrag"><inputExpression typeRef="number"><text>steps.read.total</text></inputExpression></input>
      <input id="i2" label="Auffälligkeiten"><inputExpression typeRef="number"><text>steps.read.anomaly_count</text></inputExpression></input>
      <input id="i3" label="Typ"><inputExpression typeRef="string"><text>trigger.doctype</text></inputExpression></input>
      <output id="o1" name="outcome" typeRef="string"/>
      <output id="o2" name="reason" typeRef="string"/>
      <rule id="r1"><inputEntry><text>&lt; 100</text></inputEntry><inputEntry><text>0</text></inputEntry><inputEntry><text>-</text></inputEntry>
        <outputEntry><text>"archive"</text></outputEntry><outputEntry><text>"small and clean"</text></outputEntry></rule>
      <rule id="r2"><inputEntry><text>[100..1000]</text></inputEntry><inputEntry><text>0</text></inputEntry><inputEntry><text>"invoice","receipt"</text></inputEntry>
        <outputEntry><text>"review"</text></outputEntry><outputEntry><text>"mid-range"</text></outputEntry></rule>
      <rule id="r3"><inputEntry><text>&gt; 1000</text></inputEntry><inputEntry><text>-</text></inputEntry><inputEntry><text>-</text></inputEntry>
        <outputEntry><text>"review"</text></outputEntry><outputEntry><text>"large"</text></outputEntry></rule>
      <rule id="r4"><inputEntry><text>&lt;= 1000</text></inputEntry><inputEntry><text>&gt; 0</text></inputEntry><inputEntry><text>-</text></inputEntry>
        <outputEntry><text>"review"</text></outputEntry><outputEntry><text>"anomalies"</text></outputEntry></rule>
      <rule id="r5"><inputEntry><text>[100..1000]</text></inputEntry><inputEntry><text>0</text></inputEntry><inputEntry><text>not("invoice","receipt")</text></inputEntry>
        <outputEntry><text>"archive"</text></outputEntry><outputEntry><text>trigger.doctype</text></outputEntry></rule>
    </decisionTable>
  </decision>
</definitions>
"""


def test_table_parses_and_evaluates_with_unique_hit_policy():
    decision = parse_dmn(TRIAGE, "triage.dmn")
    assert decision.hit_policy == "UNIQUE" and len(decision.rules) == 5
    ctx = {"steps": {"read": {"total": 42, "anomaly_count": 0}}, "trigger": {"doctype": "letter"}}
    assert evaluate(decision, ctx) == {"outcome": "archive", "reason": "small and clean"}
    assert matched_rules(decision, ctx) == ["r1"]
    ctx = {
        "steps": {"read": {"total": "518.90", "anomaly_count": 0}},
        "trigger": {"doctype": "invoice"},
    }
    assert evaluate(decision, ctx)["outcome"] == "review"  # numeric string coerced
    ctx = {"steps": {"read": {"total": 5000, "anomaly_count": 0}}, "trigger": {"doctype": "x"}}
    assert evaluate(decision, ctx)["reason"] == "large"
    ctx = {"steps": {"read": {"total": 300, "anomaly_count": 2}}, "trigger": {"doctype": "x"}}
    assert evaluate(decision, ctx)["reason"] == "anomalies"
    ctx = {"steps": {"read": {"total": 300, "anomaly_count": 0}}, "trigger": {"doctype": "letter"}}
    assert evaluate(decision, ctx) == {"outcome": "archive", "reason": "letter"}  # variable output
    assert evaluate(decision, {"steps": {}, "trigger": {}}) == {"outcome": None, "reason": None}


def test_unique_with_overlapping_rules_is_an_evaluation_error():
    overlapping = TRIAGE.replace("&lt;= 1000", "-")  # r4 now overlaps r3 for > 1000 with anomalies
    decision = parse_dmn(overlapping, "x.dmn")
    ctx = {"steps": {"read": {"total": 5000, "anomaly_count": 3}}, "trigger": {"doctype": "x"}}
    with pytest.raises(DmnError, match="UNIQUE"):
        evaluate(decision, ctx)
    first = parse_dmn(overlapping.replace('hitPolicy="UNIQUE"', 'hitPolicy="FIRST"'), "y.dmn")
    assert evaluate(first, ctx)["reason"] == "large"
    collect = parse_dmn(overlapping.replace('hitPolicy="UNIQUE"', 'hitPolicy="COLLECT"'), "z.dmn")
    assert evaluate(collect, ctx)["reason"] == ["large", "anomalies"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ('hitPolicy="UNIQUE"', 'hitPolicy="PRIORITY"'),
        ("<text>steps.read.total</text>", "<text>steps.read.total * 2</text>"),
        (
            '<outputEntry><text>"archive"</text></outputEntry><outputEntry><text>"small and clean"</text></outputEntry>',
            '<outputEntry><text>"archive"</text></outputEntry>',
        ),
    ],
)
def test_profile_rejects(mutation, message):
    with pytest.raises(DmnError):
        parse_dmn(TRIAGE.replace(mutation, message), "bad.dmn")


def test_unary_tests_cover_s_feel_subset():
    base = TRIAGE
    decision = parse_dmn(base, "t.dmn")
    # date comparison and boolean literal via a tiny table
    table = """<?xml version="1.0"?>
<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/" id="d">
  <decision id="dates"><decisionTable hitPolicy="FIRST">
    <input id="a"><inputExpression><text>due</text></inputExpression></input>
    <input id="b"><inputExpression><text>paid</text></inputExpression></input>
    <output id="o" name="state"/>
    <rule id="r1"><inputEntry><text>-</text></inputEntry><inputEntry><text>true</text></inputEntry><outputEntry><text>"paid"</text></outputEntry></rule>
    <rule id="r2"><inputEntry><text>&lt; date("2026-10-01")</text></inputEntry><inputEntry><text>false</text></inputEntry><outputEntry><text>"overdue"</text></outputEntry></rule>
    <rule id="r3"><inputEntry><text>-</text></inputEntry><inputEntry><text>-</text></inputEntry><outputEntry><text>"open"</text></outputEntry></rule>
  </decisionTable></decision>
</definitions>"""
    dates = parse_dmn(table, "dates.dmn")
    assert evaluate(dates, {"due": "2026-09-15", "paid": False})["state"] == "overdue"
    assert evaluate(dates, {"due": "2026-11-15", "paid": False})["state"] == "open"
    assert evaluate(dates, {"due": "2026-09-15", "paid": True})["state"] == "paid"
    assert decision.inputs[0].label == "Betrag"


def _solution_with_decision(tmp_path: Path) -> Path:
    root = tmp_path / "apps" / "demo-dmn"
    for folder in ("processes", "decisions", "ai"):
        (root / folder).mkdir(parents=True)
    (root / "solution.toml").write_text(
        'format = 2\nid = "demo-dmn"\nversion = "0.1.0"\nname = "Demo DMN"\n', encoding="utf-8"
    )
    (root / "decisions" / "triage.dmn").write_text(TRIAGE, encoding="utf-8")
    (root / "decisions" / "triage.test.toml").write_text(
        '[[case]]\nid = "small-clean-archives"\n'
        '[case.given.inputs]\nsteps = { read = { total = 42, anomaly_count = 0 } }\ntrigger = { doctype = "letter" }\n'
        '[case.expect.outputs]\noutcome = "archive"\n\n'
        '[[case]]\nid = "large-reviews"\n'
        '[case.given.inputs]\nsteps = { read = { total = 9000, anomaly_count = 0 } }\ntrigger = { doctype = "x" }\n'
        '[case.expect.outputs]\noutcome = "review"\nreason = "large"\n',
        encoding="utf-8",
    )
    (root / "processes" / "triage-flow.bpmn").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:aimn="https://aitmation.com/schema/aimn/1" id="d" targetNamespace="x">
<bpmn:process id="triage-flow" isExecutable="true">
  <bpmn:startEvent id="intake"><bpmn:extensionElements><aimn:trigger source="manual"/></bpmn:extensionElements></bpmn:startEvent>
  <bpmn:businessRuleTask id="triage"><bpmn:extensionElements><aimn:decision ref="triage"/></bpmn:extensionElements></bpmn:businessRuleTask>
  <bpmn:userTask id="review"><bpmn:extensionElements><aimn:gate approving="approved"/></bpmn:extensionElements></bpmn:userTask>
  <bpmn:serviceTask id="archive"><bpmn:extensionElements><aimn:toolCall action="archive"/></bpmn:extensionElements></bpmn:serviceTask>
  <bpmn:endEvent id="finished"/>
  <bpmn:sequenceFlow id="f1" sourceRef="intake" targetRef="triage"/>
  <bpmn:sequenceFlow id="f2" name="review" sourceRef="triage" targetRef="review"/>
  <bpmn:sequenceFlow id="f3" name="archive" sourceRef="triage" targetRef="archive"/>
  <bpmn:sequenceFlow id="f4" name="failed" sourceRef="triage" targetRef="finished"/>
  <bpmn:sequenceFlow id="f5" name="approved" sourceRef="review" targetRef="archive"/>
  <bpmn:sequenceFlow id="f6" name="rejected" sourceRef="review" targetRef="finished"/>
  <bpmn:sequenceFlow id="f7" name="done" sourceRef="archive" targetRef="finished"/>
</bpmn:process>
</bpmn:definitions>
""",
        encoding="utf-8",
    )
    (root / "processes" / "triage-flow.test.toml").write_text(
        '[[case]]\nid = "large-amount-needs-review"\n'
        'given = { trigger = { source = "manual", doctype = "invoice", outputs = { read = { total = 5000, anomaly_count = 0 } } } }\n'
        '[case.expect]\nstatus = "running"\nwaiting_at = "review"\noutcomes = { triage = "review" }\n\n'
        '[[case]]\nid = "small-letter-archives-directly"\n'
        'given = { trigger = { source = "manual", doctype = "letter", outputs = { read = { total = 10, anomaly_count = 0 } } } }\n'
        '[case.expect]\nstatus = "completed"\noutcomes = { triage = "archive", archive = "done" }\npath = ["intake", "triage", "archive"]\n',
        encoding="utf-8",
    )
    return root


def test_business_rule_task_routes_by_decision(tmp_path):
    root = _solution_with_decision(tmp_path)
    report = validate_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    assert report.ok, [str(f) for f in report.errors]
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=root.parent
    )
    runtime = Runtime(settings)
    try:
        payload = {
            "source": "manual",
            "doctype": "invoice",
            "outputs": {"read": {"total": 5000, "anomaly_count": 0}},
        }
        # Chained-style payload: the runtime reads predecessor outputs when source is chain;
        # here the trigger carries steps directly via "outputs" -> use chain semantics.
        payload["source"] = "chain"
        case_id = runtime.engine.start_case("triage-flow", payload)
        runtime.tick()
        row = runtime.conn.execute(
            "SELECT outcome, result FROM steps WHERE case_id = ? AND step_id = 'triage'", (case_id,)
        ).fetchone()
        assert row["outcome"] == "review"
        result = json.loads(row["result"])
        assert result["matched_rules"] == ["r3"] and result["reason"] == "large"
        audit = runtime.conn.execute(
            "SELECT details FROM audit_log WHERE case_id = ? AND event = 'step_completed'",
            (case_id,),
        ).fetchone()
        assert json.loads(audit["details"])["decision"] == "triage"
    finally:
        runtime.close()


def test_decision_sidecars_and_process_sidecars_run_in_the_proving_run(tmp_path):
    root = _solution_with_decision(tmp_path)
    # Trigger payloads in sidecars use chain semantics for predecessor outputs.
    text = (
        (root / "processes" / "triage-flow.test.toml")
        .read_text()
        .replace('source = "manual"', 'source = "chain"')
    )
    (root / "processes" / "triage-flow.test.toml").write_text(text, encoding="utf-8")
    report = run_solution_tests(root, core_templates_dir=CORE_TEMPLATES)
    assert report.ok, format_report(report)
    kinds = {(r.subject_kind, r.case_id) for r in report.results}
    assert ("decision", "small-clean-archives") in kinds
    assert ("process", "small-letter-archives-directly") in kinds
    assert report.coverage["triage-flow"].outcomes_visited == [
        "archive:done",
        "intake:fired",
        "triage:archive",
        "triage:review",
    ]


def test_solution_loader_exposes_decisions(tmp_path):
    root = _solution_with_decision(tmp_path)
    app = load_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    assert "triage" in app.decisions
    bad = (
        (root / "processes" / "triage-flow.bpmn")
        .read_text()
        .replace('ref="triage"', 'ref="missing"')
    )
    (root / "processes" / "triage-flow.bpmn").write_text(bad, encoding="utf-8")
    report = validate_app(root, core_definitions=load_templates(CORE_TEMPLATES))
    assert any("not in sight" in f.message for f in report.errors)


def test_feel_types_are_not_python_types():
    """Booleans and numbers stay distinct, typeRef coerces form strings,
    and a list of negations is a disjunction."""
    table = """<?xml version="1.0"?>
<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/" id="d">
  <decision id="types"><decisionTable hitPolicy="FIRST">
    <input id="a"><inputExpression typeRef="number"><text>amount</text></inputExpression></input>
    <input id="b"><inputExpression typeRef="boolean"><text>flag</text></inputExpression></input>
    <input id="c"><inputExpression typeRef="string"><text>kind</text></inputExpression></input>
    <output id="o" name="state"/>
    <rule id="r1"><inputEntry><text>1</text></inputEntry><inputEntry><text>true</text></inputEntry><inputEntry><text>-</text></inputEntry><outputEntry><text>"one-and-true"</text></outputEntry></rule>
    <rule id="r2"><inputEntry><text>&gt; 5</text></inputEntry><inputEntry><text>-</text></inputEntry><inputEntry><text>not("a"), not("b")</text></inputEntry><outputEntry><text>"big"</text></outputEntry></rule>
    <rule id="r3"><inputEntry><text>-</text></inputEntry><inputEntry><text>-</text></inputEntry><inputEntry><text>-</text></inputEntry><outputEntry><text>"rest"</text></outputEntry></rule>
  </decisionTable></decision>
</definitions>"""
    d = parse_dmn(table, "types.dmn")
    assert evaluate(d, {"amount": 1, "flag": True, "kind": "x"})["state"] == "one-and-true"
    assert evaluate(d, {"amount": True, "flag": 1, "kind": "x"})["state"] == "rest"
    assert evaluate(d, {"amount": "12", "flag": "false", "kind": "a"})["state"] == "big"
    assert evaluate(d, {"amount": "12 CHF", "flag": False, "kind": "a"})["state"] == "rest"
    assert matched_rules(d, {"amount": 7, "flag": False, "kind": "b"}) == ["r2", "r3"]
