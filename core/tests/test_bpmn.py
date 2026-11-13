"""BPMN profile reader/writer and AIMN briefings (ADR-0034).

The engine does not change; what these tests prove is that a BPMN file
turns into exactly the definition a TOML template would, that the profile
rejects what it must, and that a migrated process survives a round trip.
"""

from pathlib import Path

import pytest

from aitmation_core import aimn
from aitmation_core.bpmn import BpmnError, load_bpmn, parse_bpmn, to_bpmn
from aitmation_core.definition import END, StepKind
from aitmation_core.engine import Engine
from aitmation_core.template import load_template, load_templates

REPO = Path(__file__).resolve().parents[2]
APP_TEMPLATES = Path(__file__).resolve().parent / "fixtures" / "document-basics-v1" / "templates"
CORE_TEMPLATES = REPO / "core" / "templates"

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:aimn="https://aitmation.com/schema/aimn/1" id="d" targetNamespace="x">
"""
TAIL = "</bpmn:definitions>\n"


def _wrap(process_xml: str) -> str:
    return HEAD + process_xml + TAIL


GATEWAY_PROCESS = _wrap(
    """
<bpmn:process id="demo" isExecutable="true" aimn:version="2">
  <bpmn:extensionElements>
    <aimn:meta><aimn:title lang="de">Demo</aimn:title><aimn:title lang="en">Demo</aimn:title></aimn:meta>
    <aimn:policy name="privacy"><aimn:property name="category" value="internal"/></aimn:policy>
    <aimn:policy name="model"><aimn:property name="confidence_threshold" value="0.5"/></aimn:policy>
  </bpmn:extensionElements>
  <bpmn:startEvent id="intake"><bpmn:extensionElements><aimn:trigger source="folder"/></bpmn:extensionElements></bpmn:startEvent>
  <bpmn:task id="prepare">
    <bpmn:extensionElements>
      <aimn:aiTask modelRole="drafter">
        <aimn:role lang="en">You summarize documents.</aimn:role>
        <aimn:instructions lang="en">Summarize the document.</aimn:instructions>
        <aimn:limit lang="en">Never invent amounts.</aimn:limit>
        <aimn:output name="summary">two sentences</aimn:output>
        <aimn:property name="read_document" value="true"/>
      </aimn:aiTask>
    </bpmn:extensionElements>
  </bpmn:task>
  <bpmn:exclusiveGateway id="split"/>
  <bpmn:userTask id="approve">
    <bpmn:extensionElements><aimn:gate approving="approved"/></bpmn:extensionElements>
  </bpmn:userTask>
  <bpmn:boundaryEvent id="approve_deadline" attachedToRef="approve" cancelActivity="false">
    <bpmn:timerEventDefinition><bpmn:timeDuration>P2D</bpmn:timeDuration></bpmn:timerEventDefinition>
  </bpmn:boundaryEvent>
  <bpmn:serviceTask id="export">
    <bpmn:extensionElements><aimn:toolCall action="send_mail" effect="external"/></bpmn:extensionElements>
  </bpmn:serviceTask>
  <bpmn:intermediateCatchEvent id="cool_off">
    <bpmn:timerEventDefinition><bpmn:timeDuration>PT1H30M</bpmn:timeDuration></bpmn:timerEventDefinition>
  </bpmn:intermediateCatchEvent>
  <bpmn:endEvent id="finished"/>
  <bpmn:endEvent id="handoff"><bpmn:extensionElements><aimn:chainTo process="next-one"/></bpmn:extensionElements></bpmn:endEvent>
  <bpmn:sequenceFlow id="f1" sourceRef="intake" targetRef="prepare"/>
  <bpmn:sequenceFlow id="f2" sourceRef="prepare" targetRef="split"/>
  <bpmn:sequenceFlow id="f3" name="done" sourceRef="split" targetRef="approve"/>
  <bpmn:sequenceFlow id="f4" name="failed" sourceRef="split" targetRef="finished"/>
  <bpmn:sequenceFlow id="f5" name="approved" sourceRef="approve" targetRef="export"/>
  <bpmn:sequenceFlow id="f6" name="rejected" sourceRef="approve" targetRef="cool_off"/>
  <bpmn:sequenceFlow id="f7" sourceRef="cool_off" targetRef="prepare"/>
  <bpmn:sequenceFlow id="f8" name="done" sourceRef="export" targetRef="handoff"/>
