"""Scaffolds the workshop writes when something new is created (ADR-0035):
a solution, a process, a briefing, a decision table, a sidecar case.

Every text here is a complete, valid element of the smallest useful
shape - a process already runs (start -> AI step -> human gate -> end),
a briefing already has a role and an output, a decision already has a
rule. The person then edits on the canvas and in the panel instead of
starting from an empty file. Nothing here is specific to a business
domain (the core stays neutral).
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape as _xml_escape

from .i18n import LANGUAGES


def escape(text: str) -> str:
    """Text that is safe in element content *and* in a double-quoted
    attribute."""
    return _xml_escape(text, {'"': "&quot;"})

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ScaffoldError(ValueError):
    pass


def check_id(value: str, what: str) -> str:
    if not ID_PATTERN.match(value or ""):
        raise ScaffoldError(
            f"{what} id {value!r} must be lowercase letters, digits, '-' or '_'"
        )
    return value


def _names(title: str | dict[str, str], fallback: str) -> dict[str, str]:
    """Titles in the four product languages (ADR-0022): a plain string
    counts as German and is copied to the others, to be refined later."""
    if isinstance(title, dict):
        base = {k: v for k, v in title.items() if k in LANGUAGES and v}
    else:
        base = {"de": title} if title else {}
    first = base.get("de") or next(iter(base.values()), fallback)
    return {lang: base.get(lang, first) for lang in LANGUAGES}


def _toml_table(values: dict[str, str]) -> str:
    return "{ " + ", ".join(f'{k} = "{_q(v)}"' for k, v in values.items()) + " }"


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def solution_toml(solution_id: str, name: str | dict, description: str | dict = "") -> str:
    check_id(solution_id, "solution")
    names = _names(name, solution_id)
    descriptions = _names(description, "") if description else {}
    lines = [
        "# Created in the workshop. Format 2 (ADR-0035): processes under processes/,",
        "# briefings under ai/, decisions under decisions/, samples under samples/.",
        "format = 2",
        f'id = "{solution_id}"',
        'version = "0.1.0"',
        f"name = {_toml_table(names)}",
    ]
    if descriptions:
        lines.append(f"description = {_toml_table(descriptions)}")
    lines += [
        'license = "proprietary"',
        "",
        "[requires]",
        'core = "0.0.1"',
        'data_class = "internal"',
        "",
        "# Model roles (ADR-0035 §3): what the solution needs, never which model.",
        "[model_roles.default]",
        "structured_output = true",
        "vision = false",
        'max_data_class = "internal"',
        "",
    ]
    return "\n".join(lines)


def briefing_toml(briefing_id: str, model_role: str = "default") -> str:
    check_id(briefing_id, "briefing")
    check_id(model_role, "model role")
    return "\n".join(
        [
            "# Briefing (ADR-0034 §3): role, instructions, limits, output - no free prompt.",
            f'id = "{briefing_id}"',
            f'model_role = "{model_role}"',
            "read_document = true",
            "",
            "[role]",
            'de = "Du bereitest ein eingehendes Dokument für eine Person vor."',
            'en = "You prepare an incoming document for a person."',
            "",
            "[instructions]",
            'de = "Fasse das Dokument in zwei Sätzen zusammen und nenne Auffälligkeiten."',
            'en = "Summarize the document in two sentences and name anything unusual."',
            "",
            "[output]",
            'summary = "two sentences"',
            'anomalies = "list of anomalies, may be empty"',
            "",
            "[context]",
            "document = true",
            "",
        ]
    )


def decision_dmn(decision_id: str, name: str = "") -> str:
    check_id(decision_id, "decision")
    label = escape(name or decision_id)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/" id="defs_{decision_id}" name="{label}" namespace="https://aitmation.com/solutions">
  <decision id="{decision_id}" name="{label}">
    <decisionTable id="{decision_id}_table" hitPolicy="FIRST">
      <input id="i_value" label="Wert"><inputExpression typeRef="number"><text>steps.read.result.total</text></inputExpression></input>
      <output id="o_outcome" name="outcome" typeRef="string"/>
      <rule id="r_small"><inputEntry><text>&lt; 100</text></inputEntry><outputEntry><text>"small"</text></outputEntry></rule>
      <rule id="r_other"><inputEntry><text>-</text></inputEntry><outputEntry><text>"review"</text></outputEntry></rule>
    </decisionTable>
  </decision>
</definitions>
"""


