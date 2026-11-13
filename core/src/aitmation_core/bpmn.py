"""BPMN 2.0 reader and writer for the aitmation profile (ADR-0034 §1).

The engine (ADR-0008) stays our own; what changes is the language it
reads. A process is a BPMN 2.0 XML file whose elements map onto the five
primitives, carrying AIMN extension elements (:mod:`aitmation_core.aimn`)
for everything BPMN does not know - the AI task, the gate, the tool call,
the trigger source, the chain hand-over.

Profile (anything else is a load error, never a warning)::

    startEvent                 Trigger       aimn:trigger (source, ...)
    task + aimn:aiTask         AgentStep     ref="<ai/ briefing>" or inline
    userTask                   HumanGate     aimn:gate (approving, ...)
    serviceTask                SystemAction  aimn:toolCall action="..."
    businessRuleTask           SystemAction  aimn:decision ref="..." (DMN)
    callActivity               SystemAction  action "call" (waits for child)
    intermediateCatchEvent     Timer         timerEventDefinition
    boundaryEvent (on userTask) gate deadline, no outgoing flow
    exclusiveGateway           pass-through: names the outcomes; with
                               aimn:condition on its flows a step of the
                               internal action "route" (ADR-0056)
    endEvent                   END, or process:<id> via aimn:chainTo
    sequenceFlow               outcome: its name is the outcome key

Outcomes: every sequence flow leaving a step is named; the name is the
outcome key (``done``, ``approved``, ``elapsed`` ...). A step may instead
have one unnamed flow into an exclusive gateway whose named outgoing flows
are then its outcomes - that is how a diagram looks conventional while the
engine sees exactly the same graph.

Diagram interchange (``bpmndi``) is read for nothing and written with a
simple layered layout so that bpmn-js can show a migrated process.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import aimn
from .definition import (
    END,
    PROCESS_PREFIX,
    DefinitionError,
    ProcessDefinition,
    StepDefinition,
    StepKind,
)
from .i18n import LANGUAGES, normalize

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS = "http://www.omg.org/spec/DD/20100524/DI"
AIMN_NS = aimn.NAMESPACE
TARGET_NS = "https://aitmation.com/solutions"

PROFILE_ELEMENTS = frozenset(
    {
        "startEvent",
        "task",
        "userTask",
        "serviceTask",
        "businessRuleTask",
        "callActivity",
        "intermediateCatchEvent",
        "boundaryEvent",
        "exclusiveGateway",
        "parallelGateway",
        "endEvent",
        "sequenceFlow",
        "extensionElements",
        "documentation",
    }
)
_FOR_EACH_ATTRS = ("over", "as", "collect", "max")
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

ET.register_namespace("bpmn", BPMN_NS)
ET.register_namespace("bpmndi", BPMNDI_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("di", DI_NS)
ET.register_namespace("aimn", AIMN_NS)


def _b(tag: str) -> str:
    return f"{{{BPMN_NS}}}{tag}"


def _a(tag: str) -> str:
    return f"{{{AIMN_NS}}}{tag}"


class BpmnError(ValueError):
    """The BPMN file is outside the profile or structurally invalid."""


# -- generic AIMN properties ---------------------------------------------------


def props_to_dict(parent: ET.Element | None) -> dict:
    """``<aimn:property name="x" value="..."/>`` children as a dict.

    Scalars are typed by inference (true/false, int, float, string); a
    property with ``<aimn:item>`` children is a list; nested properties
    form a table. This is the generic escape hatch that lets any step
    config travel in XML without inventing an element per key.
    """
    out: dict = {}
    if parent is None:
        return out
    for prop in parent.findall(_a("property")):
        name = prop.get("name", "")
        if not name:
            raise BpmnError("aimn:property without a name")
        out[name] = _prop_value(prop)
    return out


def _default_value(text: str):
    stripped = text.strip()
    if stripped.startswith(("[", "{")):
        try:
            return json.loads(stripped)
        except ValueError:
            return stripped
    return _scalar(stripped)


def _prop_value(prop: ET.Element):
    items = prop.findall(_a("item"))
    nested = prop.findall(_a("property"))
    if items:
        return [_scalar(item.get("value", item.text or "")) for item in items]
    if nested:
        return props_to_dict(prop)
    if "value" in prop.attrib:
        return _scalar(prop.get("value", ""))
    return _scalar((prop.text or "").strip())


def _scalar(raw: str):
    text = raw.strip()
    if text in ("true", "false"):
        return text == "true"
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?\d+\.\d+", text):
        return float(text)
    return raw


def dict_to_props(parent: ET.Element, values: dict) -> None:
    for name, value in values.items():
        prop = ET.SubElement(parent, _a("property"), {"name": str(name)})
        _write_value(prop, value)


def _write_value(prop: ET.Element, value) -> None:
    if isinstance(value, dict):
        dict_to_props(prop, value)
    elif isinstance(value, list | tuple):
        for item in value:
            ET.SubElement(prop, _a("item"), {"value": _fmt(item)})
    else:
        prop.set("value", _fmt(value))


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _texts(parent: ET.Element, tag: str) -> dict[str, str]:
    """``<aimn:title lang="de">…</aimn:title>`` siblings as a text table."""
    out: dict[str, str] = {}
    for node in parent.findall(_a(tag)):
        lang = node.get("lang", "de")
        if lang not in LANGUAGES:
            raise BpmnError(f"aimn:{tag}: unknown language {lang!r}")
        out[lang] = (node.text or "").strip()
    return out


def _write_texts(parent: ET.Element, tag: str, table: dict[str, str] | str) -> None:
    if isinstance(table, str):
        table = normalize(table, tag)
    table = table or {}
    for lang in LANGUAGES:
        if lang in table:
            node = ET.SubElement(parent, _a(tag), {"lang": lang})
            node.text = table[lang]


# -- reading --------------------------------------------------------------------


@dataclass
class _Flow:
    id: str
    name: str
    source: str
    target: str
    element: ET.Element | None = None


@dataclass
class _Node:
    id: str
    tag: str
    element: ET.Element
    outgoing: list[_Flow] = field(default_factory=list)
    incoming: list[_Flow] = field(default_factory=list)


def parse_bpmn(
    text: str,
    source: str = "<string>",
    briefings: dict[str, aimn.Briefing] | None = None,
    skills: dict[str, aimn.Briefing] | None = None,
    constants: dict | None = None,
    tools: dict | None = None,
) -> ProcessDefinition:
    """Parse BPMN XML into a validated :class:`ProcessDefinition`.

    ``briefings`` resolve ``aimn:aiTask ref="…"``; ``skills`` resolve the
    skills a briefing names. Both come from the solution the process
    belongs to (ADR-0035 §2). ``tools`` are the declared tools in sight:
    a toolCall on a read-only connector (ADR-0042) is marked here so the
    ADR-0007 check knows it before the binder runs.
    """
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise BpmnError(f"{source}: DOCTYPE/ENTITY declarations are not allowed")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise BpmnError(f"{source}: invalid XML: {exc}") from exc
    if root.tag != _b("definitions"):
        raise BpmnError(f"{source}: root element must be bpmn:definitions")
    processes = root.findall(_b("process"))
    if len(processes) != 1:
        raise BpmnError(f"{source}: exactly one bpmn:process required, found {len(processes)}")
    if root.find(_b("collaboration")) is not None:
        raise BpmnError(f"{source}: pools and collaborations are not in the profile")
    process = processes[0]
    process_id = process.get("id", "")
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", process_id):
        raise BpmnError(f"{source}: process id {process_id!r} must be a lowercase identifier")

    ext = process.find(_b("extensionElements"))
    meta, parameters, contract, policies = _process_extensions(ext, source)
    version_raw = process.get(_a("version"), "1")
    try:
        version = int(version_raw)
    except ValueError:
        raise BpmnError(f"{source}: aimn:version must be an integer") from None

    nodes: dict[str, _Node] = {}
    flows: list[_Flow] = []
    for child in process:
        tag = child.tag
        if not tag.startswith(f"{{{BPMN_NS}}}"):
            raise BpmnError(f"{source}: element {tag!r} is not BPMN")
        local = tag[len(BPMN_NS) + 2 :]
        if local not in PROFILE_ELEMENTS:
            raise BpmnError(f"{source}: bpmn:{local} is not in the aitmation profile")
        if local in ("extensionElements", "documentation"):
            continue
        element_id = child.get("id", "")
        if local == "sequenceFlow":
            flows.append(
                _Flow(
                    id=element_id,
                    name=(child.get("name") or "").strip(),
                    source=child.get("sourceRef", ""),
                    target=child.get("targetRef", ""),
                    element=child,
                )
            )
            continue
        if not _ID_PATTERN.match(element_id):
            raise BpmnError(f"{source}: element id {element_id!r} must match {_ID_PATTERN.pattern}")
        if element_id == "end":
            raise BpmnError(f"{source}: element id 'end' is reserved")
        if element_id in nodes:
            raise BpmnError(f"{source}: duplicate element id {element_id!r}")
        nodes[element_id] = _Node(id=element_id, tag=local, element=child)
    for flow in flows:
        if flow.source not in nodes or flow.target not in nodes:
            raise BpmnError(f"{source}: sequence flow {flow.id!r} references an unknown element")
        nodes[flow.source].outgoing.append(flow)
        nodes[flow.target].incoming.append(flow)

    for node in nodes.values():
        if node.tag != "boundaryEvent":
            continue
        host = nodes.get(str(node.element.get("attachedToRef", "")))
        if host is None or host.tag != "userTask":
            raise BpmnError(
                f"{source}: boundary event {node.id!r} must be attached to a userTask - the"
                " profile knows boundary timers only as gate deadlines (ADR-0034)"
            )

    for node in nodes.values():
        if node.tag == "parallelGateway":
            _classify_parallel(node, source)

    steps: dict[str, StepDefinition] = {}
    for node in nodes.values():
        if _is_routing(node):
            # A deciding gateway with conditions is a step (ADR-0056 §3).
            outcomes, forks = _outcomes(node, nodes, source)
            steps[node.id] = StepDefinition(
                id=node.id, kind=StepKind.SYSTEM_ACTION, next=outcomes,
                config=_route_config(node, nodes, source), forks=forks,
            )
            continue
        if node.tag in ("exclusiveGateway", "endEvent", "boundaryEvent"):
            continue
        if node.tag == "parallelGateway":
            if _is_parallel_split(node):
                continue  # a split is a property of the flows into it, not a step
            # A parallel join is the synchronisation step (ADR-0037 §1).
            target = _resolve_target(nodes[node.outgoing[0].target], nodes, source)
            if target is END or (isinstance(target, str) and target.startswith(PROCESS_PREFIX)):
                raise BpmnError(
                    f"{source}: join {node.id!r} must continue to a step, not to the end"
                )
            steps[node.id] = StepDefinition(
                id=node.id, kind=StepKind.SYSTEM_ACTION, next={"done": target},
                config={"action": "join"},
            )
            continue
        kind, config = _step_of(node, source, briefings or {}, skills or {})
        if kind is StepKind.SYSTEM_ACTION and tools:
            tool = tools.get(str(config.get("action", "")))
            if tool is not None and getattr(tool, "effect", "") == "read":
                config["tool"], config["effect"] = tool.id, "read"
        if node.tag == "userTask":
            _attach_boundary_deadline(node, nodes, config, source)
        outcomes, forks = _outcomes(node, nodes, source)
        if node.tag == "startEvent":
            if len(outcomes) != 1:
                raise BpmnError(
                    f"{source}: start event {node.id!r} needs exactly one outgoing flow"
                )
            key = next(iter(outcomes))
            outcomes = {"fired": outcomes[key]}
            forks = {"fired": forks[key]} if key in forks else {}
        if node.tag == "intermediateCatchEvent":
            if len(outcomes) != 1:
                raise BpmnError(f"{source}: timer {node.id!r} needs exactly one outgoing flow")
            key = next(iter(outcomes))
            outcomes = {"elapsed": outcomes[key]}
            forks = {"elapsed": forks[key]} if key in forks else {}
        steps[node.id] = StepDefinition(
            id=node.id, kind=kind, next=outcomes, config=config, forks=forks
        )

    # Parameter substitution and the shared post-load checks are the same
    # as for TOML templates: one rule set, two file formats.
    from .template import TemplateError, finish_definition

    try:
        return finish_definition(
            process_id, version, steps, meta, parameters, contract, policies, source,
            constants=constants,
        )
    except TemplateError as exc:
        raise BpmnError(str(exc)) from exc


def _process_extensions(ext: ET.Element | None, source: str):
    meta: dict = {}
    parameters: dict = {}
    contract: dict = {}
    policies: dict = {}
    if ext is None:
        return meta, parameters, contract, policies
    meta_el = ext.find(_a("meta"))
    if meta_el is not None:
        title = _texts(meta_el, "title")
        description = _texts(meta_el, "description")
        if title:
            meta["title"] = title
        if description:
            meta["description"] = description
        meta.update(props_to_dict(meta_el))
    for param in ext.findall(_a("parameter")):
        name = param.get("name", "")
        if not re.match(r"^[a-z0-9_]+$", name):
            raise BpmnError(f"{source}: parameter name {name!r} is invalid")
        spec: dict = {}
        if "default" in param.attrib:
            spec["default"] = _scalar(param.get("default", ""))
        if "description" in param.attrib:
            spec["description"] = param.get("description")
        parameters[name] = spec
    contract_el = ext.find(_a("contract"))
    if contract_el is not None:
        for direction in ("input", "output"):
            entries = {
                node.get("name", ""): (node.text or "").strip()
                for node in contract_el.findall(_a(direction))
            }
            if entries:
                contract[direction] = entries
        # ``default="…"`` on an input: what the start form offers when the
        # person does not say otherwise (typed like a property value; a
        # JSON list or table is parsed).
        defaults = {}
        for node in contract_el.findall(_a("input")):
            if "default" in node.attrib:
                defaults[node.get("name", "")] = _default_value(node.get("default", ""))
        if defaults:
            contract["defaults"] = defaults
    for policy in ext.findall(_a("policy")):
        name = policy.get("name", "")
        if not name:
            raise BpmnError(f"{source}: aimn:policy without a name")
        policies[name] = props_to_dict(policy)
    return meta, parameters, contract, policies


def _extension(node: _Node, tag: str) -> ET.Element | None:
    ext = node.element.find(_b("extensionElements"))
    return None if ext is None else ext.find(_a(tag))


def _step_of(
    node: _Node,
    source: str,
    briefings: dict[str, aimn.Briefing],
    skills: dict[str, aimn.Briefing],
) -> tuple[StepKind, dict]:
    kind, config = _step_of_plain(node, source, briefings, skills)
    loop = node.element.find(_b("multiInstanceLoopCharacteristics"))
    if loop is not None:
        if node.tag not in ("task", "serviceTask"):
            raise BpmnError(
                f"{source}: multi-instance on {node.id!r} - only task and serviceTask may be"
                " multi-instance (ADR-0037 §2)"
            )
        if (loop.get("isSequential") or "false").lower() == "true":
            raise BpmnError(f"{source}: {node.id!r}: sequential multi-instance is not supported")
        spec: dict = {}
        for attr in _FOR_EACH_ATTRS:
            value = loop.get(_a(attr))
            if value is not None:
                spec[attr] = int(value) if attr == "max" else value
        if "over" not in spec or "as" not in spec:
            raise BpmnError(
                f"{source}: {node.id!r}: multiInstanceLoopCharacteristics needs aimn:over and"
                " aimn:as"
            )
        config["for_each"] = spec
    return kind, config


def _step_of_plain(
    node: _Node,
    source: str,
    briefings: dict[str, aimn.Briefing],
    skills: dict[str, aimn.Briefing],
) -> tuple[StepKind, dict]:
    tag = node.tag
    if tag == "startEvent":
        trigger = _extension(node, "trigger")
        config = props_to_dict(trigger)
        if trigger is not None and trigger.get("source"):
            config["source"] = trigger.get("source")
        if trigger is not None and trigger.get("channel"):
            # ADR-0057: the way in (chat, messenger, …) - a process may have
            # one start event per channel; the payload's "channel" picks it.
            channel = trigger.get("channel", "")
            if not _ID_PATTERN.match(channel):
                raise BpmnError(f"{source}: start event {node.id!r}: channel {channel!r} is no id")
            config["channel"] = channel
        return StepKind.TRIGGER, config
    if tag == "task":
        ai_task = _extension(node, "aiTask")
        if ai_task is None:
            raise BpmnError(
                f"{source}: task {node.id!r} has no aimn:aiTask - plain tasks are not in the"
                " profile (use serviceTask for tools, userTask for people)"
            )
        return StepKind.AGENT_STEP, _ai_task_config(node.id, ai_task, source, briefings, skills)
    if tag == "userTask":
        gate = _extension(node, "gate")
        config = props_to_dict(gate)
        if gate is not None:
            approving = gate.get("approving")
            if approving:
                config["approving_outcomes"] = [
                    o.strip() for o in approving.split(",") if o.strip()
                ]
            if gate.get("kind"):
                config["gate_kind"] = gate.get("kind")
            if gate.get("rule"):
                # the facts that make two decisions "the same" (ADR-0054 §3)
                fields = gate.get("rule").split(",")
                config["rule_fields"] = [f.strip() for f in fields if f.strip()]
        return StepKind.HUMAN_GATE, config
    if tag == "serviceTask":
        call = _extension(node, "toolCall")
        if call is None or not call.get("action"):
            raise BpmnError(f"{source}: serviceTask {node.id!r} needs aimn:toolCall action=…")
        config = props_to_dict(call)
        config["action"] = call.get("action")
        if call.get("mandate") == "true":
            config["mandate"] = True
        if call.get("exec"):
            config["exec"] = call.get("exec")
        if call.get("effect"):
            config["effect"] = call.get("effect")
        return StepKind.SYSTEM_ACTION, config
    if tag == "businessRuleTask":
        decision = _extension(node, "decision")
        if decision is None or not decision.get("ref"):
            raise BpmnError(f"{source}: businessRuleTask {node.id!r} needs aimn:decision ref=…")
        config = props_to_dict(decision)
        config["action"] = "decide"
        config["decision"] = decision.get("ref")
        return StepKind.SYSTEM_ACTION, config
    if tag == "callActivity":
        called = node.element.get("calledElement", "")
        if not called:
            raise BpmnError(f"{source}: callActivity {node.id!r} needs calledElement")
        config = props_to_dict(_extension(node, "call"))
        config["action"] = "call"
        config["process"] = called
        return StepKind.SYSTEM_ACTION, config
    if tag == "intermediateCatchEvent":
        timer_def = node.element.find(_b("timerEventDefinition"))
        if timer_def is None:
            raise BpmnError(
                f"{source}: intermediateCatchEvent {node.id!r} must carry a timerEventDefinition"
            )
        config = props_to_dict(_extension(node, "timer"))
        duration = timer_def.find(_b("timeDuration"))
        if duration is not None and duration.text:
            config.setdefault("seconds", _duration_seconds(duration.text.strip(), source))
        if "seconds" not in config and "until_from" not in config:
            raise BpmnError(
                f"{source}: timer {node.id!r} needs timeDuration or aimn:timer seconds/until_from"
            )
        return StepKind.TIMER, config
    raise BpmnError(f"{source}: bpmn:{tag} is not in the aitmation profile")


def _ai_task_config(
    step_id: str,
    ai_task: ET.Element,
    source: str,
    briefings: dict[str, aimn.Briefing],
    skills: dict[str, aimn.Briefing],
) -> dict:
    ref = ai_task.get("ref")
    if ref:
        try:
            briefing = briefings[ref]
        except KeyError:
            raise BpmnError(
                f"{source}: task {step_id!r} references briefing {ref!r} that the solution"
                " does not provide (ai/<id>.aimn.toml)"
            ) from None
    else:
        data: dict = {"id": step_id}
        role = _texts(ai_task, "role")
        instructions = _texts(ai_task, "instructions")
        if role:
            data["role"] = role
        if instructions:
            data["instructions"] = instructions
        limits = [
            {node.get("lang", "de"): (node.text or "").strip()}
            for node in ai_task.findall(_a("limit"))
        ]
        if limits:
            data["limits"] = limits
        outputs = {
            node.get("name", ""): (node.text or "").strip()
            for node in ai_task.findall(_a("output"))
        }
        if outputs:
            data["output"] = outputs
        for attr, key in (
            ("modelRole", "model_role"),
            ("flexibility", "flexibility"),
            ("confidenceThreshold", "confidence_threshold"),
        ):
            if ai_task.get(attr):
                data[key] = ai_task.get(attr)
        if ai_task.get("skills"):
            data["skills"] = [s.strip() for s in ai_task.get("skills", "").split(",") if s.strip()]
        if ai_task.get("tools"):
            data["tools"] = [t.strip() for t in ai_task.get("tools", "").split(",") if t.strip()]
        data.update(props_to_dict(ai_task))
        if not role and not instructions and (data.get("classify") or data.get("assign")):
            # Core mechanics (intake classifier, Vermittler assigner): the
            # task is built at runtime from the loaded document types or
            # addressees - there is no briefing to compose or to hash.
            config = {k: v for k, v in data.items() if k != "id"}
            return config
        try:
            briefing = aimn.briefing_from_dict(data, f"{source}: task {step_id!r}", step_id)
        except aimn.BriefingError as exc:
            raise BpmnError(str(exc)) from exc
    try:
        return briefing.to_config(skills)
    except aimn.BriefingError as exc:
        raise BpmnError(f"{source}: task {step_id!r}: {exc}") from exc


def _attach_boundary_deadline(node: _Node, nodes: dict[str, _Node], config: dict, source: str):
    for other in nodes.values():
        if other.tag != "boundaryEvent" or other.element.get("attachedToRef") != node.id:
            continue
        timer_def = other.element.find(_b("timerEventDefinition"))
        if timer_def is None:
            raise BpmnError(f"{source}: boundary event {other.id!r} must be a timer")
        if other.outgoing:
            raise BpmnError(
                f"{source}: boundary timer {other.id!r} may not have outgoing flows - it is the"
                " gate's escalation deadline, not a transition (ADR-0034 profile)"
            )
        duration = timer_def.find(_b("timeDuration"))
        if duration is None or not duration.text:
            raise BpmnError(f"{source}: boundary timer {other.id!r} needs timeDuration")
        config["deadline_seconds"] = _duration_seconds(duration.text.strip(), source)


def _duration_seconds(text: str, source: str) -> int:
    match = _DURATION.match(text)
    if not match or text in ("P", "PT"):
        raise BpmnError(f"{source}: duration {text!r} is not ISO 8601 (e.g. PT24H, P2D)")
    parts = {k: int(v or 0) for k, v in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def _flow_conditions(flow: _Flow) -> tuple[str, list[ET.Element]]:
    """``(match, conditions)`` of a flow: ``aimn:condition`` children of its
    extensions (all must hold) or of an ``aimn:when match="any|all"``."""
    if flow.element is None:
        return "all", []
    ext = flow.element.find(_b("extensionElements"))
    if ext is None:
        return "all", []
    when = ext.find(_a("when"))
    if when is not None:
        return (when.get("match") or "all"), when.findall(_a("condition"))
    return "all", ext.findall(_a("condition"))


def _is_routing(node: _Node) -> bool:
    """An exclusive gateway that decides by conditions (ADR-0056)."""
    if node.tag != "exclusiveGateway":
        return False
    return bool(node.element.get("default")) or any(
        _flow_conditions(f)[1] for f in node.outgoing
    )


_CONDITION_ROOTS = ("trigger", "steps", "records")


def _route_config(node: _Node, nodes: dict[str, _Node], source: str) -> dict:
    """The ``route`` action's config: branches in file order, one default."""
    from . import dmn

    if len(node.outgoing) < 2:
        raise BpmnError(f"{source}: deciding gateway {node.id!r} needs at least two flows")
    default_id = node.element.get("default") or ""
    routes: list[dict] = []
    default = ""
    for flow in node.outgoing:
        match, conditions = _flow_conditions(flow)
        if match not in ("all", "any"):
            raise BpmnError(f"{source}: flow {flow.id!r}: aimn:when match must be all or any")
        if not flow.name:
            raise BpmnError(
                f"{source}: flow {flow.id!r} out of gateway {node.id!r} needs a name (the branch)"
            )
        if flow.id == default_id or not conditions:
            if conditions:
                raise BpmnError(
                    f"{source}: default flow {flow.id!r} of {node.id!r} must not carry conditions"
                )
            if default:
                raise BpmnError(
                    f"{source}: gateway {node.id!r} has more than one default flow"
                    f" ({default!r}, {flow.name!r}) - only one branch is 'otherwise'"
                )
            default = flow.name
            continue
        tests = []
        for cond in conditions:
            path = (cond.get("input") or "").strip()
            test = (cond.get("test") or "").strip()
            root = path.split(".", 1)[0]
            if root not in _CONDITION_ROOTS or "." not in path:
                raise BpmnError(
                    f"{source}: condition on {flow.id!r}: input {path!r} must start with"
                    " trigger., steps. or records."
                )
            if root == "steps":
                step_ref = path.split(".")[1]
                if step_ref not in nodes:
                    raise BpmnError(
                        f"{source}: condition on {flow.id!r} reads step {step_ref!r},"
                        " which the process does not have"
                    )
                if step_ref == node.id:
                    raise BpmnError(
                        f"{source}: condition on {flow.id!r} reads its own gateway {node.id!r}"
                    )
            if not test:
                raise BpmnError(f"{source}: condition on {flow.id!r} needs a test")
            try:
                dmn._compile_unary(test, f"{source}: {flow.id}")
            except dmn.DmnError as exc:
                raise BpmnError(str(exc)) from exc
            tests.append({"input": path, "test": test})
        routes.append({"outcome": flow.name, "match": match, "conditions": tests})
    if default_id and not default:
        raise BpmnError(f"{source}: default {default_id!r} of {node.id!r} is not one of its flows")
    if not default:
        raise BpmnError(
            f"{source}: deciding gateway {node.id!r} needs one flow without conditions"
            " (the 'otherwise' branch)"
        )
    if not routes:
        raise BpmnError(f"{source}: gateway {node.id!r} has only a default flow")
    return {"action": "route", "routes": routes, "default": default}


