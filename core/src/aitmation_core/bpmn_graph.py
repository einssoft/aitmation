"""The card canvas's view of a process (ADR-0040): a graph of nodes and
edges read from the BPMN file and written back onto it.

The file stays the truth. ``read_graph`` lists the flow nodes with their
kind (the profile of ADR-0034), label, position (``bpmndi``) and the
reference that binds them (briefing, decision, tool action, called
process, chain target). ``apply_graph`` takes such a graph back and
changes the XML *structurally*: nodes that stay keep every extension
element they had, removed nodes go with their boundary events, new nodes
get the smallest valid skeleton of their kind, sequence flows and the
diagram are rewritten from the graph. Nothing else in the file moves.
"""

from __future__ import annotations

import contextlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .bpmn import (
    BPMN_NS,
    BPMNDI_NS,
    DC_NS,
    DI_NS,
    BpmnError,
    _a,
    _b,
    _duration_seconds,
    _iso_duration,
    dict_to_props,
    ensure_diagram,
    props_to_dict,
)

KINDS = ("start", "ai", "gate", "action", "decision", "call", "timer", "xor", "parallel", "end")
TAG_OF = {
    "start": "startEvent",
    "ai": "task",
    "gate": "userTask",
    "action": "serviceTask",
    "decision": "businessRuleTask",
    "call": "callActivity",
    "timer": "intermediateCatchEvent",
    "xor": "exclusiveGateway",
    "parallel": "parallelGateway",
    "end": "endEvent",
}
KIND_OF = {tag: kind for kind, tag in TAG_OF.items()}
SIZES = {
    "start": (36, 36),
    "end": (36, 36),
    "timer": (36, 36),
    "xor": (50, 50),
    "parallel": (50, 50),
}
DEFAULT_SIZE = (100, 80)
# The outcome a new edge gets when its source has none yet: the profile's
# conventional first outcome per kind (ADR-0034 §1).
DEFAULT_OUTCOMES = {
    "start": ("fired",),
    "ai": ("done", "failed"),
    "gate": ("approved", "rejected"),
    "action": ("done", "failed"),
    "decision": ("done",),
    "call": ("done", "failed"),
    "timer": ("elapsed",),
}
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_REF = re.compile(r"^[a-z0-9][a-z0-9_.:-]*$")
# The designer's settings of a step (ADR-0062): which extension element
# holds them, which of its attributes are settings (the rest of the
# settings are aimn:property children) - the binding attribute (action,
# ref, source) stays the card's ``ref``/``source``.
CONFIG_TAG = {
    "start": "trigger",
    "gate": "gate",
    "action": "toolCall",
    "decision": "decision",
    "call": "call",
    "timer": "timer",
}
CONFIG_ATTRS = {
    "start": ("channel",),
    "gate": ("approving", "kind", "rule"),
    "action": ("mandate", "exec", "effect"),
}
LOOP_KINDS = ("ai", "action")
_LOOP_ATTRS = ("over", "as", "collect", "max")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
MAX_CONFIG_CHARS = 20_000


class GraphError(ValueError):
    pass


@dataclass
class Node:
    id: str
    kind: str
    label: str = ""
    x: int = 0
    y: int = 0
    w: int = 100
    h: int = 80
    ref: str = ""  # briefing | decision | action | process | chain target
    source: str = ""  # trigger source (start)
    seconds: int | None = None  # timer
    deadline: int | None = None  # gate: boundary timer, read-only here
    extras: dict = field(default_factory=dict)

    labels: dict[str, str] | None = None  # aimn:name per language (ADR-0022)
    config: dict | None = None  # settings of the step (ADR-0062)
    loop: dict | None = None  # multi-instance: over/as/collect/max (ADR-0037)

    def to_dict(self) -> dict:
        out = {
            "id": self.id, "kind": self.kind, "label": self.label,
            **({"labels": self.labels} if self.labels else {}),
            "x": self.x, "y": self.y, "w": self.w, "h": self.h,
        }
        if self.ref:
            out["ref"] = self.ref
        if self.source:
            out["source"] = self.source
        if self.seconds is not None:
            out["seconds"] = self.seconds
        if self.deadline is not None:
            out["deadline"] = self.deadline
        if self.config:
            out["config"] = self.config
        if self.loop:
            out["loop"] = self.loop
        out.update(self.extras)
        return out


