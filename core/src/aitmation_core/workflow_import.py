"""Workflows from other platforms as drafts (WORKFLOW-IMPORT-KONZEPT, ADR-0060).

An export of n8n (workflow JSON), Activepieces (flow JSON) or Make
(scenario blueprint) is *translated* into the BPMN profile, briefings and
tool declarations of a format-2 solution - no foreign runtime comes along,
no foreign code is copied. Every node ends up in a report as

* ``translated``   - equivalent (trigger, condition, AI call, mail, wait),
* ``approximated`` - runs, but differently (simplified expression, loop
  run once, parallel branches one after the other) - with a note,
* ``placeholder``  - not translatable (code, unknown integrations): a
  gate "rebuild by hand" that carries the original settings as a note.

Rules that are not up for negotiation (concept §2):

1. Every outside effect (HTTP that writes, mail) gets a gate in front and a
   ``failed`` path (ADR-0007/0041). Nothing from the import is mandated.
2. No credentials: the report names the tool secrets to set
   (``AITMATION_SECRET_<NAME>``); values never leave the export.
3. Hosts come from the declaration: a URL with an expression in its host
   becomes a placeholder, never a tool.
4. The export is data: parsed as JSON with a size cap, nothing executed.

The result is a set of files for ``Drafts`` (ADR-0039) - the workshop
checks and releases them like any other draft.
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

MAX_EXPORT_BYTES = 2 * 1024 * 1024
MODEL_ROLE = "imported"
SOURCES = ("n8n", "activepieces", "make", "zapier")

TRANSLATED, APPROXIMATED, PLACEHOLDER = "translated", "approximated", "placeholder"


class WorkflowImportError(ValueError):
    pass


@dataclass
class Branch:
    outcome: str
    conditions: list[tuple[str, str]]  # (input path, S-FEEL test)
    match: str = "all"
    target: str | None = None


@dataclass
class Step:
    id: str
    name: str
    kind: str  # start | ai | tool | mail | route | wait | gate | placeholder | end
    source_type: str = ""
    status: str = TRANSLATED
    note: str = ""
    cfg: dict = field(default_factory=dict)
    next: str | None = None  # the successor on success (None = finish)
    branches: list[Branch] = field(default_factory=list)  # route only
    default: Branch | None = None  # route only


@dataclass
class ReportItem:
    node: str
    source_type: str
    status: str
    step: str
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "node": self.node,
            "type": self.source_type,
            "status": self.status,
            "step": self.step,
            "note": self.note,
        }


@dataclass
class ImportResult:
    source: str
    process_id: str
    title: str
    files: dict[str, str]
    report: list[ReportItem]
    secrets: list[str]
    inputs: list[str]
    model_roles: list[str]

    def summary(self) -> dict:
        counts = {TRANSLATED: 0, APPROXIMATED: 0, PLACEHOLDER: 0}
        for item in self.report:
            counts[item.status] = counts.get(item.status, 0) + 1
        return {
            "source": self.source,
            "process": self.process_id,
            "title": self.title,
            "counts": counts,
            "items": [i.as_dict() for i in self.report],
            "secrets": [f"AITMATION_SECRET_{s.upper()}" for s in self.secrets],
            "inputs": self.inputs,
            "model_roles": self.model_roles,
        }


# -- helpers ---------------------------------------------------------------


def slug(text: str, fallback: str = "step") -> str:
    out = re.sub(
        r"[^a-z0-9]+",
        "_",
        (text or "")
        .lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss"),
    )
    out = out.strip("_")[:40] or fallback
    return out if out[0].isalpha() else f"s_{out}"


def _excerpt(value, limit: int = 600) -> str:
    """The original settings as a note - credentials and secrets removed."""

    def clean(v):
        if isinstance(v, dict):
            return {
                k: (
                    "[…]"
                    if re.search(r"cred|auth|token|secret|password|key", k, re.I)
                    else clean(x)
                )
                for k, x in v.items()
            }
        if isinstance(v, list):
            return [clean(x) for x in v[:20]]
        return v

    text = json.dumps(clean(value), ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + " …"


_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _path_fields(parts: list[str]) -> list[str]:
    return [p for p in parts if _FIELD.fullmatch(p)][:4]


class Builder:
    """Collects steps, inputs, secrets and the report while a translator
    walks the foreign graph; renders the solution files at the end."""

    def __init__(self, source: str, process_id: str, title: str) -> None:
        self.source = source
        self.process_id = process_id
        self.title = title
        self.steps: dict[str, Step] = {}
        self.order: list[str] = []
        self.report: list[ReportItem] = []
        self.secrets: list[str] = []
        self.inputs: list[str] = []
        self.briefings: dict[str, str] = {}
        self.tools: dict[str, str] = {}

    # ids and bookkeeping
    def new_id(self, name: str) -> str:
        base = slug(name)
        sid, n = base, 2
        while sid in self.steps or sid in ("finished", "failed", "stopped", "filtered"):
            sid, n = f"{base}_{n}", n + 1
        return sid

    def add(self, step: Step, node: str = "", note_status: str | None = None) -> Step:
        self.steps[step.id] = step
        self.order.append(step.id)
        if node or step.source_type:
            self.report.append(
                ReportItem(
                    node or step.name.removeprefix("Von Hand nachbauen: "),
                    step.source_type,
                    note_status or step.status,
                    step.id,
                    step.note,
                )
            )
        return step

    def set_status(self, step: Step, status: str, note: str) -> None:
        step.status, step.note = status, note
        for item in self.report:
            if item.step == step.id:
                item.status, item.note = status, note

    def skip(self, node: str, source_type: str, note: str, status: str = APPROXIMATED) -> None:
        self.report.append(ReportItem(node, source_type, status, "", note))

    def input(self, name: str) -> str:
        name = slug(name, "value")
        if name not in self.inputs:
            self.inputs.append(name)
        return name

    def secret(self, name: str) -> str:
        name = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:40] or "token"
        if not name[0].isalpha():
            name = f"s_{name}"
        if name not in self.secrets:
            self.secrets.append(name)
        return name

    # rendering -------------------------------------------------------------
    def files(self) -> dict[str, str]:
        out = {f"processes/{self.process_id}.bpmn": self._bpmn()}
        for bid, text in self.briefings.items():
            out[f"ai/{bid}.aimn.toml"] = text
        for tid, text in self.tools.items():
            out[f"tools/{tid}.toml"] = text
        return out

    def _bpmn(self) -> str:
        p = self.process_id
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"',
            '  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"',
            '  xmlns:dc="http://www.omg.org/spec/DD/20100524/DC"',
            '  xmlns:di="http://www.omg.org/spec/DD/20100524/DI"',
            f'  xmlns:aimn="https://aitmation.com/schema/aimn/1" id="defs_{p}"'
            ' targetNamespace="https://aitmation.com/solutions">',
            f"  <!-- Imported from {escape(self.source)} as a draft (ADR-0060):"
            " every outside effect",
            "       waits for a gate, placeholders are gates 'rebuild by hand'. -->",
            f'  <bpmn:process id="{p}" name={quoteattr(self.title)}'
            ' isExecutable="true" aimn:version="1">',
            "    <bpmn:extensionElements>",
            "      <aimn:meta>",
            f'        <aimn:title lang="de">{escape(self.title)}</aimn:title>',
            f'        <aimn:description lang="de">Importiert aus {escape(self.source)}'
            " - Entwurf, vor der Freigabe prüfen.</aimn:description>",
            "      </aimn:meta>",
        ]
        if self.inputs:
            lines.append("      <aimn:contract>")
            for name in self.inputs:
                lines.append(
                    f'        <aimn:input name="{name}" default="">'
                    "from the imported trigger</aimn:input>"
                )
            lines.append("      </aimn:contract>")
        lines.append("    </bpmn:extensionElements>")
        flows: list[str] = []
        used_ends: set[str] = set()
        counter = [0]

        def flow(src: str, dst: str | None, name: str = "", ext: str = "") -> None:
            target = dst or "finished"
            if dst is None:
                used_ends.add("finished")
            if target in ("failed", "stopped", "filtered"):
                used_ends.add(target)
            counter[0] += 1
            attr = f' name="{name}"' if name else ""
            if ext:
                flows.append(
                    f'    <bpmn:sequenceFlow id="f{counter[0]}"{attr} sourceRef="{src}"'
                    f' targetRef="{target}">\n      <bpmn:extensionElements>{ext}'
                    "</bpmn:extensionElements>\n    </bpmn:sequenceFlow>"
                )
            else:
                flows.append(
                    f'    <bpmn:sequenceFlow id="f{counter[0]}"{attr}'
                    f' sourceRef="{src}" targetRef="{target}"/>'
                )

        def into(step_id: str | None, outcome: str, src: str) -> None:
            """A flow from src (with its outcome name) - gateways of the
            routing kind take a named incoming flow too."""
            flow(src, step_id, outcome)

        for sid in self.order:
            s = self.steps[sid]
            doc = (
                f"\n      <bpmn:documentation>{escape(s.note)}</bpmn:documentation>"
                if s.note
                else ""
            )
            name = quoteattr(s.name[:80])
            if s.kind == "start":
                trig = s.cfg.get("trigger", "manual")
                if trig == "schedule":
                    props = "".join(
                        f'<aimn:property name="{k}" value={quoteattr(str(v))}/>'
                        for k, v in s.cfg.get("schedule", {}).items()
                    )
                    trigger = f'<aimn:trigger source="schedule">{props}</aimn:trigger>'
                else:
                    trigger = '<aimn:trigger source="manual"/>'
                lines.append(
                    f'    <bpmn:startEvent id="{sid}" name={name}><bpmn:extensionElements>'
                    f"{trigger}</bpmn:extensionElements>{doc}</bpmn:startEvent>"
                )
                flow(sid, s.next)
            elif s.kind == "ai":
                lines.append(
                    f'    <bpmn:task id="{sid}" name={name}><bpmn:extensionElements>'
                    f'<aimn:aiTask ref="{s.cfg["briefing"]}"/></bpmn:extensionElements>'
                    f"{doc}</bpmn:task>"
                )
                into(s.next, "done", sid)
                flow(sid, "failed", "failed")
            elif s.kind in ("tool", "mail"):
                action = "mail" if s.kind == "mail" else s.cfg["tool"]
                props = "".join(
                    f'<aimn:property name="{k}" value={quoteattr(v)}/>'
                    for k, v in s.cfg.get("inputs", {}).items()
                )
                inputs = f'<aimn:property name="inputs">{props}</aimn:property>' if props else ""
                lines.append(
                    f'    <bpmn:serviceTask id="{sid}" name={name}><bpmn:extensionElements>'
                    f'<aimn:toolCall action="{action}">{inputs}</aimn:toolCall>'
                    f"</bpmn:extensionElements>{doc}</bpmn:serviceTask>"
                )
                into(s.next, "done", sid)
                flow(sid, "failed", "failed")
            elif s.kind in ("gate", "placeholder"):
                approving = s.cfg.get("approving", "done")
                lines.append(
                    f'    <bpmn:userTask id="{sid}" name={name}><bpmn:extensionElements>'
                    f'<aimn:gate approving="{approving}"/></bpmn:extensionElements>'
                    f"{doc}</bpmn:userTask>"
                )
                into(s.next, approving, sid)
                if s.cfg.get("reject"):
                    flow(sid, "stopped", "rejected")
            elif s.kind == "wait":
                lines.append(
                    f'    <bpmn:intermediateCatchEvent id="{sid}" name={name}>'
                    "<bpmn:timerEventDefinition>"
                    f"<bpmn:timeDuration>PT{int(s.cfg['seconds'])}S</bpmn:timeDuration>"
                    f"</bpmn:timerEventDefinition>{doc}</bpmn:intermediateCatchEvent>"
                )
                flow(sid, s.next)
            elif s.kind == "route":
                # outcome names once each; the default flow is named on the
                # gateway, which also marks it as deciding by conditions
                d = s.default or Branch("otherwise", [], target=None)
                taken: set[str] = set()
                for b in [*s.branches, d]:
                    base, k = b.outcome, 2
                    while b.outcome in taken:
                        b.outcome, k = f"{base}_{k}", k + 1
                    taken.add(b.outcome)
                default_id = f"f{counter[0] + len(s.branches) + 1}"
                lines.append(
                    f'    <bpmn:exclusiveGateway id="{sid}" name={name} default="{default_id}">'
                    f"{doc}</bpmn:exclusiveGateway>"
                )
                for b in s.branches:
                    conds = "".join(
                        f'<aimn:condition input="{escape(i)}" test={quoteattr(t)}/>'
                        for i, t in b.conditions
                    )
                    ext = (
                        f'<aimn:when match="any">{conds}</aimn:when>' if b.match == "any" else conds
                    )
                    flow(sid, b.target, b.outcome, ext)
                flow(sid, d.target, d.outcome)
        for end, label in (
            ("finished", "fertig"),
            ("failed", "fehlgeschlagen"),
            ("stopped", "abgelehnt"),
            ("filtered", "ausgefiltert"),
        ):
            if end in used_ends:
                lines.append(f'    <bpmn:endEvent id="{end}" name="{label}"/>')
        lines.extend(flows)
        lines.append("  </bpmn:process>")
        lines.append("</bpmn:definitions>")
        return "\n".join(lines) + "\n"


# -- pieces every translator uses -------------------------------------------


def _int(value, default: int) -> int:
    """A number from an export - an expression or junk gives the default."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def add_ai(
    b: Builder,
    name: str,
    source_type: str,
    *,
    prompt: str,
    system: str = "",
    context_steps: list[str] | None = None,
    status: str = TRANSLATED,
    note: str = "",
    model: str = "",
) -> Step:
    sid = b.new_id(name)
    bid = f"{b.process_id}-{sid}".replace("_", "-")
    lines = [
        f"# Imported from {b.source} ({source_type}) - check the wording before release.",
        f'id = "{bid}"',
        f'model_role = "{MODEL_ROLE}"',
        'flexibility = "guided"',
        "",
    ]
    if system:
        lines += ["[role]", f"de = {_toml_str(system[:4000])}", ""]
    lines += [
        "[instructions]",
        f"de = {_toml_str((prompt or '(kein Prompt im Export)')[:8000])}",
        "",
        "[output]",
        'text = "the answer"',
        "",
    ]
    if context_steps:
        lines += [
            "[context]",
            "steps = [" + ", ".join(_toml_str(s) for s in context_steps) + "]",
            "",
        ]
    b.briefings[bid] = "\n".join(lines)
    notes = [note] if note else []
    if model:
        notes.append(f"Modell im Export: {model} - hier gilt die Modellrolle '{MODEL_ROLE}'.")
    return b.add(Step(sid, name, "ai", source_type, status, " ".join(notes), {"briefing": bid}))