def _is_parallel_split(node: _Node) -> bool:
    return node.tag == "parallelGateway" and len(node.outgoing) > 1


def _classify_parallel(node: _Node, source: str) -> None:
    """A parallel gateway is a split (1 in, n out) or a join (n in, 1 out)
    - never both, never a router (ADR-0037 §1). Its outgoing flows are
    unnamed (nothing is chosen); incoming flows carry the outcome names
    of the steps they come from, as everywhere else."""
    for flow in node.outgoing:
        if flow.name:
            raise BpmnError(
                f"{source}: flow {flow.id!r} at parallel gateway {node.id!r} must be unnamed -"
                " a parallel gateway chooses nothing"
            )
    if len(node.incoming) == 1 and len(node.outgoing) >= 2:
        return
    if len(node.incoming) >= 2 and len(node.outgoing) == 1:
        return
    raise BpmnError(
        f"{source}: parallel gateway {node.id!r} must be a split (1 in, 2+ out) or a join"
        f" (2+ in, 1 out), found {len(node.incoming)} in / {len(node.outgoing)} out"
    )


def _split_heads(split: _Node, nodes: dict[str, _Node], source: str) -> tuple[str, ...]:
    heads: list[str] = []
    for flow in split.outgoing:
        target = nodes[flow.target]
        if (
            target.tag in ("exclusiveGateway", "endEvent", "boundaryEvent")
            and not _is_routing(target)
        ) or (target.tag == "parallelGateway" and _is_parallel_split(target)):
            raise BpmnError(
                f"{source}: branch of split {split.id!r} must start with a step or a join,"
                f" not {target.id!r}"
            )
        heads.append(target.id)
    return tuple(heads)