@dataclass
class Edge:
    id: str
    source: str
    target: str
    outcome: str = ""
    when: dict | None = None  # conditions of a deciding gateway's flow (ADR-0056)
    default: bool = False  # the "otherwise" flow of a deciding gateway

    def to_dict(self) -> dict:
        out = {
            "id": self.id, "source": self.source, "target": self.target, "outcome": self.outcome
        }
        if self.when:
            out["when"] = self.when
        if self.default:
            out["default"] = True
        return out


def _names_of(element: ET.Element) -> dict[str, str]:
    """``<aimn:name lang="fr">…</aimn:name>`` in an element's extensions:
    the element's name per language; ``name`` itself is the German one."""
    ext = element.find(_b("extensionElements"))
    if ext is None:
        return {}
    return {
        n.get("lang", ""): (n.text or "").strip()
        for n in ext.findall(_a("name"))
        if n.get("lang") and (n.text or "").strip()
    }


def localize(graph: dict, lang: str) -> dict:
    """The graph with labels in ``lang`` where the file has them."""
    for node in graph.get("nodes", []):
        text = (node.get("labels") or {}).get(lang)
        if text:
            node["label"] = text
    return graph


def _when_of(flow: ET.Element) -> dict | None:
    ext = flow.find(_b("extensionElements"))
    if ext is None:
        return None
    holder = ext.find(_a("when"))
    match = "all"
    if holder is not None:
        match = holder.get("match") or "all"
    else:
        holder = ext
    conditions = [
        {"input": c.get("input", ""), "test": c.get("test", "")}
        for c in holder.findall(_a("condition"))
    ]
    return {"match": match, "conditions": conditions} if conditions else None


def _write_when(flow: ET.Element, when: dict) -> None:
    match = str(when.get("match") or "all")
    if match not in ("all", "any"):
        raise GraphError(f"flow {flow.get('id')!r}: match must be all or any")
    conditions = when.get("conditions") or []
    if not isinstance(conditions, list):
        raise GraphError(f"flow {flow.get('id')!r}: conditions must be a list")
    if not conditions:
        return
    ext = ET.SubElement(flow, _b("extensionElements"))
    holder = ET.SubElement(ext, _a("when"), {"match": match}) if match == "any" else ext
    for cond in conditions:
        if not isinstance(cond, dict):
            raise GraphError(f"flow {flow.get('id')!r}: a condition is input + test")
        path, test = str(cond.get("input", "")).strip(), str(cond.get("test", "")).strip()
        if not path or not test or len(path) > 200 or len(test) > 200:
            raise GraphError(f"flow {flow.get('id')!r}: a condition needs input and test")
        ET.SubElement(holder, _a("condition"), {"input": path, "test": test})


def _local(element: ET.Element) -> str:
    return element.tag[len(BPMN_NS) + 2 :] if element.tag.startswith("{" + BPMN_NS) else ""


def _parse(text: str) -> tuple[ET.Element, ET.Element]:
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise GraphError("DOCTYPE and entities are not allowed")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise GraphError(f"not well-formed: {exc}") from exc
    process = root.find(_b("process"))
    if process is None:
        raise GraphError("no bpmn:process")
    return root, process


def _ext(element: ET.Element, tag: str) -> ET.Element | None:
    ext = element.find(_b("extensionElements"))
    return None if ext is None else ext.find(_a(tag))