def add_gate(
    b: Builder, name: str, *, approving: str = "approved", reject: bool = True, note: str = ""
) -> Step:
    sid = b.new_id(f"freigabe {name}")
    return b.add(
        Step(
            sid,
            f"Freigabe: {name}",
            "gate",
            "",
            TRANSLATED,
            note,
            {"approving": approving, "reject": reject},
        )
    )


def add_placeholder(
    b: Builder, name: str, source_type: str, note: str, status: str = PLACEHOLDER
) -> Step:
    sid = b.new_id(name)
    return b.add(
        Step(
            sid,
            f"Von Hand nachbauen: {name}",
            "placeholder",
            source_type,
            status,
            note,
            {"approving": "done"},
        )
    )


def add_http(
    b: Builder,
    name: str,
    source_type: str,
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    query: dict[str, str],
    body,
    secret_hint: str = "",
    status: str = TRANSLATED,
    note: str = "",
) -> list[Step]:
    """GET without credentials on a fixed address → a read tool (no gate,
    ADR-0059); anything else → an http tool behind a gate."""
    method = (method or "GET").upper()
    parts = re.match(r"^(https?)://([^/{}?#]+)(/[^?#]*)?(?:\?([^#]*))?", url or "")
    if (
        not parts
        or "{" in (parts.group(2) or "")
        or method not in ("GET", "POST", "PUT", "PATCH", "DELETE")
    ):
        return [
            add_placeholder(
                b,
                name,
                source_type,
                f"HTTP-Adresse ohne festen Host ({url!r}) - Host in der Deklaration festlegen. "
                + note,
            )
        ]
    tid = f"{b.process_id}-{slug(name).replace('_', '-')}"[:60].rstrip("-")
    while tid in b.tools:
        tid += "-2"
    inputs: dict[str, str] = {}
    declared: dict[str, str] = {}

    def bind(value: str, key: str, url_part: bool = False) -> str:
        """An expression in a URL/query/body becomes a tool input."""
        if "{trigger." in value or "{steps." in value:
            single = re.fullmatch(r"\{((?:trigger|steps)(?:\.[A-Za-z0-9_]+)+)\}", value.strip())
            if single or not url_part:
                binding = single.group(1) if single else f"template:{value}"
                return f"{{input:{new_input(key, binding)}}}"
            # inside an address: one input per reference, the text around stays
            return re.sub(
                r"\{((?:trigger|steps)(?:\.[A-Za-z0-9_]+)+)\}",
                lambda m: f"{{input:{new_input(m.group(1).split('.')[-1], m.group(1))}}}",
                value,
            )
        return value

    def new_input(key: str, binding: str) -> str:
        iname = slug(key, "value")
        n = 2
        while iname in inputs:
            iname, n = f"{slug(key, 'value')}_{n}", n + 1
        inputs[iname] = binding
        declared[iname] = f"from the imported {key}"
        return iname

    has_secret = bool(secret_hint) or any(re.search(r"auth|token|key", k, re.I) for k in headers)
    path = parts.group(3) or "/"
    fixed = f"{parts.group(1)}://{parts.group(2)}"
    readable = method == "GET" and not has_secret and parts.group(1) == "https"
    readable = (
        readable
        and "{" not in path
        and not any(
            "{" in re.sub(r"\{(?:trigger|steps)(?:\.[A-Za-z0-9_]+)+\}", "", v)
            or not re.fullmatch(r"\s*\{(?:trigger|steps)(?:\.[A-Za-z0-9_]+)+\}\s*|[^{]*", v)
            for v in query.values()
        )
    )
    if readable:
        q = {k: bind(v, k) for k, v in query.items()}
        lines = [
            f"# Imported from {b.source} ({source_type}): a read (GET, fixed address, no secret).",
            f'id = "{tid}"',
            f"name = {_toml_str(name)}",
            f"description = {_toml_str('Imported: ' + name)}",
            'effect = "read"',
            'connector = "fetch"',
            'on_error = "failed"',
            "[inputs]",
        ]
        lines += [f"{k} = {_toml_str(v)}" for k, v in declared.items()] or ["# none"]
        lines += [
            "[outputs]",
            'data = "the answer"',
            "[fetch]",
            'format = "json"',
            f"url = {_toml_str(fixed + path)}",
        ]
        if q:
            lines.append(
                "query = { "
                + ", ".join(f"{_toml_str(k)} = {_toml_str(v)}" for k, v in q.items())
                + " }"
            )
        lines.append('response = { data = "" }')
        b.tools[tid] = "\n".join(lines) + "\n"
        step = b.add(
            Step(
                b.new_id(name),
                name,
                "tool",
                source_type,
                status,
                (note + " Lesend ohne Freigabe (ADR-0059).").strip(),
                {"tool": tid, "inputs": inputs},
            )
        )
        return [step]
    url_path = bind(path, "path", url_part=True) if "{" in path else path
    query_part = ""
    if query:
        query_part = "?" + "&".join(f"{k}={bind(v, k)}" for k, v in query.items())
    body_table = {}
    if isinstance(body, dict):
        body_table = {str(k): bind(str(v), str(k)) for k, v in body.items()}
    elif body not in (None, ""):
        status = APPROXIMATED
        note = (
            note + " Rohkörper nicht als Tabelle übernehmbar - Felder in [http] body eintragen."
        ).strip()
    lines = [
        f"# Imported from {b.source} ({source_type}): writes outside - behind a gate.",
        f'id = "{tid}"',
        f"name = {_toml_str(name)}",
        f"description = {_toml_str('Imported: ' + name)}",
        'effect = "external"',
        'connector = "http"',
        'on_error = "failed"',
        "[inputs]",
    ]
    lines += [f"{k} = {_toml_str(v)}" for k, v in declared.items()] or ["# none"]
    lines += [
        "[outputs]",
        'data = "the answer"',
        "[http]",
        f'method = "{method}"',
        f"url = {_toml_str(fixed + url_path + query_part)}",
    ]
    hdrs = {
        k: v
        for k, v in headers.items()
        if not re.search(r"auth|token|key|cookie", k, re.I) and "{" not in v
    }
    if has_secret:
        secret = b.secret(secret_hint or f"{tid}_token")
        hdrs["Authorization"] = f"Bearer {{secret:{secret}}}"
        note = (note + f" Zugang: Geheimnis {secret} setzen (Kopfzeile prüfen).").strip()
    if hdrs:
        lines.append(
            "headers = { "
            + ", ".join(f"{_toml_str(k)} = {_toml_str(v)}" for k, v in hdrs.items())
            + " }"
        )
    if body_table:
        lines.append(
            "body = { "
            + ", ".join(f"{_toml_str(k)} = {_toml_str(v)}" for k, v in body_table.items())
            + " }"
        )
    lines.append('response = { data = "" }')
    b.tools[tid] = "\n".join(lines) + "\n"
    gate = add_gate(b, name)
    step = b.add(
        Step(
            b.new_id(name), name, "tool", source_type, status, note, {"tool": tid, "inputs": inputs}
        )
    )
    gate.next = step.id
    return [gate, step]


def add_mail(
    b: Builder,
    name: str,
    source_type: str,
    *,
    to: str,
    subject: str,
    body: str,
    status: str = TRANSLATED,
    note: str = "",
) -> list[Step]:
    def bind(value: str) -> str:
        single = re.fullmatch(r"\{((?:trigger|steps)(?:\.[A-Za-z0-9_]+)+)\}", (value or "").strip())
        if single:
            return single.group(1)
        if "{trigger." in value or "{steps." in value:
            return f"template:{value}"
        return f"literal:{value}"

    gate = add_gate(b, name, note="Mail geht erst nach Freigabe hinaus (ADR-0048).")
    step = b.add(
        Step(
            b.new_id(name),
            name,
            "mail",
            source_type,
            status,
            (note + " Empfänger müssen in der Allowlist mail_recipients stehen.").strip(),
            {"inputs": {"to": bind(to), "subject": bind(subject), "body": bind(body)}},
        )
    )
    gate.next = step.id
    return [gate, step]