def _outcomes(
    node: _Node, nodes: dict[str, _Node], source: str
) -> tuple[dict[str, str | None], dict[str, tuple[str, ...]]]:
    out: dict[str, str | None] = {}
    forks: dict[str, tuple[str, ...]] = {}
    if not node.outgoing:
        raise BpmnError(f"{source}: element {node.id!r} has no outgoing sequence flow")
    for flow in node.outgoing:
        target = nodes[flow.target]
        if target.tag == "parallelGateway" and _is_parallel_split(target):
            name = flow.name
            if not name:
                if node.tag in ("startEvent", "intermediateCatchEvent") and len(node.outgoing) == 1:
                    name = "_"
                else:
                    raise BpmnError(
                        f"{source}: flow {flow.id!r} from {node.id!r} into split {target.id!r}"
                        " needs a name (the outcome key)"
                    )
            if name in out:
                raise BpmnError(f"{source}: element {node.id!r} has outcome {name!r} twice")
            heads = _split_heads(target, nodes, source)
            forks[name] = heads
            out[name] = heads[0]
            continue
        if target.tag == "exclusiveGateway" and not _is_routing(target):
            if flow.name:
                raise BpmnError(
                    f"{source}: flow {flow.id!r} into gateway {target.id!r} must be unnamed -"
                    " the gateway's outgoing flows name the outcomes"
                )
            for name, resolved in _gateway_outcomes(target, nodes, source, set()).items():
                if name in out:
                    raise BpmnError(f"{source}: element {node.id!r} has outcome {name!r} twice")
                out[name] = resolved
            continue
        if not flow.name:
            if node.tag in ("startEvent", "intermediateCatchEvent") and len(node.outgoing) == 1:
                # Events have exactly one implicit outcome (fired / elapsed).
                out["_"] = _resolve_target(target, nodes, source)
                continue
            raise BpmnError(
                f"{source}: flow {flow.id!r} from {node.id!r} needs a name (the outcome key)"
            )
        if not re.match(r"^[a-z][a-z0-9_]*$", flow.name):
            raise BpmnError(f"{source}: outcome name {flow.name!r} must be a lowercase identifier")
        if flow.name in out:
            raise BpmnError(f"{source}: element {node.id!r} has outcome {flow.name!r} twice")
        out[flow.name] = _resolve_target(target, nodes, source)
    return out, forks