def _ref_of(kind: str, element: ET.Element) -> str:
    if kind == "ai":
        task = _ext(element, "aiTask")
        return (task.get("ref") or "inline") if task is not None else ""
    if kind == "decision":
        decision = _ext(element, "decision")
        return decision.get("ref", "") if decision is not None else ""
    if kind == "action":
        call = _ext(element, "toolCall")
        return call.get("action", "") if call is not None else ""
    if kind == "call":
        return element.get("calledElement", "")
    if kind == "end":
        chain = _ext(element, "chainTo")
        return chain.get("process", "") if chain is not None else ""
    return ""


def _config_of(kind: str, element: ET.Element) -> dict:
    """A step's settings as one flat table: the setting attributes of its
    extension element plus its aimn:property children."""
    tag = CONFIG_TAG.get(kind)
    holder = _ext(element, tag) if tag else None
    if holder is None:
        return {}
    out: dict = {}
    for name in CONFIG_ATTRS.get(kind, ()):
        value = holder.get(name)
        if value is not None:
            out[name] = (value == "true") if name == "mandate" else value
    try:
        out.update(props_to_dict(holder))
    except BpmnError as exc:
        raise GraphError(f"{element.get('id')!r}: {exc}") from exc
    return out


def _loop_of(element: ET.Element) -> dict:
    loop = element.find(_b("multiInstanceLoopCharacteristics"))
    if loop is None:
        return {}
    out: dict = {}
    for name in _LOOP_ATTRS:
        value = loop.get(_a(name))
        if value is not None:
            out[name] = int(value) if name == "max" and value.isdigit() else value
    return out


def _check_value(owner: str, value, depth: int = 0) -> None:
    if isinstance(value, bool | int | float):
        return
    if isinstance(value, str):
        if len(value) > 4000:
            raise GraphError(f"{owner}: a setting is longer than 4000 characters")
        return
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, str | int | float | bool):
                raise GraphError(f"{owner}: list settings hold plain values only")
            _check_value(owner, item, depth + 1)
        return
    if isinstance(value, dict):
        if depth >= 3:
            raise GraphError(f"{owner}: settings nest at most three levels")
        for key, inner in value.items():
            if not isinstance(key, str) or not _KEY.match(key):
                raise GraphError(f"{owner}: setting name {key!r} is not allowed")
            _check_value(owner, inner, depth + 1)
        return
    raise GraphError(f"{owner}: unsupported setting value {type(value).__name__}")


def _write_config(kind: str, element: ET.Element, config: dict) -> None:
    owner = f"step {element.get('id')!r}"
    tag = CONFIG_TAG.get(kind)
    if not isinstance(config, dict):
        raise GraphError(f"{owner}: config must be an object")
    if tag is None:
        if config:
            raise GraphError(f"{owner}: a {kind} card has no settings")
        return
    if len(repr(config)) > MAX_CONFIG_CHARS:
        raise GraphError(f"{owner}: settings too large")
    _check_value(owner, config)
    holder = _ext(element, tag)
    if holder is None:
        ext = element.find(_b("extensionElements"))
        if ext is None:
            ext = ET.Element(_b("extensionElements"))
            element.insert(0, ext)
        holder = ET.SubElement(ext, _a(tag))
    attrs = CONFIG_ATTRS.get(kind, ())
    for name in attrs:
        value = config.get(name)
        if value in (None, "", False):
            holder.attrib.pop(name, None)
        else:
            holder.set(name, "true" if value is True else str(value))
    for prop in holder.findall(_a("property")):
        holder.remove(prop)
    dict_to_props(holder, {k: v for k, v in config.items() if k not in attrs})
    if kind in ("timer", "call") and not len(holder) and not holder.attrib:
        ext = element.find(_b("extensionElements"))
        if ext is not None:
            ext.remove(holder)
            if not len(ext):
                element.remove(ext)