_OPS = {
    "equals": "eq",
    "equal": "eq",
    "is": "eq",
    "exactly": "eq",
    "text_exactly_matches": "eq",
    "text:equal": "eq",
    "number:equal": "eq",
    "number_is_equal_to": "eq",
    "boolean:equal": "eq",
    "notequals": "ne",
    "notequal": "ne",
    "not_equals": "ne",
    "text:notequal": "ne",
    "text_does_not_exactly_match": "ne",
    "isnot": "ne",
    "gt": "gt",
    "larger": "gt",
    "number:greater": "gt",
    "number_is_greater_than": "gt",
    "lt": "lt",
    "smaller": "lt",
    "number:less": "lt",
    "number_is_less_than": "lt",
    "gte": "ge",
    "largerequal": "ge",
    "number:greaterorequal": "ge",
    "lte": "le",
    "smallerequal": "le",
    "number:lessorequal": "le",
    "true": "true",
    "boolean_is_true": "true",
    "false": "false",
    "boolean_is_false": "false",
    "exists": "exists",
    "exist": "exists",
    "isnotempty": "exists",
    "notempty": "exists",
    "list_is_not_empty": "exists",
    "notexists": "missing",
    "notexist": "missing",
    "doesnotexist": "missing",
    "does_not_exist": "missing",
    "isempty": "missing",
    "empty": "missing",
    "list_is_empty": "missing",
}


def condition(path: str, op: str, value) -> tuple[tuple[str, str] | None, str]:
    """(input, S-FEEL test) for a simple comparison, or (None, why)."""
    kind = _OPS.get(str(op).lower(), "") or _OPS.get(re.sub(r"[\s_-]", "", str(op).lower()), "")
    if not path:
        return None, "Wert nicht als Pfad lesbar"
    if kind in ("eq", "ne"):
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)) or re.fullmatch(r"-?\d+(\.\d+)?", str(value).strip()):
            text = str(value).strip()
        else:
            text = json.dumps(str(value), ensure_ascii=False)
        return (path, text if kind == "eq" else f"not({text})"), ""
    if kind in ("gt", "lt", "ge", "le"):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None, f"Vergleich mit {value!r} ist keine Zahl"
        sym = {"gt": ">", "lt": "<", "ge": ">=", "le": "<="}[kind]
        return (path, f"{sym} {num:g}"), ""
    if kind == "true":
        return (path, "true"), ""
    if kind == "false":
        return (path, "false"), ""
    if kind == "exists":
        return (path, "not(null)"), ""
    if kind == "missing":
        return (path, "null"), ""
    return None, f"Operator {op!r} hat keine S-FEEL-Entsprechung"


def route_from_groups(
    b: Builder,
    sid_name: str,
    source_type: str,
    groups: list[list[tuple[str, str, object]]],
    *,
    combinator: str = "and",
) -> tuple[list[tuple[str, str]], str, str]:
    """Conditions of one branch from groups (outer OR of inner AND, the
    shape of Make and Activepieces) or one flat list with a combinator
    (n8n). Returns (conditions, match, note)."""
    notes = []
    flat: list[tuple[str, str]] = []
    if len(groups) == 1:
        match = "any" if combinator == "or" else "all"
        for path, op, value in groups[0]:
            c, why = condition(path, op, value)
            if c:
                flat.append(c)
            else:
                notes.append(why)
    elif all(len(g) == 1 for g in groups):
        match = "any"
        for ((path, op, value),) in groups:
            c, why = condition(path, op, value)
            if c:
                flat.append(c)
            else:
                notes.append(why)
    else:
        match = "all"
        for path, op, value in groups[0]:
            c, why = condition(path, op, value)
            if c:
                flat.append(c)
        notes.append("mehrere ODER-Gruppen mit UND - nur die erste Gruppe übernommen")
    if not flat:
        # never an always-true branch: a test that does not hold
        flat = [("trigger.imported_condition", '"von Hand setzen"')]
        notes.append("Bedingung von Hand setzen")
    return flat, match, "; ".join(notes)


# -- expressions -----------------------------------------------------------


class Refs:
    """Turns a platform's expressions into ``{trigger.x}`` / ``{steps.id.x}``."""

    def __init__(self, b: Builder) -> None:
        self.b = b
        self.unresolved: list[str] = []

    def trigger(self, parts: list[str]) -> str:
        fields = _path_fields(parts)
        if not fields:
            self.unresolved.append(".".join(parts))
            return ""
        name = self.b.input(
            fields[-1] if len(fields) > 1 and fields[0] in ("body", "query", "json") else fields[0]
        )
        return f"trigger.{name}"

    def step(self, sid: str, parts: list[str]) -> str:
        """AI steps answer in ``text``; tools keep the whole answer under
        ``outputs.data`` (``response = { data = "" }``)."""
        fields = _path_fields(parts)
        kind = self.b.steps[sid].kind if sid in self.b.steps else ""
        if kind == "ai":
            return f"steps.{sid}.text"
        if kind == "tool":
            if fields[:1] in (["data"], ["body"]):
                fields = fields[1:]
            return f"steps.{sid}.outputs.data" + "".join(f".{f}" for f in fields)
        return f"steps.{sid}" + "".join(f".{f}" for f in (fields or ["text"]))


def substitute(text: str, pattern: re.Pattern, resolve) -> tuple[str, bool]:
    """Replace every expression; returns (text, all resolved)."""
    ok = [True]

    def rep(m):
        path = resolve(m.group(1).strip())
        if not path:
            ok[0] = False
            return "⟨" + m.group(1).strip()[:60] + "⟩"
        return "{" + path + "}"

    return pattern.sub(rep, text or ""), ok[0]


def single_path(text: str) -> str:
    m = re.fullmatch(r"\{((?:trigger|steps)(?:\.[A-Za-z0-9_]+)+)\}", (text or "").strip())
    return m.group(1) if m else ""


# -- n8n ---------------------------------------------------------------------

_N8N_EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)