def _gateway_outcomes(
    gateway: _Node, nodes: dict[str, _Node], source: str, seen: set[str]
) -> dict[str, str | None]:
    if gateway.id in seen:
        raise BpmnError(f"{source}: gateway cycle at {gateway.id!r}")
    seen.add(gateway.id)
    if len(gateway.outgoing) == 1 and not gateway.outgoing[0].name:
        # Converging gateway: a pure join, pass through.
        target = nodes[gateway.outgoing[0].target]
        if target.tag == "exclusiveGateway":
            return _gateway_outcomes(target, nodes, source, seen)
        raise BpmnError(
            f"{source}: joining gateway {gateway.id!r} must not be the only path of a step -"
            " name the flow out of the step instead"
        )
    out: dict[str, str | None] = {}
    for flow in gateway.outgoing:
        if not flow.name:
            raise BpmnError(
                f"{source}: flow {flow.id!r} out of gateway {gateway.id!r} needs a name"
            )
        target = nodes[flow.target]
        if (target.tag == "exclusiveGateway" and not _is_routing(target)) or (
            target.tag == "parallelGateway" and _is_parallel_split(target)
        ):
            raise BpmnError(
                f"{source}: gateway {gateway.id!r} routes into gateway {target.id!r} - chained"
                " deciding gateways are not in the profile (put the split behind a step)"
            )
        out[flow.name] = _resolve_target(target, nodes, source)
    return out