def _write_loop(element: ET.Element, loop: dict | None) -> None:
    owner = f"step {element.get('id')!r}"
    old = element.find(_b("multiInstanceLoopCharacteristics"))
    if old is not None:
        element.remove(old)
    if not loop:
        return
    if not isinstance(loop, dict) or not loop.get("over") or not loop.get("as"):
        raise GraphError(f"{owner}: 'for each' needs over (a list) and as (a name)")
    attrs = {"isSequential": "false"}
    for name in _LOOP_ATTRS:
        value = loop.get(name)
        if value in (None, ""):
            continue
        text = str(value)
        if len(text) > 200 or (name == "max" and not text.isdigit()):
            raise GraphError(f"{owner}: invalid {name} {text!r}")
        attrs[_a(name)] = text
    ET.SubElement(element, _b("multiInstanceLoopCharacteristics"), attrs)


def _write_deadline(
    process: ET.Element, gate: ET.Element, seconds, boundaries: dict[str, list[ET.Element]]
) -> None:
    """A gate's reminder deadline is a non-interrupting boundary timer."""
    gate_id = gate.get("id", "")
    timers = [
        b for b in boundaries.get(gate_id, []) if b.find(_b("timerEventDefinition")) is not None
    ]
    if not seconds:
        for boundary in timers:
            process.remove(boundary)
            boundaries[gate_id].remove(boundary)
        return
    try:
        seconds = int(seconds)
    except (TypeError, ValueError) as exc:
        raise GraphError(f"gate {gate_id!r}: deadline must be seconds") from exc
    if seconds < 60:
        raise GraphError(f"gate {gate_id!r}: a deadline is at least a minute")
    if timers:
        duration = timers[0].find(_b("timerEventDefinition")).find(_b("timeDuration"))
        if duration is not None:
            duration.text = _iso_duration(seconds)
            return
    boundary = ET.Element(
        _b("boundaryEvent"),
        {"id": f"{gate_id}_deadline", "attachedToRef": gate_id, "cancelActivity": "false"},
    )
    timer = ET.SubElement(boundary, _b("timerEventDefinition"))
    ET.SubElement(timer, _b("timeDuration")).text = _iso_duration(seconds)
    process.insert(list(process).index(gate) + 1, boundary)
    boundaries.setdefault(gate_id, []).append(boundary)


def _contract_of(process: ET.Element) -> list[dict]:
    ext = process.find(_b("extensionElements"))
    contract = ext.find(_a("contract")) if ext is not None else None
    if contract is None:
        return []
    out = []
    for node in contract.findall(_a("input")):
        entry = {"name": node.get("name", ""), "description": (node.text or "").strip()}
        if "default" in node.attrib:
            entry["default"] = node.get("default", "")
        out.append(entry)
    return out


def _write_contract(process: ET.Element, inputs: list) -> None:
    """The start form's fields (contract inputs); outputs stay as they are."""
    if not isinstance(inputs, list) or len(inputs) > 40:
        raise GraphError("contract: a list of at most 40 inputs")
    ext = process.find(_b("extensionElements"))
    if ext is None:
        ext = ET.Element(_b("extensionElements"))
        process.insert(0, ext)
    contract = ext.find(_a("contract"))
    if contract is None:
        contract = ET.SubElement(ext, _a("contract"))
    for old in contract.findall(_a("input")):
        contract.remove(old)
    seen: set[str] = set()
    for position, raw in enumerate(inputs):
        if not isinstance(raw, dict):
            raise GraphError("contract: each input is name + description")
        name = str(raw.get("name", "")).strip()
        if not _ID.match(name) or name in seen:
            raise GraphError(f"contract: input name {name!r} is invalid or repeated")
        seen.add(name)
        attrs = {"name": name}
        if raw.get("default") is not None:
            attrs["default"] = str(raw["default"])[:2000]
        element = ET.Element(_a("input"), attrs)
        element.text = str(raw.get("description", "") or "")[:500]
        contract.insert(position, element)
    if not len(contract):
        ext.remove(contract)