def _n8n(data: dict, b: Builder) -> None:
    nodes = {
        n.get("name"): n for n in data.get("nodes") or [] if isinstance(n, dict) and n.get("name")
    }
    conns = data.get("connections") or {}
    # sub-nodes of an AI node (model, memory, tools, parser) hang on non-main links
    sub_of: dict[str, tuple[str, str]] = {}
    for src, c in conns.items():
        for kind, outs in (c or {}).items():
            if kind != "main":
                for o in outs or []:
                    for tgt in o or []:
                        sub_of.setdefault(src, (kind, str(tgt.get("node"))))
    triggers = [
        n
        for n in nodes.values()
        if re.search(r"(trigger|webhook|formtrigger)$", str(n.get("type", "")).split(".")[-1], re.I)
        and not str(n.get("type", "")).endswith("respondToWebhook")
    ]
    if not triggers:
        incoming = {
            t.get("node")
            for c in conns.values()
            for outs in (c.get("main") or [])
            for t in (outs or [])
        }
        triggers = [
            n
            for n in nodes.values()
            if n["name"] not in incoming and "stickyNote" not in str(n.get("type"))
        ][:1]
    if not triggers:
        raise WorkflowImportError("n8n: no trigger node in the export")
    if len(triggers) > 1:
        b.skip(
            ", ".join(t["name"] for t in triggers[1:]),
            "trigger",
            "weitere Auslöser nicht übernommen - je Auslöser ein Einstieg (ADR-0057) von Hand",
        )
    mapped: dict[str, str] = {}  # n8n node name → first step id
    output_of: dict[str, str] = {}  # n8n node name → the step whose result it is
    refs = Refs(b)
    passthrough_vars: dict[str, dict[str, str]] = {}

    def resolve_for(prev: str | None):
        def resolve(expr: str) -> str:
            e = expr.strip()
            m = re.match(
                r"^\$\(\s*['\"](.+?)['\"]\s*\)\.(?:item|first\(\)|last\(\))\.json\.?(.*)$", e
            ) or re.match(r"^\$node\[\s*['\"](.+?)['\"]\s*\]\.json\.?(.*)$", e)
            if m:
                return ref_node(m.group(1), m.group(2))
            m = re.match(r"^\$json\.?(.*)$", e) or re.match(r"^\$json\[['\"](.+?)['\"]\]$", e)
            if m:
                return ref_node(prev, m.group(1)) if prev else ""
            return ""

        return resolve

    def ref_node(name: str | None, rest: str) -> str:
        parts = [p for p in re.split(r"[.\[\]'\"]+", rest or "") if p]
        if name is None or name not in nodes:
            return ""
        node = nodes[name]
        if node in triggers:
            return refs.trigger(parts)
        if name in passthrough_vars:
            return refs.trigger(parts)
        sid = output_of.get(name) or mapped.get(name)
        if not sid or sid.startswith("__"):
            return ""
        return refs.step(sid, parts[1:] if parts[:1] == ["output"] else parts)

    def text(value, prev: str | None) -> tuple[str, bool]:
        if not isinstance(value, str):
            return ("" if value is None else str(value)), True
        if not value.startswith("="):
            return value, True
        return substitute(value[1:], _N8N_EXPR, resolve_for(prev))

    def outputs(name: str, index: int = 0) -> list[str]:
        main = (conns.get(name) or {}).get("main") or []
        if index >= len(main):
            return []
        return [t.get("node") for t in (main[index] or []) if t.get("node") in nodes]

    def prev_of(name: str) -> str | None:
        for src, c in conns.items():
            for outs in c.get("main") or []:
                if any(t.get("node") == name for t in (outs or [])):
                    return src
        return None

    def n8n_conditions(
        params: dict, prev: str | None
    ) -> tuple[list[list[tuple[str, str, object]]], str]:
        cond = params.get("conditions") or {}
        if isinstance(cond, dict) and isinstance(cond.get("conditions"), list):  # v2
            items = []
            for c in cond["conditions"]:
                left, _ = text(c.get("leftValue"), prev)
                op = (c.get("operator") or {}).get("operation", "")
                right, _ = text(c.get("rightValue"), prev)
                items.append((single_path(left), op, right))
            return [items], str(cond.get("combinator") or "and")
        items = []
        for _kind, lst in cond.items() if isinstance(cond, dict) else []:
            for c in lst or []:
                left, _ = text(c.get("value1"), prev)
                right, _ = text(c.get("value2"), prev)
                items.append((single_path(left), c.get("operation", "equal"), right))
        combine = "or" if params.get("combineOperation") == "any" else "and"
        return [items], combine

    stack: list[str] = []

    def translate(name: str) -> str | None:
        """First step id of the node (translating it once); None = finish.
        An edge back to a node still being translated (a loop) is cut."""
        if name in stack:
            b.skip(
                name,
                str(nodes[name].get("type")),
                "Rückkante (Schleife) entfernt - als Multi-Instance (for_each) nachbauen",
            )
            return None
        if name in mapped:
            return mapped[name]
        stack.append(name)
        try:
            return _translate(name)
        finally:
            stack.pop()

    def _translate(name: str) -> str | None:
        node = nodes[name]
        t = str(node.get("type", ""))
        short = t.split(".")[-1]
        params = node.get("parameters") or {}
        prev = prev_of(name)
        steps: list[Step] = []
        creds = node.get("credentials") or {}
        secret_hint = ""
        if isinstance(creds, dict):
            secret_hint = next(
                (str(v.get("name") or k) for k, v in creds.items() if isinstance(v, dict)), ""
            )

        if node in triggers:
            cfg: dict = {"trigger": "manual"}
            note = ""
            if short in ("scheduleTrigger", "cron"):
                rules = (params.get("rule") or {}).get("interval") or [{}]
                r = rules[0] if rules else {}
                f = r.get("field", "days")
                hour = _int(r.get("triggerAtHour"), 8)
                minute = _int(r.get("triggerAtMinute"), 0)
                sched = {"every": "day", "at": f"{hour:02d}:{minute:02d}"}
                if f == "weeks":
                    days = r.get("triggerAtDay") or [1]
                    names = [
                        "sunday",
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                    ]
                    sched = {
                        "every": "week",
                        "weekday": names[_int(days[0], 1) % 7],
                        "at": sched["at"],
                    }
                elif f in ("hours", "minutes"):
                    sched = {"every": "day", "at": "08:00"}
                    note = (
                        f"Intervall ({f}) auf täglich 08:00 angenähert"
                        " - Zeitpläne kennen Tag/Woche/Monat."
                    )
                elif f == "cronExpression":
                    note = f"Cron {r.get('expression')!r} auf täglich angenähert."
                cfg = {"trigger": "schedule", "schedule": sched}
                status = APPROXIMATED if note else TRANSLATED
            elif short in ("manualTrigger", "executeWorkflowTrigger"):
                status = TRANSLATED
            elif short in ("webhook", "formTrigger", "chatTrigger"):
                status, note = (
                    APPROXIMATED,
                    (
                        "Webhook/Formular → manueller Start (Startformular aus dem "
                        "Vertrag, Intake-Kanal POST /api/v1/intake oder /templates/{id}/start)."
                    ),
                )
            else:
                status, note = (
                    APPROXIMATED,
                    (
                        f"Auslöser {short} → manueller Start; als Kanal "
                        "(E-Mail-Intake, Zeitplan) von Hand anbinden."
                    ),
                )
            step = b.add(Step(b.new_id(name), name, "start", t, status, note, cfg))
            mapped[name] = step.id
            nxt = outputs(name)
            if len(nxt) > 1:
                b.skip(name, t, "mehrere Folgeknoten nacheinander statt parallel")
            step.next = chain(nxt)
            return step.id

        if short == "stickyNote":
            return None
        if short == "respondToWebhook":
            b.skip(
                name,
                t,
                "Antwort an den Aufrufer: das Ergebnis steht im Lauf-Bericht "
                "(POST /templates/{id}/run)",
            )
            mapped[name] = "__pass__"
            target = chain(outputs(name))
            mapped[name] = target or "__end__"
            return target
        if short in ("noOp", "merge", "set", "splitOut", "filter") and short != "filter":
            if short == "set":
                passthrough_vars[name] = {}
                b.skip(name, t, "Felder setzen: Werte werden als Eingaben des Vorgangs gelesen")
            else:
                b.skip(name, t, "durchgereicht", TRANSLATED)
            mapped[name] = "__pass__"
            target = chain(outputs(name))
            mapped[name] = target or "__end__"
            return target

        if short in ("if", "filter"):
            groups, comb = n8n_conditions(params, prev)
            sid = b.new_id(name)
            step = b.add(Step(sid, name, "route", t))
            mapped[name] = sid
            conds, match, note = route_from_groups(b, name, t, groups, combinator=comb)
            b.set_status(step, APPROXIMATED if note else TRANSLATED, note)
            true_target = chain(outputs(name, 0))
            false_target = chain(outputs(name, 1)) if short == "if" else None
            step.branches = [Branch("yes", conds, match, true_target)]
            step.default = Branch("no", [], target=false_target if short == "if" else "filtered")
            return sid

        if short == "switch":
            sid = b.new_id(name)
            step = b.add(Step(sid, name, "route", t))
            mapped[name] = sid
            rules = (
                (params.get("rules") or {}).get("values")
                or (params.get("rules") or {}).get("rules")
                or []
            )
            notes = []
            for i, rule in enumerate(rules):
                if isinstance(rule.get("conditions"), dict):
                    groups, comb = n8n_conditions(rule, prev)
                else:
                    left, _ = text(params.get("value1"), prev)
                    groups, comb = (
                        [[(single_path(left), rule.get("operation", "equal"), rule.get("value2"))]],
                        "and",
                    )
                conds, match, note = route_from_groups(b, name, t, groups, combinator=comb)
                if note:
                    notes.append(f"Regel {i + 1}: {note}")
                outcome = slug(str(rule.get("outputKey") or f"rule_{i + 1}"), f"rule_{i + 1}")
                step.branches.append(Branch(outcome, conds, match, chain(outputs(name, i))))
            if not rules:
                # rules in a form we do not read (expression mode, older
                # versions): one branch per output, conditions by hand
                n_out = len((conns.get(name) or {}).get("main") or [])
                for i in range(max(n_out - 1, 1)):
                    step.branches.append(
                        Branch(
                            f"rule_{i + 1}",
                            [("trigger.imported_condition", '"von Hand setzen"')],
                            "all",
                            chain(outputs(name, i)),
                        )
                    )
                mode = params.get("mode") or "Format"
                notes.append(f"Regeln ({mode}) nicht lesbar - Bedingungen von Hand setzen")
            fallback = outputs(name, len(rules) if rules else max(len(step.branches), 1))
            step.default = Branch("otherwise", [], target=chain(fallback) if fallback else None)
            b.set_status(step, APPROXIMATED if notes else TRANSLATED, "; ".join(notes))
            return sid

        if short == "wait":
            amount = float(params.get("amount") or 1)
            unit = str(params.get("unit") or "hours")
            seconds = amount * {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}.get(
                unit, 3600
            )
            if params.get("resume") not in (None, "timeInterval"):
                steps = [
                    add_placeholder(
                        b,
                        name,
                        t,
                        f"Warten auf {params.get('resume')} - als Gate oder Zeitplan nachbauen.",
                        APPROXIMATED,
                    )
                ]
            else:
                steps = [
                    b.add(
                        Step(b.new_id(name), name, "wait", t, TRANSLATED, "", {"seconds": seconds})
                    )
                ]
        elif short == "httpRequest":
            url, ok1 = text(params.get("url"), prev)
            query = {}
            for p in (params.get("queryParameters") or {}).get("parameters") or []:
                query[str(p.get("name"))], _ = text(p.get("value"), prev)
            headers = {}
            for p in (params.get("headerParameters") or {}).get("parameters") or []:
                headers[str(p.get("name"))], _ = text(p.get("value"), prev)
            body = None
            if params.get("sendBody"):
                bp = (params.get("bodyParameters") or {}).get("parameters") or []
                if bp:
                    body = {str(p.get("name")): text(p.get("value"), prev)[0] for p in bp}
                else:
                    body = params.get("jsonBody") or params.get("body") or ""
            auth = params.get("authentication") not in (None, "none") or bool(creds)
            note = "" if ok1 else "Ausdruck in der Adresse nicht vollständig übersetzt."
            steps = add_http(
                b,
                name,
                t,
                method=str(params.get("method") or "GET"),
                url=url,
                headers=headers,
                query=query,
                body=body,
                secret_hint=(secret_hint or "api_token") if auth else "",
                status=TRANSLATED if ok1 else APPROXIMATED,
                note=note,
            )
        elif short in ("emailSend", "gmail", "microsoftOutlook", "sendGrid", "mailjet") and (
            short == "emailSend" or str(params.get("operation", "send")) in ("send", "sendAndWait")
        ):
            to, _ = text(
                params.get("toEmail") or params.get("sendTo") or params.get("toRecipients") or "",
                prev,
            )
            subject, _ = text(params.get("subject") or "", prev)
            body, ok = text(
                params.get("text")
                or params.get("message")
                or params.get("html")
                or params.get("bodyContent")
                or "",
                prev,
            )
            steps = add_mail(
                b,
                name,
                t,
                to=to,
                subject=subject,
                body=body,
                status=TRANSLATED if ok else APPROXIMATED,
                note="" if ok else "Ausdruck im Text nicht vollständig übersetzt.",
            )
        elif re.search(
            r"langchain\.(agent|chainLlm|openAi|anthropic|lmChat)|nodes-base\.openAi|"
            r"langchain\.(chainSummarization|textClassifier|informationExtractor|sentimentAnalysis)",
            t,
        ):
            prompt_raw = params.get("text") or params.get("prompt") or ""
            msgs = (
                (params.get("messages") or {}).get("values")
                or (params.get("messages") or {}).get("messageValues")
                or []
            )
            system = ""
            for m in msgs:
                content = m.get("content") or m.get("message") or ""
                if m.get("role") == "system" or m.get("type") == "SystemMessagePromptTemplate":
                    system = content
                elif not prompt_raw:
                    prompt_raw = content
            system = system or ((params.get("options") or {}).get("systemMessage") or "")
            prompt, ok = text(prompt_raw, prev)
            system_t, _ = text(system, prev)
            steps_in = sorted(set(re.findall(r"\{steps\.([a-z0-9_]+)", prompt)))
            prompt = re.sub(
                r"\{steps\.([a-z0-9_]+)((?:\.[A-Za-z0-9_]+)*)\}",
                lambda m: f"(Ergebnis von Schritt {m.group(1)}{m.group(2)})",
                prompt,
            )
            tools_used = [
                s
                for s, c in conns.items()
                if any(
                    tgt.get("node") == name
                    for k, outs in c.items()
                    if k.startswith("ai_tool")
                    for o in outs
                    for tgt in (o or [])
                )
            ]
            note = "" if ok else "Ausdruck im Prompt nicht vollständig übersetzt."
            if tools_used:
                note += (
                    f" Agent-Werkzeuge ({', '.join(tools_used)}) nicht übernommen"
                    " - als Schritte oder MCP nachbauen."
                )
            model = str(
                (params.get("model") or {}).get("value")
                if isinstance(params.get("model"), dict)
                else params.get("model") or params.get("modelId") or ""
            )
            steps = [
                add_ai(
                    b,
                    name,
                    t,
                    prompt=prompt,
                    system=system_t,
                    context_steps=steps_in,
                    status=TRANSLATED if ok and not tools_used else APPROXIMATED,
                    note=note.strip(),
                    model=model,
                )
            ]
        elif short in ("code", "function", "functionItem", "executeCommand", "aiTransform"):
            steps = [
                add_placeholder(
                    b,
                    name,
                    t,
                    "Code wird nie übernommen. Original: "
                    + _excerpt(
                        params.get("jsCode")
                        or params.get("pythonCode")
                        or params.get("functionCode")
                        or params,
                        400,
                    ),
                )
            ]
        elif short in ("splitInBatches",):
            steps = [
                add_placeholder(
                    b,
                    name,
                    t,
                    "Schleife: als Multi-Instance (for_each) am Folgeschritt "
                    "nachbauen; die Schritte darin laufen hier einmal.",
                    APPROXIMATED,
                )
            ]
        elif short == "executeWorkflow":
            steps = [
                add_placeholder(
                    b,
                    name,
                    t,
                    "Teilprozess: als callActivity mit dem importierten Prozess verbinden.",
                    APPROXIMATED,
                )
            ]
        else:
            hint = MCP_HINTS.get(short.lower().replace("trigger", ""), "")
            steps = [
                add_placeholder(
                    b,
                    name,
                    t,
                    f"Integration {short} ohne Entsprechung."
                    + (
                        f" {hint}"
                        if hint
                        else " Über einen MCP-Konnektor oder ein HTTP-Werkzeug nachbauen."
                    )
                    + " Einstellungen: "
                    + _excerpt(params, 300),
                )
            ]
            if creds:
                steps[0].note += f" Zugang im Export: {', '.join(creds)} (nicht übernommen)."
        mapped[name] = steps[0].id
        output_of[name] = steps[-1].id  # a gate in front: the output is the action's
        nxt = outputs(name)
        if len(nxt) > 1:
            b.report.append(
                ReportItem(
                    name,
                    t,
                    APPROXIMATED,
                    steps[-1].id,
                    "mehrere Folgeknoten laufen hier nacheinander statt parallel",
                )
            )
        steps[-1].next = chain(nxt)
        return steps[0].id

    def chain(names: list[str]) -> str | None:
        """Several successors on one output: one after the other."""
        first: str | None = None
        last: Step | None = None
        for n in names:
            sid = translate(n)
            if sid in ("__pass__",):
                continue
            if sid == "__end__":
                sid = None
            if first is None:
                first = sid
            elif last is not None and sid and last.next is None and last.kind not in ("route",):
                last.next = sid
            if sid:
                last = _last_of(b, sid)
        return first

    translate(triggers[0]["name"])
    for name, node in nodes.items():
        if (
            name not in mapped
            and "stickyNote" not in str(node.get("type"))
            and node not in triggers[1:]
        ):
            parent = sub_of.get(name)
            if parent:
                kind, target = parent
                what = {
                    "ai_languageModel": "Modell - hier gilt die Modellrolle des KI-Schritts",
                    "ai_memory": "Gedächtnis - der KI-Schritt liest den Vorgang (context)",
                    "ai_tool": "Werkzeug des Agenten - als Schritt oder MCP-Werkzeug nachbauen",
                    "ai_outputParser": "Ausgabeformat - steht im Briefing unter [output]",
                }.get(kind, f"Unterknoten ({kind})")
                b.skip(name, str(node.get("type")), f"Teil von {target}: {what}")
            else:
                b.skip(
                    name,
                    str(node.get("type")),
                    "nicht mit dem Auslöser verbunden - nicht übernommen",
                )