def _resolve_target(target: _Node, nodes: dict[str, _Node], source: str) -> str | None:
    if target.tag == "endEvent":
        chain = _extension(target, "chainTo")
        if chain is not None:
            process = chain.get("process", "")
            if not process:
                raise BpmnError(f"{source}: aimn:chainTo on {target.id!r} needs process=…")
            return PROCESS_PREFIX + process
        return END
    if _is_routing(target):
        return target.id
    if target.tag == "exclusiveGateway":
        # A join reached through a named flow: follow its single outgoing.
        if len(target.outgoing) == 1 and not target.outgoing[0].name:
            return _resolve_target(nodes[target.outgoing[0].target], nodes, source)
        raise BpmnError(
            f"{source}: named flow into deciding gateway {target.id!r} is not in the profile"
        )
    if target.tag == "boundaryEvent":
        raise BpmnError(f"{source}: flows may not target boundary event {target.id!r}")
    if target.tag == "parallelGateway" and _is_parallel_split(target):
        raise BpmnError(
            f"{source}: split {target.id!r} must be entered from a step's outcome directly"
        )
    return target.id


def load_bpmn(
    path: str | Path,
    briefings: dict[str, aimn.Briefing] | None = None,
    skills: dict[str, aimn.Briefing] | None = None,
) -> ProcessDefinition:
    path = Path(path)
    return parse_bpmn(path.read_text(encoding="utf-8"), str(path), briefings, skills)


# -- writing --------------------------------------------------------------------