</bpmn:process>
"""
)


def test_gateway_process_maps_onto_the_five_primitives():
    definition = parse_bpmn(GATEWAY_PROCESS, "demo.bpmn")
    assert definition.id == "demo" and definition.version == 2
    assert definition.meta["title"] == {"de": "Demo", "en": "Demo"}
    assert definition.policies["model"]["confidence_threshold"] == 0.5
    kinds = {sid: s.kind for sid, s in definition.steps.items()}
    assert kinds == {
        "intake": StepKind.TRIGGER,
        "prepare": StepKind.AGENT_STEP,
        "approve": StepKind.HUMAN_GATE,
        "export": StepKind.SYSTEM_ACTION,
        "cool_off": StepKind.TIMER,
    }
    assert definition.steps["intake"].next == {"fired": "prepare"}
    assert definition.steps["intake"].config == {"source": "folder"}
    # The gateway named the outcomes; the engine never sees it.
    assert definition.steps["prepare"].next == {"done": "approve", "failed": END}
    assert definition.steps["approve"].next == {"approved": "export", "rejected": "cool_off"}
    assert definition.steps["approve"].config["deadline_seconds"] == 172800
    assert definition.steps["cool_off"].next == {"elapsed": "prepare"}
    assert definition.steps["cool_off"].config["seconds"] == 5400
    assert definition.steps["export"].next == {"done": "process:next-one"}
    prepare = definition.steps["prepare"].config
    assert prepare["read_document"] is True
    assert prepare["output"] == {"summary": "two sentences"}
    assert prepare["briefing"]["model_role"] == "drafter"
    assert prepare["task"].startswith("Role: You summarize documents.")
    assert "Never:\n- Never invent amounts." in prepare["task"]
    assert len(prepare["prompt_hash"]) == 64


def test_prompt_composition_is_deterministic_and_hashed():
    a = parse_bpmn(GATEWAY_PROCESS, "a").steps["prepare"].config
    b = parse_bpmn(GATEWAY_PROCESS, "b").steps["prepare"].config
    assert a["task"] == b["task"] and a["prompt_hash"] == b["prompt_hash"]
    changed = GATEWAY_PROCESS.replace("Summarize the document.", "Summarise the document.")
    assert parse_bpmn(changed, "c").steps["prepare"].config["prompt_hash"] != a["prompt_hash"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ('<bpmn:scriptTask id="x"/>', "not in the aitmation profile"),
        (
            '<bpmn:task id="plain"/><bpmn:sequenceFlow id="fx" name="done" sourceRef="plain"'
            ' targetRef="finished"/>',
            "has no aimn:aiTask",
        ),
    ],
)
def test_profile_rejects_what_it_must(mutation, message):
    bad = GATEWAY_PROCESS.replace(
        '<bpmn:endEvent id="finished"/>', mutation + '<bpmn:endEvent id="finished"/>'
    )
    with pytest.raises(BpmnError, match=message):
        parse_bpmn(bad, "bad.bpmn")


def test_collaborations_are_outside_the_profile():
    bad = GATEWAY_PROCESS.replace("<bpmn:process", '<bpmn:collaboration id="c"/><bpmn:process', 1)
    with pytest.raises(BpmnError, match="pools and collaborations"):
        parse_bpmn(bad, "bad.bpmn")


def test_unnamed_flow_out_of_a_step_is_an_error():
    bad = GATEWAY_PROCESS.replace('name="approved" sourceRef="approve"', 'sourceRef="approve"')
    with pytest.raises(BpmnError, match="needs a name"):
        parse_bpmn(bad, "bad.bpmn")


def test_boundary_timer_with_outgoing_flow_is_rejected():
    bad = GATEWAY_PROCESS.replace(
        "</bpmn:process>",
        '<bpmn:sequenceFlow id="f9" name="elapsed" sourceRef="approve_deadline"'
        ' targetRef="finished"/></bpmn:process>',
    )
    with pytest.raises(BpmnError, match="escalation deadline"):
        parse_bpmn(bad, "bad.bpmn")


def test_gate_invariant_is_checked_on_the_graph():
    bad = GATEWAY_PROCESS.replace(
        'name="done" sourceRef="split" targetRef="approve"',
        'name="done" sourceRef="split" targetRef="export"',
    )
    with pytest.raises(BpmnError, match="ADR-0007"):
        parse_bpmn(bad, "bad.bpmn")


def test_doctype_declarations_are_refused():
    with pytest.raises(BpmnError, match="DOCTYPE"):
        parse_bpmn("<!DOCTYPE x [<!ENTITY a 'b'>]>" + GATEWAY_PROCESS, "evil.bpmn")


def test_briefing_reference_must_resolve():
    xml = GATEWAY_PROCESS.replace(
        '<aimn:aiTask modelRole="drafter">', '<aimn:aiTask ref="summarize">'
    )
    with pytest.raises(BpmnError, match="briefing 'summarize'"):
        parse_bpmn(xml, "x.bpmn")
    briefing = aimn.parse_briefing(
        'id = "summarize"\ninstructions = { en = "Summarize." }\n[output]\nsummary = "s"\n'
    )
    definition = parse_bpmn(xml, "x.bpmn", briefings={"summarize": briefing})
    assert definition.steps["prepare"].config["task"].startswith("Summarize.")


def test_skills_fold_into_the_briefing():
    skill = aimn.parse_briefing(
        'id = "amounts"\ninstructions = { en = "Extract all amounts in CHF." }\n'
        'limits = ["Never round."]\n[output]\namounts = "list of amounts"\n'
    )
    task = aimn.parse_briefing(
        'id = "read"\nskills = ["amounts"]\ninstructions = { en = "Then summarize." }\n'
        '[output]\nsummary = "s"\n'
    )
    config = task.to_config({"amounts": skill})
    assert config["task"].startswith("Extract all amounts in CHF.\nThen summarize.")
    assert config["output"] == {"amounts": "list of amounts", "summary": "s"}
    assert "Never round." in config["task"]
    with pytest.raises(aimn.BriefingError, match="unknown skill"):
        task.to_config({})


@pytest.mark.parametrize(
    "name", ["document-read", "document-review", "document-archive", "follow-up"]
)
def test_shipped_templates_round_trip_through_bpmn(tmp_path, name):
    original = load_template(APP_TEMPLATES / f"{name}.toml")
    xml, briefings = to_bpmn(original)
    (tmp_path / "ai").mkdir()
    for bid, briefing in briefings.items():
        (tmp_path / "ai" / f"{bid}.aimn.toml").write_text(aimn.briefing_to_toml(briefing))
    (tmp_path / f"{name}.bpmn").write_text(xml, encoding="utf-8")
    loaded = load_templates(tmp_path)[name]
    assert loaded.id == original.id and loaded.version == original.version
    assert loaded.meta == original.meta
    assert loaded.contract == original.contract
    assert loaded.policies == original.policies
    assert {k: v.next for k, v in loaded.steps.items()} == {
        k: v.next for k, v in original.steps.items()
    }
    for sid, step in original.steps.items():
        got = loaded.steps[sid].config
        if step.kind is StepKind.AGENT_STEP:
            # The task text survives as the briefing's English instructions.
            assert got["task"].startswith(step.config["task"])
            assert got.get("output") == step.config.get("output")
            assert got.get("context") == step.config.get("context")
            assert got.get("read_document") == step.config.get("read_document")
        else:
            assert got == step.config
    assert "bpmndi:BPMNDiagram" in xml and "dc:Bounds" in xml


def test_core_intake_round_trip_keeps_classify_mode(tmp_path):
    """The format-1 intake (fixture) and the shipped BPMN intake are the
    same process: mechanics steps carry no briefing and no prompt hash."""
    fixture = Path(__file__).parent / "fixtures" / "core-templates-v1" / "document-intake.toml"
    original = load_template(fixture)
    xml, briefings = to_bpmn(original, inline_briefings=True)
    assert not briefings
    (tmp_path / "document-intake.bpmn").write_text(xml, encoding="utf-8")
    loaded = load_bpmn(tmp_path / "document-intake.bpmn")
    classify = loaded.steps["classify"].config
    assert classify["classify"] is True and classify["read_document"] is True
    assert "briefing" not in classify and "prompt_hash" not in classify
    assert loaded.steps["categorize"].config == {"categorization": True}
    assert loaded.steps["dispatch"].config == {"action": "dispatch"}
    shipped = load_bpmn(CORE_TEMPLATES / "document-intake.bpmn")
    for sid, step in original.steps.items():
        assert shipped.steps[sid].next == step.next
        assert shipped.steps[sid].config == step.config


def test_bpmn_definition_runs_in_the_engine(conn):
    definition = parse_bpmn(
        GATEWAY_PROCESS.replace('<aimn:chainTo process="next-one"/>', ""), "demo"
    )
    engine = Engine(conn, {"demo": definition})
    case_id = engine.start_case("demo", {"file": "x.pdf"})
    pending = engine.pending_steps(StepKind.AGENT_STEP)
    assert [row["step_id"] for row in pending] == ["prepare"]
    engine.complete_step(case_id, "prepare", "done", {"summary": "ok"})
    engine.decide_gate(case_id, "approve", "approved", actor_id="alex")
    engine.complete_step(case_id, "export", "done", {}, actor_type="system", actor_id="t")
    row = conn.execute("SELECT status FROM cases WHERE id = ?", (case_id,)).fetchone()
    assert row["status"] == "completed"