def _last_of(b: Builder, sid: str) -> Step:
    """The last step of a straight run starting at sid."""
    seen = set()
    s = b.steps[sid]
    while s.next and s.next in b.steps and s.kind != "route" and s.id not in seen:
        seen.add(s.id)
        s = b.steps[s.next]
    return s


# Integrations for which a ready MCP server or a declared tool is the
# natural replacement - shown in the report instead of a bare "unknown".
MCP_HINTS = {
    "slack": (
        "MCP: Slack-Server (z. B. Activepieces-Piece slack, MIT)"
        " oder HTTP-Werkzeug chat.postMessage."
    ),
    "googlesheets": "In aitmation: Sammlung + record/rows; extern: MCP Google Sheets.",
    "airtable": "In aitmation: Sammlung + record/rows; extern: MCP Airtable.",
    "notion": "MCP: Notion-Server (offizieller MCP-Server von Notion).",
    "telegram": "HTTP-Werkzeug sendMessage (Bot-Token als Werkzeug-Geheimnis).",
    "googledrive": "MCP: Google-Drive-Server; Dateien im Vorgang bleiben im Kern.",
    "hubspot": "MCP: HubSpot-Server oder HTTP-Werkzeug (Private-App-Token).",
    "github": "MCP: GitHub-Server (offiziell).",
    "jira": "MCP: Atlassian-Server (offiziell).",
    "trello": "HTTP-Werkzeug (Key/Token als Werkzeug-Geheimnis).",
    "googlecalendar": "MCP: Google-Calendar-Server.",
    "microsoftteams": "MCP: Microsoft-365-Server oder Webhook über HTTP-Werkzeug.",
}


# -- Activepieces ------------------------------------------------------------

_AP_EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)


def _activepieces(data: dict, b: Builder) -> None:
    flow = data.get("template") if isinstance(data.get("template"), dict) else data
    if isinstance(flow.get("flows"), list) and flow["flows"]:
        flow = flow["flows"][0].get("template") or flow["flows"][0]
    if isinstance(flow.get("version"), dict):
        flow = flow["version"]
    trigger = flow.get("trigger")
    if not isinstance(trigger, dict):
        raise WorkflowImportError("activepieces: no trigger in the export")
    refs = Refs(b)
    mapped: dict[str, str] = {}

    def resolve(expr: str) -> str:
        e = expr.strip()
        m = re.match(r"^trigger(?:\[['\"](.+?)['\"]\]|\.(.*))?(.*)$", e)
        if m:
            rest = (m.group(1) or "") + "." + (m.group(2) or "") + (m.group(3) or "")
            return refs.trigger([p for p in re.split(r"[.\[\]'\"]+", rest) if p])
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(.*)$", e)
        if m and m.group(1) in mapped:
            parts = [p for p in re.split(r"[.\[\]'\"]+", m.group(2)) if p]
            sid = output_of.get(m.group(1)) or mapped[m.group(1)]
            return refs.step(sid, parts[1:] if parts[:1] == ["output"] else parts)
        return ""

    def text(value) -> tuple[str, bool]:
        if not isinstance(value, str):
            return (
                ""
                if value is None
                else json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list))
                else str(value)
            ), True
        return substitute(value, _AP_EXPR, resolve)

    settings = trigger.get("settings") or {}
    piece = str(settings.get("pieceName") or "")
    tname = str(settings.get("triggerName") or "")
    cfg: dict = {"trigger": "manual"}
    status, note = TRANSLATED, ""
    if "schedule" in piece:
        inp = settings.get("input") or {}
        if "week" in tname:
            days = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
            cfg = {
                "trigger": "schedule",
                "schedule": {
                    "every": "week",
                    "weekday": days[_int(inp.get("day_of_the_week"), 1) % 7],
                    "at": f"{_int(inp.get('hour_of_the_day'), 8):02d}:00",
                },
            }
        elif "day" in tname:
            cfg = {
                "trigger": "schedule",
                "schedule": {
                    "every": "day",
                    "at": f"{_int(inp.get('hour_of_the_day'), 8):02d}:00",
                },
            }
        else:
            cfg = {"trigger": "schedule", "schedule": {"every": "day", "at": "08:00"}}
            status, note = APPROXIMATED, f"Zeitplan {tname} auf täglich 08:00 angenähert."
    elif trigger.get("type") == "EMPTY" or "manual" in piece:
        pass
    else:
        status, note = (
            APPROXIMATED,
            f"Auslöser {piece or trigger.get('type')} → manueller Start (Kanal von Hand).",
        )
    start = b.add(
        Step(
            b.new_id(trigger.get("displayName") or "Start"),
            trigger.get("displayName") or "Start",
            "start",
            piece or str(trigger.get("type")),
            status,
            note,
            cfg,
        )
    )
    mapped[str(trigger.get("name") or "trigger")] = start.id
    output_of: dict[str, str] = {}

    def action(a: dict | None) -> str | None:
        """Translate a chain of actions; returns the first step id."""
        if not isinstance(a, dict):
            return None
        name = str(a.get("displayName") or a.get("name") or "Schritt")
        typ = str(a.get("type") or "")
        st = a.get("settings") or {}
        inp = st.get("input") or {}
        piece = str(st.get("pieceName") or "")
        act = str(st.get("actionName") or "")
        stype = f"{piece}:{act}" if piece else typ
        steps: list[Step]
        if typ in ("ROUTER", "BRANCH"):
            sid = b.new_id(name)
            r = b.add(Step(sid, name, "route", typ))
            mapped[str(a.get("name"))] = sid
            after = action(a.get("nextAction"))
            notes = []
            if typ == "BRANCH":
                groups = [
                    [
                        (
                            single_path(text(c.get("firstValue"))[0]),
                            c.get("operator"),
                            text(c.get("secondValue"))[0],
                        )
                        for c in g
                    ]
                    for g in st.get("conditions") or []
                ]
                conds, match, note = route_from_groups(b, name, typ, groups or [[]])
                r.branches = [
                    Branch("yes", conds, match, action(a.get("onSuccessAction")) or after)
                ]
                r.default = Branch("no", [], target=action(a.get("onFailureAction")) or after)
                notes.append(note)
            else:
                children = a.get("children") or []
                for i, br in enumerate(st.get("branches") or []):
                    target = action(children[i] if i < len(children) else None) or after
                    if br.get("branchType") == "FALLBACK":
                        r.default = Branch(
                            slug(br.get("branchName") or "otherwise", "otherwise"),
                            [],
                            target=target,
                        )
                        continue
                    groups = [
                        [
                            (
                                single_path(text(c.get("firstValue"))[0]),
                                c.get("operator"),
                                text(c.get("secondValue"))[0],
                            )
                            for c in g
                        ]
                        for g in br.get("conditions") or []
                    ]
                    conds, match, note = route_from_groups(b, name, typ, groups or [[]])
                    if note:
                        notes.append(note)
                    r.branches.append(
                        Branch(
                            slug(br.get("branchName") or f"branch_{i + 1}", f"branch_{i + 1}"),
                            conds,
                            match,
                            target,
                        )
                    )
                if st.get("executionType") == "EXECUTE_ALL_MATCH":
                    notes.append("alle zutreffenden Zweige → hier nur der erste")
                if r.default is None:
                    r.default = Branch("otherwise", [], target=after)
            joined = "; ".join(n for n in notes if n)
            b.set_status(r, APPROXIMATED if joined else TRANSLATED, joined)
            # branches end in the chain after the router
            for br in [*r.branches, r.default]:
                if br and br.target and br.target != after:
                    tail = _last_of(b, br.target)
                    if tail.next is None and tail.kind != "route":
                        tail.next = after
            return sid
        if typ == "CODE":
            steps = [
                add_placeholder(
                    b,
                    name,
                    typ,
                    "Code wird nie übernommen. Original: "
                    + _excerpt((st.get("sourceCode") or {}).get("code") or st, 400),
                )
            ]
        elif typ in ("LOOP_ON_ITEMS", "PROCESS_IN_BATCHES"):
            steps = [
                add_placeholder(
                    b,
                    name,
                    typ,
                    f"Schleife über {st.get('items')!r}: als Multi-Instance "
                    "(for_each) nachbauen; die Schritte darin laufen hier einmal.",
                    APPROXIMATED,
                )
            ]
            mapped[str(a.get("name"))] = steps[0].id
            inner = action(a.get("firstLoopAction"))
            if inner:
                steps[0].next = inner
                steps = [steps[0], _last_of(b, inner)]
        elif typ == "PIECE" and piece.endswith("piece-http"):
            url, ok = text(inp.get("url"))
            headers = {k: text(v)[0] for k, v in (inp.get("headers") or {}).items()}
            query = {k: text(v)[0] for k, v in (inp.get("queryParams") or {}).items()}
            body = inp.get("body")
            if isinstance(body, dict) and "data" in body:
                body = body.get("data")
            body = (
                {k: text(v)[0] for k, v in body.items()}
                if isinstance(body, dict)
                else text(body)[0]
            )
            auth = str(inp.get("authType") or "NONE") != "NONE" or any(
                "auth" in k.lower() for k in headers
            )
            steps = add_http(
                b,
                name,
                stype,
                method=str(inp.get("method") or "GET"),
                url=url,
                headers=headers,
                query=query,
                body=body or None,
                secret_hint=slug(name) + "_token" if auth else "",
                status=TRANSLATED if ok else APPROXIMATED,
            )
        elif typ == "PIECE" and re.search(
            r"piece-(openai|claude|anthropic|google-gemini|mistral-ai|text-ai|ai)$", piece
        ):
            prompt, ok = text(inp.get("prompt") or inp.get("text") or inp.get("message") or "")
            system, _ = text(
                inp.get("systemPrompt") or inp.get("roleSystemContent") or inp.get("system") or ""
            )
            steps_in = sorted(set(re.findall(r"\{steps\.([a-z0-9_]+)", prompt)))
            prompt = re.sub(
                r"\{steps\.([a-z0-9_]+)((?:\.[A-Za-z0-9_]+)*)\}",
                lambda m: f"(Ergebnis von Schritt {m.group(1)}{m.group(2)})",
                prompt,
            )
            steps = [
                add_ai(
                    b,
                    name,
                    stype,
                    prompt=prompt,
                    system=system,
                    context_steps=steps_in,
                    status=TRANSLATED if ok else APPROXIMATED,
                    model=str(inp.get("model") or ""),
                )
            ]
        elif typ == "PIECE" and (
            piece.endswith("piece-smtp")
            or (piece.endswith(("piece-gmail", "piece-microsoft-outlook")) and "send" in act)
        ):
            to = inp.get("to") or inp.get("receiver") or ""
            to = ", ".join(to) if isinstance(to, list) else to
            steps = add_mail(
                b,
                name,
                stype,
                to=text(to)[0],
                subject=text(inp.get("subject") or "")[0],
                body=text(inp.get("body") or inp.get("body_text") or inp.get("text") or "")[0],
            )
        elif typ == "PIECE" and piece.endswith("piece-delay"):
            amount = float(inp.get("delayFor") or inp.get("delay") or 1)
            unit = str(inp.get("unit") or "seconds").lower()
            mult = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}.get(unit, 1)
            if "until" in act:
                steps = [
                    add_placeholder(
                        b,
                        name,
                        stype,
                        "Warten bis Datum: als Timer until_from nachbauen.",
                        APPROXIMATED,
                    )
                ]
            else:
                steps = [
                    b.add(
                        Step(
                            b.new_id(name),
                            name,
                            "wait",
                            stype,
                            TRANSLATED,
                            "",
                            {"seconds": amount * mult},
                        )
                    )
                ]
        elif typ == "PIECE" and piece.endswith("piece-approval"):
            sid = b.new_id(name)
            steps = [
                b.add(
                    Step(
                        sid,
                        name,
                        "gate",
                        stype,
                        TRANSLATED,
                        "Freigabe in den Pendenzen.",
                        {"approving": "approved", "reject": True},
                    )
                )
            ]
        elif typ == "PIECE" and re.search(
            r"piece-(data-mapper|store|text-helper|math-helper|date-helper)$", piece
        ):
            b.skip(name, stype, "Hilfsschritt: Werte werden direkt gelesen (prüfen)")
            mapped[str(a.get("name"))] = start.id
            return action(a.get("nextAction"))
        else:
            short = piece.replace("@activepieces/piece-", "")
            hint = MCP_HINTS.get(short.replace("-", ""), "")
            steps = [
                add_placeholder(
                    b,
                    name,
                    stype,
                    f"Integration {short or typ} ohne Entsprechung. "
                    + (
                        hint
                        or "Dieses Piece ist MIT-lizenziert und als MCP-Server nutzbar "
                        "(ADR-0060 Etappe 2)."
                    )
                    + " Einstellungen: "
                    + _excerpt(inp, 300),
                )
            ]
        mapped[str(a.get("name"))] = steps[0].id
        output_of[str(a.get("name"))] = steps[-1].id
        steps[-1].next = steps[-1].next or action(a.get("nextAction"))
        return steps[0].id

    start.next = action(trigger.get("nextAction"))