def to_bpmn(
    definition: ProcessDefinition,
    *,
    inline_briefings: bool = False,
    briefing_ids: dict[str, str] | None = None,
) -> tuple[str, dict[str, aimn.Briefing]]:
    """Serialize a definition as BPMN XML with a layered diagram.

    AgentStep configs are split into briefings (returned, to be written
    as ``ai/<id>.aimn.toml``) and the task element references them; with
    ``inline_briefings`` the briefing is embedded instead.
    """
    root = ET.Element(
        _b("definitions"),
        {"id": f"defs_{definition.id}", "targetNamespace": TARGET_NS},
    )
    process = ET.SubElement(
        root,
        _b("process"),
        {
            "id": definition.id,
            "name": _lead(definition.meta.get("title")) or definition.id,
            "isExecutable": "true",
            _a("version"): str(definition.version),
        },
    )
    _write_process_extensions(process, definition)

    briefings: dict[str, aimn.Briefing] = {}
    end_nodes: dict[str | None, str] = {}  # END / process:x -> endEvent id
    splits: dict[tuple[str, str], str] = {}  # (step, outcome) -> split gateway id
    for step in definition.steps.values():
        element = _write_step(process, step, briefings, inline_briefings, briefing_ids or {})
        if not step.is_join:
            ET.SubElement(element, _b("documentation")).text = step.id
        for outcome, heads in step.forks.items():
            split_id = _unique(f"{step.id}_{outcome}_split", definition.steps, splits.values())
            splits[(step.id, outcome)] = split_id
            ET.SubElement(process, _b("parallelGateway"), {"id": split_id})
            for head in heads:
                ET.SubElement(
                    process, _b("sequenceFlow"),
                    {"id": f"flow_{split_id}_{head}", "sourceRef": split_id, "targetRef": head},
                )
    for step in definition.steps.values():
        for outcome, target in step.next.items():
            if (step.id, outcome) in splits:
                ET.SubElement(
                    process, _b("sequenceFlow"),
                    {"id": f"flow_{step.id}_{outcome}", "name": outcome, "sourceRef": step.id,
                     "targetRef": splits[(step.id, outcome)]},
                )
                continue
            if step.is_join:
                ET.SubElement(
                    process, _b("sequenceFlow"),
                    {"id": f"flow_{step.id}_{outcome}", "sourceRef": step.id,
                     "targetRef": str(target)},
                )
                continue
            if target is END or (isinstance(target, str) and target.startswith(PROCESS_PREFIX)):
                key = target
                if key not in end_nodes:
                    suffix = (
                        "finished"
                        if target is END
                        else "to_" + target[len(PROCESS_PREFIX) :].replace("-", "_")
                    )
                    end_id = f"{suffix}" if suffix not in definition.steps else f"{suffix}_event"
                    end_nodes[key] = _unique(end_id, definition.steps, end_nodes.values())
                    end_el = ET.SubElement(process, _b("endEvent"), {"id": end_nodes[key]})
                    if target is not END:
                        ext = ET.SubElement(end_el, _b("extensionElements"))
                        ET.SubElement(
                            ext, _a("chainTo"), {"process": target[len(PROCESS_PREFIX) :]}
                        )
                target_id = end_nodes[key]
            else:
                target_id = target
            flow_id = f"flow_{step.id}_{outcome}"
            ET.SubElement(
                process,
                _b("sequenceFlow"),
                {"id": flow_id, "name": outcome, "sourceRef": step.id, "targetRef": target_id},
            )
    _write_diagram(root, definition, end_nodes, splits)
    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n", briefings


def _unique(candidate: str, *taken) -> str:
    names = set()
    for group in taken:
        names.update(group)
    name = candidate
    n = 2
    while name in names:
        name = f"{candidate}_{n}"
        n += 1
    return name


def _lead(text) -> str:
    if isinstance(text, dict):
        return text.get("de") or next(iter(text.values()), "")
    return str(text or "")


def _write_process_extensions(process: ET.Element, definition: ProcessDefinition) -> None:
    has_meta = bool(definition.meta)
    if not (has_meta or definition.parameters or definition.contract or definition.policies):
        return
    ext = ET.SubElement(process, _b("extensionElements"))
    if has_meta:
        meta = ET.SubElement(ext, _a("meta"))
        rest = dict(definition.meta)
        for key in ("title", "description"):
            if key in rest:
                _write_texts(meta, key, rest.pop(key))
        dict_to_props(meta, rest)
    for name, spec in definition.parameters.items():
        attrs = {"name": name}
        if isinstance(spec, dict):
            if "default" in spec and spec["default"] is not None:
                attrs["default"] = _fmt(spec["default"])
            if spec.get("description"):
                attrs["description"] = str(spec["description"])
        elif spec is not None:
            attrs["default"] = _fmt(spec)
        ET.SubElement(ext, _a("parameter"), attrs)
    if definition.contract:
        contract = ET.SubElement(ext, _a("contract"))
        for direction in ("input", "output"):
            for name, desc in (definition.contract.get(direction) or {}).items():
                ET.SubElement(contract, _a(direction), {"name": name}).text = str(desc)
    for name, values in definition.policies.items():
        policy = ET.SubElement(ext, _a("policy"), {"name": name})
        dict_to_props(policy, values)


def _write_step(
    process: ET.Element,
    step: StepDefinition,
    briefings: dict[str, aimn.Briefing],
    inline: bool,
    briefing_ids: dict[str, str],
) -> ET.Element:
    config = dict(step.config)
    if step.kind is StepKind.TRIGGER:
        element = ET.SubElement(process, _b("startEvent"), {"id": step.id, "name": step.id})
        ext = ET.SubElement(element, _b("extensionElements"))
        attrs = {"source": str(config.pop("source"))} if "source" in config else {}
        trigger = ET.SubElement(ext, _a("trigger"), attrs)
        dict_to_props(trigger, config)
        return element
    for_each = config.pop("for_each", None)
    if step.kind is StepKind.AGENT_STEP:
        element = ET.SubElement(process, _b("task"), {"id": step.id, "name": step.id})
        _write_for_each(element, for_each)
        ext = ET.SubElement(element, _b("extensionElements"))
        if "briefing" in config:
            briefing = _briefing_from_config(step.id, config)
            rest = {
                k: v
                for k, v in config.items()
                if k
                not in (
                    "briefing",
                    "task",
                    "prompt_hash",
                    "output",
                    "context",
                    "confidence_threshold",
                )
                and k not in aimn.PASS_THROUGH_KEYS
            }
        else:
            briefing, rest = aimn.briefing_from_legacy_config(step.id, config)
        ai_task = ET.SubElement(ext, _a("aiTask"))
        if inline:
            _write_inline_briefing(ai_task, briefing)
        else:
            wanted = briefing_ids.get(step.id, briefing.id)
            if wanted != briefing.id:
                briefing = aimn.Briefing(**{**briefing.__dict__, "id": wanted})
            ai_task.set("ref", briefing.id)
            briefings[briefing.id] = briefing
        dict_to_props(ai_task, rest)
        return element
    if step.kind is StepKind.HUMAN_GATE:
        element = ET.SubElement(process, _b("userTask"), {"id": step.id, "name": step.id})
        ext = ET.SubElement(element, _b("extensionElements"))
        attrs = {}
        approving = config.pop("approving_outcomes", None)
        if approving:
            attrs["approving"] = ",".join(approving)
        if config.get("gate_kind"):
            attrs["kind"] = str(config.pop("gate_kind"))
        rule = config.pop("rule_fields", None)
        if rule:
            attrs["rule"] = ", ".join(rule)
        gate = ET.SubElement(ext, _a("gate"), attrs)
        deadline = config.pop("deadline_seconds", None)
        dict_to_props(gate, config)
        if deadline is not None:
            boundary = ET.SubElement(
                process,
                _b("boundaryEvent"),
                {"id": f"{step.id}_deadline", "attachedToRef": step.id, "cancelActivity": "false"},
            )
            timer = ET.SubElement(boundary, _b("timerEventDefinition"))
            ET.SubElement(timer, _b("timeDuration")).text = _iso_duration(int(deadline))
        return element
    if step.kind is StepKind.SYSTEM_ACTION:
        action = str(config.pop("action", "export"))
        if action == "join":
            return ET.SubElement(process, _b("parallelGateway"), {"id": step.id})
        if action == "decide" and "decision" in config:
            element = ET.SubElement(
                process, _b("businessRuleTask"), {"id": step.id, "name": step.id}
            )
            ext = ET.SubElement(element, _b("extensionElements"))
            decision = ET.SubElement(ext, _a("decision"), {"ref": str(config.pop("decision"))})
            dict_to_props(decision, config)
            return element
        if action == "call" and "process" in config:
            element = ET.SubElement(
                process,
                _b("callActivity"),
                {"id": step.id, "name": step.id, "calledElement": str(config.pop("process"))},
            )
            if config:
                ext = ET.SubElement(element, _b("extensionElements"))
                dict_to_props(ET.SubElement(ext, _a("call")), config)
            return element
        element = ET.SubElement(process, _b("serviceTask"), {"id": step.id, "name": step.id})
        _write_for_each(element, for_each)
        ext = ET.SubElement(element, _b("extensionElements"))
        attrs = {"action": action}
        if config.pop("mandate", None) is True:
            attrs["mandate"] = "true"
        if config.get("exec"):
            attrs["exec"] = str(config.pop("exec"))
        if config.get("effect"):
            attrs["effect"] = str(config.pop("effect"))
        call = ET.SubElement(ext, _a("toolCall"), attrs)
        dict_to_props(call, config)
        return element
    if step.kind is StepKind.TIMER:
        element = ET.SubElement(
            process, _b("intermediateCatchEvent"), {"id": step.id, "name": step.id}
        )
        timer_def = ET.SubElement(element, _b("timerEventDefinition"))
        if "seconds" in config and "until_from" not in config:
            ET.SubElement(timer_def, _b("timeDuration")).text = _iso_duration(
                int(config.pop("seconds"))
            )
        if config:
            ext = ET.SubElement(element, _b("extensionElements"))
            dict_to_props(ET.SubElement(ext, _a("timer")), config)
        return element
    raise BpmnError(f"cannot serialize step kind {step.kind}")