def read_graph(text: str) -> dict:
    """Nodes, edges and the process id - with positions from the diagram
    (a layout is computed first when the file has none)."""
    text = ensure_diagram(text)
    root, process = _parse(text)
    bounds: dict[str, tuple[int, int, int, int]] = {}
    for shape in root.iter(f"{{{BPMNDI_NS}}}BPMNShape"):
        box = shape.find(f"{{{DC_NS}}}Bounds")
        if box is not None and shape.get("bpmnElement"):
            bounds[shape.get("bpmnElement", "")] = tuple(  # type: ignore[assignment]
                int(float(box.get(k, "0"))) for k in ("x", "y", "width", "height")
            )
    nodes: list[Node] = []
    edges: list[Edge] = []
    deadlines: dict[str, int] = {}
    defaults = {
        child.get("default", ""): child.get("id", "")
        for child in process
        if _local(child) == "exclusiveGateway" and child.get("default")
    }
    for child in process:
        local = _local(child)
        if local == "boundaryEvent":
            timer = child.find(_b("timerEventDefinition"))
            duration = timer.find(_b("timeDuration")) if timer is not None else None
            if duration is not None and duration.text:
                with contextlib.suppress(Exception):  # the loader reports a bad duration
                    deadlines[child.get("attachedToRef", "")] = _duration_seconds(
                        duration.text.strip(), "graph"
                    )
            continue
        if local == "sequenceFlow":
            edge = Edge(
                child.get("id", ""), child.get("sourceRef", ""), child.get("targetRef", ""),
                child.get("name", "") or "", when=_when_of(child),
            )
            edge.default = defaults.get(edge.id) == edge.source
            edges.append(edge)
            continue
        kind = KIND_OF.get(local)
        if kind is None:
            continue
        node_id = child.get("id", "")
        x, y, w, h = bounds.get(node_id, (0, 0, *SIZES.get(kind, DEFAULT_SIZE)))
        label = child.get("name", "") or ""
        node = Node(node_id, kind, label, x, y, w, h, ref=_ref_of(kind, child))
        names = _names_of(child)
        if names:
            node.labels = names
        if kind == "start":
            trigger = _ext(child, "trigger")
            node.source = trigger.get("source", "") if trigger is not None else ""
            if trigger is not None and trigger.get("channel"):
                node.extras["channel"] = trigger.get("channel")  # ADR-0057
        if kind == "timer":
            timer = child.find(_b("timerEventDefinition"))
            duration = timer.find(_b("timeDuration")) if timer is not None else None
            if duration is not None and duration.text:
                with contextlib.suppress(Exception):
                    node.seconds = _duration_seconds(duration.text.strip(), "graph")
        if kind == "ai":
            loop = child.find(_b("multiInstanceLoopCharacteristics"))
            if loop is not None:
                node.extras["for_each"] = loop.get(_a("over"), "")
        node.config = _config_of(kind, child) or None
        if kind in LOOP_KINDS:
            node.loop = _loop_of(child) or None
        nodes.append(node)
    for node in nodes:
        if node.kind == "gate" and node.id in deadlines:
            node.deadline = deadlines[node.id]
    return {
        "process": process.get("id", ""),
        "nodes": [n.to_dict() for n in nodes],
        "edges": [e.to_dict() for e in edges],
        "contract": _contract_of(process),
    }


def _node_from(raw: dict) -> Node:
    node_id = str(raw.get("id", ""))
    if not _ID.match(node_id):
        raise GraphError(f"node id {node_id!r}: lowercase letters, digits and _ only")
    kind = str(raw.get("kind", ""))
    if kind not in KINDS:
        raise GraphError(f"node {node_id!r}: unknown kind {kind!r}")
    ref = str(raw.get("ref", "") or "")
    if ref and not _REF.match(ref):
        raise GraphError(f"node {node_id!r}: invalid reference {ref!r}")
    w, h = SIZES.get(kind, DEFAULT_SIZE)
    node = Node(
        node_id, kind, str(raw.get("label", "") or "")[:120],
        int(raw.get("x", 0) or 0), int(raw.get("y", 0) or 0),
        int(raw.get("w", w) or w), int(raw.get("h", h) or h), ref=ref,
    )
    node.source = str(raw.get("source", "") or "")
    seconds = raw.get("seconds")
    node.seconds = int(seconds) if seconds not in (None, "") else None
    return node