# -- Make ----------------------------------------------------------------------

_MAKE_NAMES = {
    "util": "Hilfsmodul",
    "http": "HTTP",
    "google-drive": "Google Drive",
    "google-email": "Gmail",
    "email": "E-Mail",
    "openai-gpt-3": "OpenAI",
    "anthropic-claude": "Claude",
    "slack": "Slack",
    "google-sheets": "Google Sheets",
    "airtable": "Airtable",
    "notion": "Notion",
    "builtin": "Ablauf",
}
_MAKE_EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)
_MAKE_TRIGGERS = re.compile(
    r"(gateway:CustomWebHook|gateway:CustomMailHook|:Watch|:Trigger|watch|trigger)", re.I
)


def _make(data: dict, b: Builder) -> None:
    flow = data.get("flow")
    if not isinstance(flow, list) or not flow:
        raise WorkflowImportError("make: no flow[] in the blueprint")
    refs = Refs(b)
    by_id: dict[str, str] = {}  # module id → step id
    as_trigger: set[str] = set()

    def resolve(expr: str) -> str:
        m = re.match(r"^\s*(\d+)\.(.+)$", expr)
        if not m:
            return ""
        mid, rest = m.group(1), m.group(2)
        parts = [p for p in re.split(r"[.\[\]`'\"]+", rest) if p]
        if mid in as_trigger:
            return refs.trigger(parts)
        if mid in by_id:
            return refs.step(by_id[mid], parts)
        return ""

    def text(value) -> tuple[str, bool]:
        if isinstance(value, list):
            value = ", ".join(str(v.get("address") if isinstance(v, dict) else v) for v in value)
        if not isinstance(value, str):
            return ("" if value is None else str(value)), True
        return substitute(value, _MAKE_EXPR, resolve)

    first = flow[0]
    module = str(first.get("module") or "")
    cfg: dict = {"trigger": "manual"}
    status, note = TRANSLATED, ""
    sched = (data.get("scheduling") or (data.get("metadata") or {}).get("scheduling")) or {}
    if sched.get("type") in ("indefinitely", "interval") or sched.get("interval"):
        cfg = {"trigger": "schedule", "schedule": {"every": "day", "at": "08:00"}}
        status, note = APPROXIMATED, f"Zeitplan {sched} auf täglich 08:00 angenähert."
    rest = flow
    if _MAKE_TRIGGERS.search(module):
        as_trigger.add(str(first.get("id")))
        rest = flow[1:]
        if "CustomWebHook" in module:
            status, note = APPROXIMATED, "Webhook → manueller Start (Startformular/Intake-Kanal)."
        else:
            status, note = APPROXIMATED, f"Auslöser {module} → manueller Start (Kanal von Hand)."
    start = b.add(
        Step(
            b.new_id("Start"),
            "Start",
            "start",
            module if as_trigger else "manual",
            status,
            note,
            cfg,
        )
    )

    def module_steps(m: dict) -> list[Step]:
        mod = str(m.get("module") or "")
        mid = str(m.get("id"))
        mapper = m.get("mapper") or {}
        host = re.match(r"^https?://([^/{}]+)", str(mapper.get("url") or ""))
        name = str(
            (m.get("metadata") or {}).get("designer", {}).get("name")
            or (f"HTTP {host.group(1)}" if mod.startswith("http:") and host else "")
            or _MAKE_NAMES.get(mod.split(":")[0], "")
            or mod.split(":")[-1]
            or "Modul"
        )
        params = m.get("parameters") or {}
        if mod.startswith("util:SetVariable"):
            as_trigger.add(mid)
            for v in mapper.get("variables") or (
                [{"name": mapper.get("name")}] if mapper.get("name") else []
            ):
                if v.get("name"):
                    b.input(str(v["name"]))
            b.skip(name, mod, "Variablen → Eingaben des Vorgangs (Startformular)")
            return []
        if mod.startswith("http:"):
            url, ok = text(mapper.get("url") or "")
            headers = {
                str(h.get("name")): text(h.get("value"))[0] for h in mapper.get("headers") or []
            }
            query = {str(q.get("name")): text(q.get("value"))[0] for q in mapper.get("qs") or []}
            raw = mapper.get("data")
            body = None
            if raw:
                body_text, _ = text(raw)
                try:
                    body = json.loads(
                        re.sub(r'(?<!")\{((?:trigger|steps)[^}]*)\}(?!")', r'"{\1}"', body_text)
                    )
                    body = (
                        {k: v for k, v in body.items() if isinstance(v, (str, int, float, bool))}
                        if isinstance(body, dict)
                        else body_text
                    )
                except ValueError:
                    body = body_text
            auth = bool(params.get("auth")) or "Auth" in mod
            return add_http(
                b,
                name,
                mod,
                method=str(mapper.get("method") or "get"),
                url=url,
                headers=headers,
                query=query,
                body=body,
                secret_hint=f"{slug(name)}_token" if auth else "",
                status=TRANSLATED if ok else APPROXIMATED,
            )
        if re.search(r"^(openai-gpt-3|openai|anthropic-claude|anthropic|gemini-ai|mistral)", mod):
            msgs = mapper.get("messages") or []
            prompt_raw = mapper.get("prompt") or ""
            system_raw = ""
            for msg in msgs:
                if msg.get("role") == "system":
                    system_raw = msg.get("content") or ""
                elif not prompt_raw:
                    prompt_raw = msg.get("content") or ""
            prompt, ok = text(prompt_raw)
            system, _ = text(system_raw)
            steps_in = sorted(set(re.findall(r"\{steps\.([a-z0-9_]+)", prompt)))
            prompt = re.sub(
                r"\{steps\.([a-z0-9_]+)((?:\.[A-Za-z0-9_]+)*)\}",
                lambda x: f"(Ergebnis von Schritt {x.group(1)}{x.group(2)})",
                prompt,
            )
            return [
                add_ai(
                    b,
                    name,
                    mod,
                    prompt=prompt,
                    system=system,
                    context_steps=steps_in,
                    status=TRANSLATED if ok else APPROXIMATED,
                    model=str(mapper.get("model") or ""),
                )
            ]
        if re.search(r"^(email|google-email|microsoft-email|smtp):", mod) and re.search(
            r"send", mod, re.I
        ):
            return add_mail(
                b,
                name,
                mod,
                to=text(mapper.get("to") or "")[0],
                subject=text(mapper.get("subject") or "")[0],
                body=text(mapper.get("html") or mapper.get("text") or mapper.get("content") or "")[
                    0
                ],
            )
        if mod == "util:FunctionSleep":
            return [
                b.add(
                    Step(
                        b.new_id(name),
                        name,
                        "wait",
                        mod,
                        TRANSLATED,
                        "",
                        {"seconds": float(mapper.get("duration") or 1)},
                    )
                )
            ]
        if mod in ("builtin:BasicFeeder", "builtin:BasicAggregator", "util:TextAggregator"):
            return [
                add_placeholder(
                    b,
                    name,
                    mod,
                    "Iterator/Aggregator: als Multi-Instance (for_each) "
                    "nachbauen; die Module dazwischen laufen hier einmal.",
                    APPROXIMATED,
                )
            ]
        app = mod.split(":")[0]
        hint = MCP_HINTS.get(app.replace("-", "").replace("google", "google"), "")
        return [
            add_placeholder(
                b,
                name,
                mod,
                f"Modul {mod} ohne Entsprechung. "
                + (hint or "Über einen MCP-Konnektor oder ein HTTP-Werkzeug nachbauen.")
                + " Einstellungen: "
                + _excerpt(mapper, 300),
            )
        ]

    def make_filter(m: dict) -> tuple[list[tuple[str, str]], str, str]:
        f = m.get("filter") or {}
        groups = [
            [(single_path(text(c.get("a"))[0]), c.get("o"), text(c.get("b"))[0]) for c in g]
            for g in f.get("conditions") or []
        ]
        return route_from_groups(b, f.get("name") or "Filter", "filter", groups or [[]])

    def run(modules: list[dict], after: str | None, route_head: bool = False) -> str | None:
        """Translate a list of modules; returns the first step id. The
        first module of a route has its filter on the router already."""
        first_id: str | None = None
        prev_tail: Step | None = None
        for i, m in enumerate(modules):
            mod = str(m.get("module") or "")
            if mod == "builtin:BasicRouter":
                sid = b.new_id("Router")
                r = b.add(Step(sid, "Router", "route", mod))
                remaining = run(modules[i + 1 :], after)
                notes = []
                for k, route in enumerate(m.get("routes") or []):
                    rflow = route.get("flow") or []
                    if rflow and rflow[0].get("filter"):
                        conds, match, note = make_filter(rflow[0])
                        if note:
                            notes.append(note)
                        r.branches.append(
                            Branch(
                                slug(
                                    (rflow[0].get("filter") or {}).get("name") or f"route_{k + 1}",
                                    f"route_{k + 1}",
                                ),
                                conds,
                                match,
                                run(rflow, remaining, True),
                            )
                        )
                    elif r.default is None:
                        r.default = Branch("otherwise", [], target=run(rflow, remaining))
                    else:
                        notes.append(
                            f"Route {k + 1} ohne Filter nach der ersten:"
                            " nur eine Route ohne Filter möglich"
                        )
                        r.branches.append(
                            Branch(
                                f"route_{k + 1}",
                                [("trigger.imported_condition", '"von Hand setzen"')],
                                "all",
                                run(rflow, remaining),
                            )
                        )
                if r.default is None:
                    r.default = Branch("otherwise", [], target=remaining)
                notes.append("Make führt alle passenden Routen aus, hier die erste")
                b.set_status(r, APPROXIMATED, "; ".join(notes))
                if prev_tail is not None:
                    prev_tail.next = sid
                return first_id or sid
            if m.get("filter") and not (route_head and i == 0):
                conds, match, note = make_filter(m)
                fid = b.new_id(f"Filter {(m.get('filter') or {}).get('name') or ''}")
                fstep = b.add(
                    Step(
                        fid,
                        f"Filter: {(m.get('filter') or {}).get('name') or ''}".strip(),
                        "route",
                        "filter",
                        APPROXIMATED if note else TRANSLATED,
                        note,
                    )
                )
                fstep.default = Branch("no", [], target="filtered")
                fbranch = Branch("yes", conds, match, None)
                fstep.branches = [fbranch]
                if prev_tail is not None:
                    prev_tail.next = fid
                first_id = first_id or fid
                steps = module_steps(m)
                by_id[str(m.get("id"))] = steps[-1].id if steps else fid
                if steps:
                    fbranch.target = steps[0].id
                    prev_tail = steps[-1]
                else:
                    prev_tail = None
                    fbranch.target = after  # replaced below by the next module
                    pending_filter.append(fbranch)
                continue
            steps = module_steps(m)
            if not steps:
                continue
            by_id[str(m.get("id"))] = steps[-1].id
            for fb in pending_filter:
                fb.target = steps[0].id
            pending_filter.clear()
            if prev_tail is not None and prev_tail.next is None:
                prev_tail.next = steps[0].id
            first_id = first_id or steps[0].id
            prev_tail = steps[-1]
        if prev_tail is not None and prev_tail.next is None:
            prev_tail.next = after
        return first_id or after

    pending_filter: list[Branch] = []
    start.next = run(rest, None)