def _write_for_each(element: ET.Element, spec) -> None:
    if not isinstance(spec, dict):
        return
    attrs = {_a(k): str(spec[k]) for k in _FOR_EACH_ATTRS if k in spec}
    ET.SubElement(element, _b("multiInstanceLoopCharacteristics"), attrs)


def _briefing_from_config(step_id: str, config: dict) -> aimn.Briefing:
    data = dict(config["briefing"])
    data["id"] = data.get("id") or step_id
    data = {k: v for k, v in data.items() if v not in ({}, [], "", None)}
    if config.get("output"):
        data["output"] = config["output"]
    if config.get("context"):
        data["context"] = config["context"]
    if config.get("confidence_threshold") is not None:
        data["confidence_threshold"] = config["confidence_threshold"]
    for key in aimn.PASS_THROUGH_KEYS:
        if key in config:
            data[key] = config[key]
    return aimn.briefing_from_dict(data, f"step {step_id!r}", step_id)


def _write_inline_briefing(ai_task: ET.Element, briefing: aimn.Briefing) -> None:
    if briefing.model_role:
        ai_task.set("modelRole", briefing.model_role)
    if briefing.flexibility != "strict":
        ai_task.set("flexibility", briefing.flexibility)
    if briefing.confidence_threshold is not None:
        ai_task.set("confidenceThreshold", str(briefing.confidence_threshold))
    if briefing.skills:
        ai_task.set("skills", ",".join(briefing.skills))
    if briefing.tools:
        ai_task.set("tools", ",".join(briefing.tools))
    _write_texts(ai_task, "role", briefing.role)
    _write_texts(ai_task, "instructions", briefing.instructions)
    for limit in briefing.limits:
        for lang in LANGUAGES:
            if lang in limit:
                ET.SubElement(ai_task, _a("limit"), {"lang": lang}).text = limit[lang]
    for name, desc in briefing.output.items():
        ET.SubElement(ai_task, _a("output"), {"name": name}).text = desc
    rest = dict(briefing.extra)
    if briefing.context:
        rest["context"] = briefing.context
    dict_to_props(ai_task, rest)