def _skeleton(process: ET.Element, node: Node, anchor: int) -> ET.Element:
    """The smallest valid element of a kind, inserted before the flows."""
    tag = TAG_OF[node.kind]
    attrs = {"id": node.id}
    if node.kind not in ("xor", "parallel", "end") or node.label:
        attrs["name"] = node.label or node.id
    element = ET.Element(_b(tag), attrs)
    process.insert(anchor, element)

    def ext() -> ET.Element:
        return ET.SubElement(element, _b("extensionElements"))

    if node.kind == "start":
        ET.SubElement(ext(), _a("trigger"), {"source": node.source or "manual"})
    elif node.kind == "ai":
        if not node.ref:
            raise GraphError(f"AI step {node.id!r} needs a briefing (ref)")
        ET.SubElement(ext(), _a("aiTask"), {"ref": node.ref})
    elif node.kind == "gate":
        ET.SubElement(ext(), _a("gate"))
    elif node.kind == "action":
        if not node.ref:
            raise GraphError(f"action {node.id!r} needs an action or tool (ref)")
        ET.SubElement(ext(), _a("toolCall"), {"action": node.ref})
    elif node.kind == "decision":
        if not node.ref:
            raise GraphError(f"decision {node.id!r} needs a decision table (ref)")
        ET.SubElement(ext(), _a("decision"), {"ref": node.ref})
    elif node.kind == "call":
        if not node.ref:
            raise GraphError(f"call {node.id!r} needs a process (ref)")
        element.set("calledElement", node.ref)
    elif node.kind == "timer":
        timer = ET.SubElement(element, _b("timerEventDefinition"))
        ET.SubElement(timer, _b("timeDuration")).text = _iso_duration(
            int(node.seconds or 3600)
        )
    elif node.kind == "end" and node.ref:
        ET.SubElement(ext(), _a("chainTo"), {"process": node.ref})
    return element


def _update(element: ET.Element, node: Node) -> None:
    """Apply the editable facts of the canvas to an existing element -
    label and binding only; every other extension stays as it is."""
    if node.kind in ("xor", "parallel", "end"):
        if node.label:
            element.set("name", node.label)
        elif "name" in element.attrib:
            del element.attrib["name"]
    else:
        element.set("name", node.label or node.id)
    if node.kind == "ai" and node.ref and node.ref != "inline":
        task = _ext(element, "aiTask")
        if task is not None and task.get("ref") != node.ref:
            task.set("ref", node.ref)
            for child in list(task):  # an inline briefing gives way to the reference
                task.remove(child)
    elif node.kind == "decision" and node.ref:
        decision = _ext(element, "decision")
        if decision is not None:
            decision.set("ref", node.ref)
    elif node.kind == "action" and node.ref:
        call = _ext(element, "toolCall")
        if call is not None:
            call.set("action", node.ref)
    elif node.kind == "call" and node.ref:
        element.set("calledElement", node.ref)
    elif node.kind == "start" and node.source:
        trigger = _ext(element, "trigger")
        if trigger is not None:
            trigger.set("source", node.source)
    elif node.kind == "timer" and node.seconds:
        timer = element.find(_b("timerEventDefinition"))
        duration = timer.find(_b("timeDuration")) if timer is not None else None
        if duration is not None:
            duration.text = _iso_duration(int(node.seconds))
    elif node.kind == "end":
        chain = _ext(element, "chainTo")
        ext = element.find(_b("extensionElements"))
        if node.ref:
            if chain is None:
                if ext is None:
                    ext = ET.SubElement(element, _b("extensionElements"))
                chain = ET.SubElement(ext, _a("chainTo"))
            chain.set("process", node.ref)
        elif chain is not None and ext is not None:
            ext.remove(chain)
            if len(ext) == 0:
                element.remove(ext)