# -- Zapier ------------------------------------------------------------------
#
# A Zap export (Zapfile.json from "Export my data", or the Team/Enterprise
# "Export Zaps") holds ``zaps[]``; each zap has ``nodes`` keyed by id with
# ``parent_id`` (the step before), ``selected_api`` (the app), ``action``
# and ``params``. The trigger is the node without a parent. A Paths step is
# a branching node whose children are one filter node per path; each path's
# steps hang under its filter. References read ``{{<node id>__field__sub}}``.

_ZAP_EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)
_ZAP_OPS = {
    # Zapier's filter match names onto the shared comparison table
    "exact": "equals",
    "iexact": "equals",
    "text_exact": "equals",
    "stop:exact": "notequals",
    "not_exact": "notequals",
    "not_iexact": "notequals",
    "gt": "gt",
    "greater_than": "gt",
    "number_gt": "gt",
    "lt": "lt",
    "less_than": "lt",
    "number_lt": "lt",
    "number_exact": "equals",
    "bool_true": "true",
    "is_true": "true",
    "bool_false": "false",
    "is_false": "false",
    "exists": "exists",
    "not_exists": "notexists",
    "does_not_exist": "notexists",
}
_ZAP_AI = re.compile(
    r"^(ChatGPT|OpenAI|Anthropic|Claude|GoogleAIStudio|GoogleGemini|Gemini|Mistral|"
    r"ZapierAI|AIbyZapier|AiByZapier|Perplexity)",
    re.I,
)
_ZAP_MAIL = re.compile(r"^(GoogleMail|Gmail|Outlook|MicrosoftOutlook|Email|SMTP|ZapierMail)", re.I)
_ZAP_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ZAPIER_MCP_HINT = (
    "Zapier MCP (mcp.zapier.com): die Aktion dort freigeben und als MCP-Werkzeug"
    " binden - jeder Aufruf kostet 2 Zapier-Tasks."
)


def _zap_app(api: str) -> str:
    """``SlackCLIAPI`` / ``GoogleMailV2CLIAPI`` / ``App64450CLIAPI`` → a
    short app name for the report."""
    name = re.sub(r"(V\d+)?(CLI)?API$", "", api or "")
    return name or "App"


def _zap_time(value) -> tuple[str, bool]:
    """'09:00', '9', '9am', '21' → ('HH:MM', exact)."""
    text = str(value or "").strip().lower()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not m:
        return "08:00", not text
    hour, minute = int(m.group(1)) % 24, int(m.group(2) or 0)
    if m.group(3) == "pm" and hour < 12:
        hour += 12
    if m.group(3) == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}", True


def _zap_nodes(zap: dict) -> dict[str, dict]:
    nodes = zap.get("nodes")
    if isinstance(nodes, dict):
        items = list(nodes.items())
    elif isinstance(nodes, list):
        items = [(n.get("id"), n) for n in nodes if isinstance(n, dict)]
    elif isinstance(zap.get("steps"), list):
        # the API's step list: a straight run, no parents in the data
        items, prev = [], None
        for n in zap["steps"]:
            if isinstance(n, dict):
                n = {**n, "parent_id": n.get("parent_id", prev)}
                items.append((n.get("id"), n))
                prev = n.get("id")
    else:
        items = []
    return {str(k): v for k, v in items if isinstance(v, dict) and k is not None}