def _iso_duration(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    out = "P"
    if days:
        out += f"{days}D"
    if hours or minutes or secs or not days:
        out += "T"
        if hours:
            out += f"{hours}H"
        if minutes:
            out += f"{minutes}M"
        if secs or not (hours or minutes or days):
            out += f"{secs}S"
    return out


# -- diagram ---------------------------------------------------------------------

_SIZES = {
    "event": (36, 36),
    "task": (100, 80),
}


def _write_diagram(
    root: ET.Element,
    definition: ProcessDefinition,
    end_nodes: dict,
    splits: dict[tuple[str, str], str] | None = None,
) -> None:
    """Layered left-to-right layout: BFS depth = column, order = row."""
    splits = splits or {}
    levels: dict[str, int] = {}
    order: list[str] = []
    queue = deque([(start.id, 0) for start in definition.start_steps()])
    while queue:
        node, depth = queue.popleft()
        if node in levels:
            continue
        levels[node] = depth
        order.append(node)
        if node not in definition.steps:
            continue  # a split gateway: its heads were queued with it
        step = definition.steps[node]
        for outcome in step.next:
            if (node, outcome) in splits:
                split_id = splits[(node, outcome)]
                if split_id not in levels:
                    levels[split_id] = depth + 1
                    order.append(split_id)
                for head in step.forks[outcome]:
                    queue.append((head, depth + 2))
                continue
            target = step.next[outcome]
            if isinstance(target, str) and not target.startswith(PROCESS_PREFIX):
                queue.append((target, depth + 1))
    for step_id in definition.steps:
        if step_id not in levels:  # unreachable, still drawn
            levels[step_id] = 0
            order.append(step_id)
    max_depth = max(levels.values(), default=0)
    for end_id in end_nodes.values():
        levels[end_id] = max_depth + 1
        order.append(end_id)

    rows: dict[int, int] = {}
    positions: dict[str, tuple[int, int, int, int]] = {}
    for node in order:
        depth = levels[node]
        row = rows.get(depth, 0)
        rows[depth] = row + 1
        is_event = node in end_nodes.values() or (
            definition.steps.get(node) is not None
            and definition.steps[node].kind in (StepKind.TRIGGER, StepKind.TIMER)
        )
        is_gateway = node in splits.values() or (
            definition.steps.get(node) is not None and definition.steps[node].is_join
        )
        if is_gateway:
            width, height = 50, 50
        else:
            width, height = _SIZES["event"] if is_event else _SIZES["task"]
        x = 160 + depth * 200
        y = 120 + row * 130 + (22 if is_event else 15 if is_gateway else 0)
        positions[node] = (x, y, width, height)
    # Boundary deadline events sit on the bottom-right corner of their gate.
    for step in definition.steps.values():
        if step.kind is StepKind.HUMAN_GATE and "deadline_seconds" in step.config:
            x, y, w, h = positions[step.id]
            positions[f"{step.id}_deadline"] = (x + w - 18, y + h - 18, 36, 36)

    diagram = ET.SubElement(root, f"{{{BPMNDI_NS}}}BPMNDiagram", {"id": f"diagram_{definition.id}"})
    plane = ET.SubElement(
        diagram,
        f"{{{BPMNDI_NS}}}BPMNPlane",
        {"id": f"plane_{definition.id}", "bpmnElement": definition.id},
    )
    for node, (x, y, w, h) in positions.items():
        shape = ET.SubElement(
            plane, f"{{{BPMNDI_NS}}}BPMNShape", {"id": f"shape_{node}", "bpmnElement": node}
        )
        ET.SubElement(
            shape,
            f"{{{DC_NS}}}Bounds",
            {"x": str(x), "y": str(y), "width": str(w), "height": str(h)},
        )
    def edge(flow_id: str, source_id: str, target_id: str) -> None:
        sx, sy, sw, sh = positions[source_id]
        tx, ty, _tw, th = positions[target_id]
        el = ET.SubElement(
            plane, f"{{{BPMNDI_NS}}}BPMNEdge", {"id": f"edge_{flow_id}", "bpmnElement": flow_id}
        )
        ET.SubElement(el, f"{{{DI_NS}}}waypoint", {"x": str(sx + sw), "y": str(sy + sh // 2)})
        ET.SubElement(el, f"{{{DI_NS}}}waypoint", {"x": str(tx), "y": str(ty + th // 2)})

    for step in definition.steps.values():
        for outcome, target in step.next.items():
            if (step.id, outcome) in splits:
                split_id = splits[(step.id, outcome)]
                edge(f"flow_{step.id}_{outcome}", step.id, split_id)
                for head in step.forks[outcome]:
                    edge(f"flow_{split_id}_{head}", split_id, head)
                continue
            target_id = (
                end_nodes[target]
                if (
                    target is END or (isinstance(target, str) and target.startswith(PROCESS_PREFIX))
                )
                else target
            )
            edge(f"flow_{step.id}_{outcome}", step.id, target_id)


def load_processes(
    directory: str | Path,
    briefings: dict[str, aimn.Briefing] | None = None,
    skills: dict[str, aimn.Briefing] | None = None,
) -> dict[str, ProcessDefinition]:
    """Every ``*.bpmn`` in ``directory``, keyed by process id."""
    out: dict[str, ProcessDefinition] = {}
    directory = Path(directory)
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.bpmn")):
        definition = load_bpmn(path, briefings, skills)
        if definition.id in out:
            raise BpmnError(f"{path}: duplicate process id {definition.id!r}")
        out[definition.id] = definition
    return out


__all__ = [
    "AIMN_NS",
    "BPMN_NS",
    "BpmnError",
    "DefinitionError",
    "load_bpmn",
    "load_processes",
    "parse_bpmn",
    "to_bpmn",
]


def ensure_diagram(text: str) -> str:
    """Return the XML with a ``bpmndi`` section added when it has none -
    a hand-written profile file renders in bpmn-js like a migrated one.
    Layout: layered left-to-right over the flow nodes, gateways included.
    The file itself is untouched; the workshop decides what to save."""
    if "BPMNDiagram" in text:
        return text
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return text  # never expand entities, even for display
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return text
    process = root.find(_b("process"))
    if process is None:
        return text
    sizes = {
        "startEvent": (36, 36),
        "endEvent": (36, 36),
        "intermediateCatchEvent": (36, 36),
        "boundaryEvent": (36, 36),
        "exclusiveGateway": (50, 50),
        "parallelGateway": (50, 50),
    }
    nodes: dict[str, str] = {}
    flows: list[tuple[str, str, str]] = []
    attached: dict[str, str] = {}
    for child in process:
        local = child.tag[len(BPMN_NS) + 2 :]
        node_id = child.get("id", "")
        if local == "sequenceFlow":
            flows.append((node_id, child.get("sourceRef", ""), child.get("targetRef", "")))
        elif local in ("extensionElements", "documentation"):
            continue
        elif local == "boundaryEvent":
            attached[node_id] = child.get("attachedToRef", "")
        else:
            nodes[node_id] = local
    outgoing: dict[str, list[str]] = {n: [] for n in nodes}
    for _fid, src, dst in flows:
        if src in outgoing and dst in nodes:
            outgoing[src].append(dst)
    starts = [n for n, local in nodes.items() if local == "startEvent"] or list(nodes)[:1]
    levels: dict[str, int] = {}
    queue = deque((s, 0) for s in starts)
    order: list[str] = []
    while queue:
        node, depth = queue.popleft()
        if node in levels:
            continue
        levels[node] = depth
        order.append(node)
        for nxt in outgoing.get(node, []):
            queue.append((nxt, depth + 1))
    for node in nodes:
        if node not in levels:
            levels[node] = 0
            order.append(node)
    rows: dict[int, int] = {}
    positions: dict[str, tuple[int, int, int, int]] = {}
    for node in order:
        depth = levels[node]
        row = rows.get(depth, 0)
        rows[depth] = row + 1
        width, height = sizes.get(nodes[node], (100, 80))
        x = 160 + depth * 180
        y = 120 + row * 130 + (80 - height) // 2
        positions[node] = (x, y, width, height)
    for boundary, host in attached.items():
        if host in positions:
            x, y, w, h = positions[host]
            positions[boundary] = (x + w - 18, y + h - 18, 36, 36)
    diagram = ET.SubElement(
        root, f"{{{BPMNDI_NS}}}BPMNDiagram", {"id": f"diagram_{process.get('id')}"}
    )
    plane = ET.SubElement(
        diagram,
        f"{{{BPMNDI_NS}}}BPMNPlane",
        {"id": f"plane_{process.get('id')}", "bpmnElement": process.get("id", "")},
    )
    for node, (x, y, w, h) in positions.items():
        shape = ET.SubElement(
            plane, f"{{{BPMNDI_NS}}}BPMNShape", {"id": f"shape_{node}", "bpmnElement": node}
        )
        if nodes.get(node) == "exclusiveGateway":
            shape.set("isMarkerVisible", "true")
        ET.SubElement(
            shape,
            f"{{{DC_NS}}}Bounds",
            {"x": str(x), "y": str(y), "width": str(w), "height": str(h)},
        )
    for fid, src, dst in flows:
        if src not in positions or dst not in positions:
            continue
        sx, sy, sw, sh = positions[src]
        tx, ty, _tw, th = positions[dst]
        edge = ET.SubElement(
            plane, f"{{{BPMNDI_NS}}}BPMNEdge", {"id": f"edge_{fid}", "bpmnElement": fid}
        )
        ET.SubElement(edge, f"{{{DI_NS}}}waypoint", {"x": str(sx + sw), "y": str(sy + sh // 2)})
        ET.SubElement(edge, f"{{{DI_NS}}}waypoint", {"x": str(tx), "y": str(ty + th // 2)})
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