def apply_graph(text: str, graph: dict) -> str:
    """The graph written back onto the file: structure and diagram from
    the graph, everything else from the file."""
    root, process = _parse(text)
    raw_nodes = graph.get("nodes") or []
    raw_edges = graph.get("edges") or []
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise GraphError("nodes and edges must be lists")
    nodes = [_node_from(n) for n in raw_nodes]
    seen: set[str] = set()
    for node in nodes:
        if node.id in seen:
            raise GraphError(f"node id {node.id!r} appears twice")
        seen.add(node.id)
    if not any(n.kind == "start" for n in nodes):
        raise GraphError("a process needs a start")

    existing: dict[str, ET.Element] = {}
    boundaries: dict[str, list[ET.Element]] = {}
    flows: list[ET.Element] = []
    for child in list(process):
        local = _local(child)
        if local == "sequenceFlow":
            flows.append(child)
        elif local == "boundaryEvent":
            boundaries.setdefault(child.get("attachedToRef", ""), []).append(child)
        elif local in KIND_OF:
            existing[child.get("id", "")] = child
    # Removed nodes leave with their boundary events; kind changes are a
    # remove-and-add (the old extensions would not fit the new kind).
    for node_id, element in list(existing.items()):
        keep = next((n for n in nodes if n.id == node_id), None)
        if keep is None or KIND_OF[_local(element)] != keep.kind:
            process.remove(element)
            for boundary in boundaries.pop(node_id, []):
                process.remove(boundary)
            del existing[node_id]
    old_flows: dict[tuple[str, str], ET.Element] = {
        (f.get("sourceRef", ""), f.get("name", "") or ""): f for f in flows
    }
    old_defaults = {
        child.get("default", "")
        for child in process
        if _local(child) == "exclusiveGateway" and child.get("default")
    }
    for flow in flows:
        process.remove(flow)
    # Updates and additions, in graph order; new elements go before the
    # (re-created) flows, after the last existing flow node.
    anchor = len(process)
    by_id = {str(raw.get("id", "")): raw for raw in raw_nodes}
    for node in nodes:
        element = existing.get(node.id)
        if element is not None and KIND_OF[_local(element)] == node.kind:
            _update(element, node)
        else:
            element = _skeleton(process, node, anchor)
            anchor += 1
        raw = by_id.get(node.id, {})
        # Settings travel only when the graph carries them, and are only
        # rewritten when they changed - an unchanged step keeps its file text.
        if "config" in raw and (raw.get("config") or {}) != _config_of(node.kind, element):
            _write_config(node.kind, element, raw.get("config") or {})
        loop = raw.get("loop") or {}
        if node.kind in LOOP_KINDS and "loop" in raw and loop != _loop_of(element):
            _write_loop(element, loop)
        if node.kind == "gate" and "deadline" in raw:
            _write_deadline(process, element, raw.get("deadline"), boundaries)
    if "contract" in graph and (graph.get("contract") or []) != _contract_of(process):
        _write_contract(process, graph.get("contract") or [])
    # Flows: ids from source and outcome; unnamed flows are allowed into
    # gateways and out of parallel ones, the loader checks the rest.
    kinds = {n.id: n.kind for n in nodes}
    used: set[str] = set()
    edges: list[Edge] = []
    for raw in raw_edges:
        source, target = str(raw.get("source", "")), str(raw.get("target", ""))
        if source not in kinds or target not in kinds:
            raise GraphError(f"edge {source!r} -> {target!r} names an unknown node")
        outcome = str(raw.get("outcome", "") or "").strip()
        if outcome and not _ID.match(outcome):
            raise GraphError(f"outcome {outcome!r}: lowercase letters, digits and _ only")
        if kinds[source] == "end":
            raise GraphError("an end has no outgoing flow")
        base = f"flow_{source}_{outcome}" if outcome else f"flow_{source}_to_{target}"
        flow_id, n = base, 2
        while flow_id in used or flow_id in kinds:
            flow_id, n = f"{base}_{n}", n + 1
        used.add(flow_id)
        edge = Edge(flow_id, source, target, outcome)
        old = old_flows.get((source, outcome))
        if "when" in raw:
            edge.when = raw.get("when") or None
            if edge.when is not None and not isinstance(edge.when, dict):
                raise GraphError(f"edge {source!r} -> {target!r}: when must be an object")
        elif old is not None:
            edge.when = _when_of(old)  # conditions stay with the flow they were on
        if "default" in raw:
            edge.default = bool(raw.get("default"))
        else:
            edge.default = old is not None and old.get("id", "") in old_defaults
        edges.append(edge)
    for edge in edges:
        attrs = {"id": edge.id}
        if edge.outcome:
            attrs["name"] = edge.outcome
        attrs.update({"sourceRef": edge.source, "targetRef": edge.target})
        flow = ET.SubElement(process, _b("sequenceFlow"), attrs)
        if edge.when:
            _write_when(flow, edge.when)
    # The default flow of each deciding gateway (ADR-0056), by the new ids.
    elements = {child.get("id", ""): child for child in process if _local(child) in KIND_OF}
    for node in nodes:
        if node.kind != "xor":
            continue
        element = elements.get(node.id)
        if element is None:
            continue
        chosen = [e.id for e in edges if e.source == node.id and e.default]
        if len(chosen) > 1:
            raise GraphError(f"gateway {node.id!r}: only one branch can be 'otherwise'")
        if chosen:
            element.set("default", chosen[0])
        elif "default" in element.attrib:
            del element.attrib["default"]
    _write_diagram(root, process, nodes, edges, boundaries)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _write_diagram(
    root: ET.Element,
    process: ET.Element,
    nodes: list[Node],
    edges: list[Edge],
    boundaries: dict[str, list[ET.Element]],
) -> None:
    for old in root.findall(f"{{{BPMNDI_NS}}}BPMNDiagram"):
        root.remove(old)
    process_id = process.get("id", "")
    diagram = ET.SubElement(root, f"{{{BPMNDI_NS}}}BPMNDiagram", {"id": f"diagram_{process_id}"})
    plane = ET.SubElement(
        diagram, f"{{{BPMNDI_NS}}}BPMNPlane",
        {"id": f"plane_{process_id}", "bpmnElement": process_id},
    )
    boxes: dict[str, tuple[int, int, int, int]] = {}
    for node in nodes:
        boxes[node.id] = (node.x, node.y, node.w, node.h)
        shape = ET.SubElement(
            plane, f"{{{BPMNDI_NS}}}BPMNShape", {"id": f"shape_{node.id}", "bpmnElement": node.id}
        )
        if node.kind == "xor":
            shape.set("isMarkerVisible", "true")
        ET.SubElement(
            shape, f"{{{DC_NS}}}Bounds",
            {"x": str(node.x), "y": str(node.y), "width": str(node.w), "height": str(node.h)},
        )
        for boundary in boundaries.get(node.id, []):
            bid = boundary.get("id", "")
            bshape = ET.SubElement(
                plane, f"{{{BPMNDI_NS}}}BPMNShape", {"id": f"shape_{bid}", "bpmnElement": bid}
            )
            ET.SubElement(
                bshape, f"{{{DC_NS}}}Bounds",
                {"x": str(node.x + node.w - 18), "y": str(node.y + node.h - 18),
                 "width": "36", "height": "36"},
            )
    for edge in edges:
        sx, sy, sw, sh = boxes[edge.source]
        tx, ty, _tw, th = boxes[edge.target]
        element = ET.SubElement(
            plane, f"{{{BPMNDI_NS}}}BPMNEdge", {"id": f"edge_{edge.id}", "bpmnElement": edge.id}
        )
        ET.SubElement(element, f"{{{DI_NS}}}waypoint", {"x": str(sx + sw), "y": str(sy + sh // 2)})
        ET.SubElement(element, f"{{{DI_NS}}}waypoint", {"x": str(tx), "y": str(ty + th // 2)})