def _zapier(data: dict, b: Builder) -> None:
    zaps = data.get("zaps") if isinstance(data.get("zaps"), list) else [data]
    zaps = [z for z in zaps if isinstance(z, dict)]
    if not zaps:
        raise WorkflowImportError("zapier: no zap in the export")
    index = _int(data.get("_zap_index"), 0)
    if not 0 <= index < len(zaps):
        raise WorkflowImportError(f"zapier: zap {index} not in the export ({len(zaps)} zaps)")
    zap = zaps[index]
    for k, other in enumerate(zaps):
        if k != index:
            b.skip(
                str(other.get("title") or f"Zap {k + 1}"),
                "zap",
                f"weiterer Zap im Export (Nr. {k}) - einzeln importieren",
            )
    nodes = _zap_nodes(zap)
    if not nodes:
        raise WorkflowImportError("zapier: the zap has no steps")
    children: dict[str, list[str]] = {}
    roots = []
    for nid, n in nodes.items():
        parent = n.get("parent_id")
        if parent in (None, "", 0) or str(parent) not in nodes:
            roots.append(nid)
        else:
            children.setdefault(str(parent), []).append(nid)

    def order(nid: str) -> tuple:
        n = nodes[nid]
        return (
            _int((n.get("params") or {}).get("path_eval_index"), 0)
            if isinstance(n.get("params"), dict)
            else 0,
            _int(n.get("id"), 0),
            nid,
        )

    for kids in children.values():
        kids.sort(key=order)
    roots.sort(key=order)
    root = roots[0]
    refs = Refs(b)
    by_id: dict[str, str] = {}

    def resolve(expr: str) -> str:
        parts = [p for p in expr.strip().split("__") if p]
        if not parts:
            return ""
        nid, rest = parts[0], parts[1:]
        if nid == root:
            return refs.trigger(rest)
        if nid in by_id:
            return refs.step(by_id[nid], rest)
        return ""

    def text(value) -> tuple[str, bool]:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        if not isinstance(value, str):
            return ("" if value is None else str(value)), True
        return substitute(value, _ZAP_EXPR, resolve)

    def title(n: dict) -> str:
        meta = n.get("meta") or {}
        return str(
            n.get("title")
            or (meta.get("stepTitle") if isinstance(meta, dict) else "")
            or f"{_zap_app(str(n.get('selected_api') or ''))} {n.get('action') or ''}".strip()
        )

    # the trigger --------------------------------------------------------
    t = nodes[root]
    api, action = str(t.get("selected_api") or ""), str(t.get("action") or "")
    params = t.get("params") if isinstance(t.get("params"), dict) else {}
    cfg: dict = {"trigger": "manual"}
    status, note = TRANSLATED, ""
    if api.startswith("Schedule"):
        at, exact = _zap_time(params.get("time_of_day"))
        a = action.lower()
        if "week" in a:
            day = str(params.get("day_of_week") or "monday").strip().lower()
            if day.isdigit():
                weekday, exact = _ZAP_DAYS[(int(day) - 1) % 7], False
            else:
                weekday = next((d for d in _ZAP_DAYS if d.startswith(day[:3])), "monday")
            cfg = {
                "trigger": "schedule",
                "schedule": {"every": "week", "weekday": weekday, "at": at},
            }
        elif "month" in a:
            day = min(max(_int(params.get("day_of_month"), 1), 1), 28)
            exact = exact and _int(params.get("day_of_month"), 1) <= 28
            cfg = {"trigger": "schedule", "schedule": {"every": "month", "day": day, "at": at}}
        elif "day" in a:
            cfg = {"trigger": "schedule", "schedule": {"every": "day", "at": at}}
            if str(params.get("trigger_on_weekends", "")).lower() in ("no", "false", "0"):
                exact, note = False, "Zapier ohne Wochenende - hier täglich."
        else:
            cfg = {"trigger": "schedule", "schedule": {"every": "day", "at": "08:00"}}
            exact, note = False, f"Zeitplan {action or api} auf täglich 08:00 angenähert."
        status = TRANSLATED if exact else APPROXIMATED
        if not exact and not note:
            note = "Zeitangabe angenähert - prüfen."
    elif api.startswith("WebHook"):
        status, note = APPROXIMATED, "Webhook → manueller Start (Startformular/Intake-Kanal)."
    else:
        status, note = (
            APPROXIMATED,
            f"Auslöser {_zap_app(api)} {action} → manueller Start (Kanal von Hand). "
            + (MCP_HINTS.get(_zap_app(api).lower(), "")),
        )
    start = b.add(
        Step(
            b.new_id(title(t) or "Start"),
            title(t) or "Start",
            "start",
            api,
            status,
            note.strip(),
            cfg,
        )
    )

    # actions ---------------------------------------------------------------
    def path_conditions(n: dict) -> tuple[list[tuple[str, str]], str, str]:
        p = n.get("params") if isinstance(n.get("params"), dict) else {}
        criteria = p.get("filter_criteria") or []
        groups: dict[str, list[tuple[str, str, object]]] = {}
        for c in criteria if isinstance(criteria, list) else []:
            if not isinstance(c, dict):
                continue
            key = str(c.get("key") or "")
            if "{{" not in key:
                key = "{{" + key + "}}"
            op = str(c.get("match") or "")
            groups.setdefault(str(c.get("group") or "0"), []).append(
                (single_path(text(key)[0]), _ZAP_OPS.get(op.lower(), op), text(c.get("value"))[0])
            )
        return route_from_groups(b, "Filter", "filter", list(groups.values()) or [[]])

    def filter_route(n: dict, name: str) -> Step:
        conds, match, why = path_conditions(n)
        step = b.add(
            Step(
                b.new_id(name),
                name,
                "route",
                str(n.get("selected_api") or "FilterAPI"),
                APPROXIMATED if why else TRANSLATED,
                why,
            )
        )
        step.branches = [Branch("yes", conds, match, None)]
        return step

    def node_steps(nid: str) -> list[Step]:
        n = nodes[nid]
        api = str(n.get("selected_api") or "")
        action = str(n.get("action") or "")
        p = n.get("params") if isinstance(n.get("params"), dict) else {}
        name = title(n)
        app = _zap_app(api)
        if api.startswith("WebHook"):
            url, ok = text(p.get("url") or "")
            method = str(p.get("method") or action or "post")
            if method.lower() in ("custom", "custom_request"):
                method = str(p.get("method") or "post")
            headers = p.get("headers") if isinstance(p.get("headers"), dict) else {}
            data_ = p.get("data")
            body = None
            if isinstance(data_, dict):
                body = {str(k): text(v)[0] for k, v in data_.items()}
            elif data_:
                body = text(data_)[0]
            query = {}
            if method.lower() == "get" and isinstance(p.get("params"), dict):
                query = {str(k): text(v)[0] for k, v in p["params"].items()}
            return add_http(
                b,
                name,
                api,
                method=method,
                url=url,
                headers={str(k): text(v)[0] for k, v in headers.items()},
                query=query,
                body=body,
                secret_hint=f"{slug(name)}_token" if p.get("basic_auth") else "",
                status=TRANSLATED if ok else APPROXIMATED,
            )
        if _ZAP_MAIL.match(app) and re.search(r"send|message|mail", action, re.I):
            return add_mail(
                b,
                name,
                api,
                to=text(p.get("to") or p.get("recipient") or "")[0],
                subject=text(p.get("subject") or "")[0],
                body=text(p.get("body") or p.get("html") or p.get("text") or "")[0],
            )
        if _ZAP_AI.match(app):
            prompt, ok = text(
                p.get("user_message") or p.get("prompt") or p.get("message") or p.get("input") or ""
            )
            system, _ = text(p.get("system_message") or p.get("instructions") or "")
            steps_in = sorted(set(re.findall(r"\{steps\.([a-z0-9_]+)", prompt)))
            prompt = re.sub(
                r"\{steps\.([a-z0-9_]+)((?:\.[A-Za-z0-9_]+)*)\}",
                lambda x: f"(Ergebnis von Schritt {x.group(1)}{x.group(2)})",
                prompt,
            )
            return [
                add_ai(
                    b,
                    name,
                    api,
                    prompt=prompt,
                    system=system,
                    context_steps=steps_in,
                    status=TRANSLATED if ok else APPROXIMATED,
                    model=str(p.get("model") or ""),
                )
            ]
        if api.startswith("Delay"):
            if "until" in action.lower():
                return [
                    add_placeholder(
                        b,
                        name,
                        api,
                        f"Warten bis {p.get('until') or '?'} - als Timer mit until_from nachbauen.",
                        APPROXIMATED,
                    )
                ]
            unit = str(p.get("unit") or "minutes").lower()
            factor = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800}
            seconds = _int(p.get("value") or p.get("delay_for"), 1) * factor.get(
                unit if unit.endswith("s") else unit + "s", 60
            )
            return [
                b.add(Step(b.new_id(name), name, "wait", api, TRANSLATED, "", {"seconds": seconds}))
            ]
        if api.startswith(("Code", "App64450")):
            return [
                add_placeholder(
                    b,
                    name,
                    api,
                    "Code-Schritt: nicht übernommen (kein fremder Code). Als Briefing,"
                    " Entscheidung (DMN) oder interne Aktion nachbauen. Code: "
                    + _excerpt(p.get("code") or "", 300),
                )
            ]
        if api.startswith("ZapierFormatter"):
            return [
                add_placeholder(
                    b,
                    name,
                    api,
                    f"Formatter ({action}): Vorlage-Bindung template:{{…}} oder Briefing. "
                    + _excerpt(p, 300),
                    APPROXIMATED,
                )
            ]
        if api.startswith("ZapierLooping"):
            return [
                add_placeholder(
                    b,
                    name,
                    api,
                    "Schleife: als Multi-Instance (for_each) nachbauen;"
                    " die Schritte danach laufen hier einmal.",
                    APPROXIMATED,
                )
            ]
        if api.startswith("SubZap"):
            return [
                add_placeholder(
                    b,
                    name,
                    api,
                    "Sub-Zap: den gerufenen Zap einzeln importieren und als callActivity binden.",
                    APPROXIMATED,
                )
            ]
        hint = MCP_HINTS.get(app.lower().replace("google", "google"), "")
        return [
            add_placeholder(
                b,
                name,
                api,
                f"Zapier-App {app} ({action}) ohne Entsprechung. "
                + (hint + " " if hint else "")
                + ZAPIER_MCP_HINT
                + " Einstellungen: "
                + _excerpt(p, 300),
            )
        ]

    def run(nid: str, after: str | None) -> str | None:
        """Translate the node, then what hangs under it (references point
        backwards, so a step exists before the steps that read it);
        returns its first step id."""
        n = nodes[nid]
        api = str(n.get("selected_api") or "")
        action = str(n.get("action") or "")
        kids = children.get(nid, [])
        if api.startswith("Branching") or action == "branch":
            r = b.add(Step(b.new_id(title(n) or "Paths"), title(n) or "Paths", "route", api))
            notes = ["Zapier führt alle passenden Pfade aus, hier der erste"]
            for k, kid in enumerate(kids):
                kn = nodes[kid]
                p = kn.get("params") if isinstance(kn.get("params"), dict) else {}
                inner = children.get(kid, [])
                if p.get("filter_criteria"):
                    conds, match, why = path_conditions(kn)
                    if why:
                        notes.append(why)
                    branch = Branch(
                        slug(title(kn) or f"path_{k + 1}", f"path_{k + 1}"), conds, match
                    )
                    r.branches.append(branch)
                elif r.default is None:
                    branch = r.default = Branch("otherwise", [])
                else:
                    notes.append(f"Pfad {k + 1} ohne Regel - Bedingung von Hand setzen")
                    branch = Branch(
                        f"path_{k + 1}",
                        [("trigger.imported_condition", '"von Hand setzen"')],
                        "all",
                    )
                    r.branches.append(branch)
                branch.target = run(inner[0], after) if inner else after
            if r.default is None:
                r.default = Branch("otherwise", [], target=after)
            b.set_status(r, APPROXIMATED, "; ".join(notes))
            return r.id
        if len(kids) > 1:
            b.skip(title(n), api, "mehrere Nachfolger ohne Paths - nur der erste übernommen")
        if api.startswith("Filter"):
            f = filter_route(n, title(n) or "Filter")
            f.default = Branch("no", [], target="filtered")
            f.branches[0].target = run(kids[0], after) if kids else after
            return f.id
        steps = node_steps(nid)
        by_id[nid] = steps[-1].id
        for prev, cur in itertools.pairwise(steps):
            prev.next = prev.next or cur.id
        steps[-1].next = run(kids[0], after) if kids else after
        return steps[0].id

    kids = children.get(root, [])
    start.next = run(kids[0], None) if kids else None


# -- entry point ---------------------------------------------------------------


def detect(data) -> str:
    if isinstance(data, dict):
        if isinstance(data.get("zaps"), list) or (
            isinstance(data.get("nodes"), dict)
            and any(isinstance(n, dict) and "selected_api" in n for n in data["nodes"].values())
        ):
            return "zapier"
        if isinstance(data.get("steps"), list) and any(
            isinstance(n, dict) and "selected_api" in n for n in data["steps"]
        ):
            return "zapier"
        if isinstance(data.get("nodes"), list) and "connections" in data:
            return "n8n"
        if isinstance(data.get("flow"), list):
            return "make"
        if any(k in data for k in ("trigger", "template", "flows")) or isinstance(
            data.get("version"), dict
        ):
            return "activepieces"
    raise WorkflowImportError(
        "unknown export format (expected n8n, Activepieces, Make or Zapier)"
    )


def export_text(raw: bytes) -> str:
    """The JSON of an upload - a Zapier "Export my data" ZIP gives its
    Zapfile; the archive is only listed and one member read, size capped."""
    if raw[:2] != b"PK":
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise WorkflowImportError("export is neither JSON text nor a ZIP") from exc
    import io
    import zipfile

    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise WorkflowImportError(f"broken ZIP: {exc}") from exc
    members = [i for i in archive.infolist() if i.filename.lower().endswith(".json")]
    members.sort(key=lambda i: (0 if "zapfile" in i.filename.lower() else 1, i.filename))
    if not members:
        raise WorkflowImportError("the ZIP holds no JSON export")
    if members[0].file_size > MAX_EXPORT_BYTES:
        raise WorkflowImportError("export too large (2 MB at most)")
    with archive.open(members[0]) as fh:
        data = fh.read(MAX_EXPORT_BYTES + 1)
    if len(data) > MAX_EXPORT_BYTES:
        raise WorkflowImportError("export too large (2 MB at most)")
    return data.decode("utf-8-sig")


def translate(
    text: str, *, source: str = "", process_id: str = "", index: int = 0
) -> ImportResult:
    """``index`` picks the zap when a Zapier export holds several."""
    if len(text.encode("utf-8")) > MAX_EXPORT_BYTES:
        raise WorkflowImportError("export too large (2 MB at most)")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise WorkflowImportError(f"not JSON: {exc}") from exc
    source = source or detect(data)
    if source not in SOURCES:
        raise WorkflowImportError(f"unknown source {source!r}")
    zap_title = ""
    if source == "zapier" and isinstance(data, dict):
        data = {**data, "_zap_index": index}
        zaps = data.get("zaps")
        if isinstance(zaps, list) and 0 <= index < len(zaps) and isinstance(zaps[index], dict):
            zap_title = str(zaps[index].get("title") or "")
        else:
            zap_title = str(data.get("title") or "")
    title = str(
        zap_title
        or (data.get("name") if isinstance(data, dict) else "")
        or (data.get("displayName") if isinstance(data, dict) else "")
        or (
            (data.get("template") or {}).get("displayName")
            if isinstance(data, dict) and isinstance(data.get("template"), dict)
            else ""
        )
        or f"Import aus {source}"
    )
    pid = process_id or slug(title, "imported").replace("_", "-")
    if not re.match(r"^[a-z][a-z0-9_-]*$", pid):
        raise WorkflowImportError(f"process id {pid!r} is not a lowercase identifier")
    b = Builder(source, pid, title[:120])
    {"n8n": _n8n, "activepieces": _activepieces, "make": _make, "zapier": _zapier}[source](
        data, b
    )
    roles = [MODEL_ROLE] if b.briefings else []
    return ImportResult(source, pid, b.title, b.files(), b.report, b.secrets, b.inputs, roles)