def process_bpmn(
    process_id: str,
    title: str | dict,
    *,
    ai_ref: str,
    source: str = "manual",
    inputs: dict[str, str] | None = None,
    outputs: dict[str, str] | None = None,
) -> str:
    """start -> AI step (briefing ``ai_ref``) -> human gate -> end, with a
    diagram so the canvas shows it at once. The AI step's failed outcome
    ends the process; the gate's two outcomes end it too - edit from here."""
    check_id(process_id, "process")
    check_id(ai_ref, "briefing")
    if source not in ("manual", "chain", "folder", "mailbox", "schedule"):
        raise ScaffoldError(f"unknown trigger source {source!r}")
    names = _names(title, process_id)
    contract = "\n".join(
        [
            f'        <aimn:input name="{escape(k)}">{escape(v)}</aimn:input>'
            for k, v in (inputs or {"file": "Pfad zum Dokument"}).items()
        ]
        + [
            f'        <aimn:output name="{escape(k)}">{escape(v)}</aimn:output>'
            for k, v in (outputs or {"summary": "Zusammenfassung"}).items()
        ]
    )
    titles = "\n".join(
        f'        <aimn:title lang="{lang}">{escape(text)}</aimn:title>'
        for lang, text in names.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:aimn="https://aitmation.com/schema/aimn/1" xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" id="defs_{process_id}" targetNamespace="https://aitmation.com/solutions">
  <bpmn:process id="{process_id}" name="{escape(names["de"])}" isExecutable="true" aimn:version="1">
    <bpmn:extensionElements>
      <aimn:meta>
{titles}
      </aimn:meta>
      <aimn:contract>
{contract}
      </aimn:contract>
      <aimn:policy name="privacy">
        <aimn:property name="category" value="internal" />
      </aimn:policy>
    </bpmn:extensionElements>
    <bpmn:startEvent id="start" name="start">
      <bpmn:extensionElements>
        <aimn:trigger source="{source}" />
      </bpmn:extensionElements>
    </bpmn:startEvent>
    <bpmn:task id="prepare" name="prepare">
      <bpmn:extensionElements>
        <aimn:aiTask ref="{ai_ref}" />
      </bpmn:extensionElements>
    </bpmn:task>
    <bpmn:userTask id="review" name="review">
      <bpmn:extensionElements>
        <aimn:gate />
      </bpmn:extensionElements>
    </bpmn:userTask>
    <bpmn:endEvent id="finished" />
    <bpmn:endEvent id="stopped" />
    <bpmn:sequenceFlow id="flow_start_fired" name="fired" sourceRef="start" targetRef="prepare" />
    <bpmn:sequenceFlow id="flow_prepare_done" name="done" sourceRef="prepare" targetRef="review" />
    <bpmn:sequenceFlow id="flow_prepare_failed" name="failed" sourceRef="prepare" targetRef="stopped" />
    <bpmn:sequenceFlow id="flow_review_approved" name="approved" sourceRef="review" targetRef="finished" />
    <bpmn:sequenceFlow id="flow_review_rejected" name="rejected" sourceRef="review" targetRef="stopped" />
  </bpmn:process>
  <bpmndi:BPMNDiagram id="diagram_{process_id}">
    <bpmndi:BPMNPlane id="plane_{process_id}" bpmnElement="{process_id}">
      <bpmndi:BPMNShape id="shape_start" bpmnElement="start"><dc:Bounds x="160" y="142" width="36" height="36" /></bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="shape_prepare" bpmnElement="prepare"><dc:Bounds x="300" y="120" width="100" height="80" /></bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="shape_review" bpmnElement="review"><dc:Bounds x="500" y="120" width="100" height="80" /></bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="shape_finished" bpmnElement="finished"><dc:Bounds x="700" y="142" width="36" height="36" /></bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="shape_stopped" bpmnElement="stopped"><dc:Bounds x="700" y="272" width="36" height="36" /></bpmndi:BPMNShape>
      <bpmndi:BPMNEdge id="edge_start_fired" bpmnElement="flow_start_fired"><di:waypoint x="196" y="160" /><di:waypoint x="300" y="160" /></bpmndi:BPMNEdge>
      <bpmndi:BPMNEdge id="edge_prepare_done" bpmnElement="flow_prepare_done"><di:waypoint x="400" y="160" /><di:waypoint x="500" y="160" /></bpmndi:BPMNEdge>
      <bpmndi:BPMNEdge id="edge_prepare_failed" bpmnElement="flow_prepare_failed"><di:waypoint x="350" y="200" /><di:waypoint x="350" y="290" /><di:waypoint x="700" y="290" /></bpmndi:BPMNEdge>
      <bpmndi:BPMNEdge id="edge_review_approved" bpmnElement="flow_review_approved"><di:waypoint x="600" y="160" /><di:waypoint x="700" y="160" /></bpmndi:BPMNEdge>
      <bpmndi:BPMNEdge id="edge_review_rejected" bpmnElement="flow_review_rejected"><di:waypoint x="550" y="200" /><di:waypoint x="550" y="290" /><di:waypoint x="700" y="290" /></bpmndi:BPMNEdge>
    </bpmndi:BPMNPlane>
  </bpmndi:BPMNDiagram>
</bpmn:definitions>
"""


def sidecar_case(
    case_id: str,
    sample: str,
    *,
    description: str = "",
    decisions: dict[str, str] | None = None,
    status: str = "completed",
    outcomes: dict[str, str] | None = None,
) -> str:
    """One ``[[case]]`` block for a process sidecar (ADR-0035 §6)."""
    check_id(case_id, "case")
    if not sample.startswith("samples/"):
        raise ScaffoldError("the sample must live under samples/")
    if status not in ("completed", "running"):
        raise ScaffoldError(f"unknown expected status {status!r}")
    lines = ["", "[[case]]", f'id = "{case_id}"']
    if description:
        lines.append(f'description = "{_q(description)}"')
    lines.append(f'given = {{ file = "{_q(sample)}" }}')
    if decisions:
        lines.append(f"decisions = {_toml_table(decisions)}")
    lines += ["", "[case.expect]", f'status = "{status}"']
    if outcomes:
        lines.append(f"outcomes = {_toml_table(outcomes)}")
    lines.append("")
    return "\n".join(lines)
