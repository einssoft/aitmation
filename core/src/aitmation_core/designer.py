"""The process designer's vocabulary (ADR-0062): what can be placed on the
canvas, what each building block asks for, and which data a step can
use at its place in the graph.

Nothing here runs or writes. ``catalog`` lists the building blocks a
solution has in sight - triggers, AI tasks, people, the core's own
actions, the solution's tools (HTTP, MCP, client, fetch, search),
decision tables and processes - each with the settings the designer
shows as a form. ``paths`` walks the graph backwards from a step and
lists every value it can bind: the start form's fields, the outputs of
the steps before it, the solution's constants and the case itself.

The schemas of the core actions live here, next to the code that runs
them (runtime.py); a test keeps the two in step.
"""

from __future__ import annotations

from collections import deque

# The core's actions as building blocks. ``config`` are settings of the
# step itself (aimn:property), ``inputs`` are values bound per case
# (paths, ``literal:`` or ``template:``), ``outputs`` what the step's
# result carries. ``effect`` external = needs a gate or mandate before it
# (ADR-0007), the designer says so on the card.
ACTION_SCHEMAS: dict[str, dict] = {
    "mail": {
        "group": "send",
        "effect": "external",
        "config": {"case_files": {"type": "bool", "label": "attach the case's files"}},
        "inputs": {
            "to": {"required": True, "label": "to"},
            "subject": {"required": True, "label": "subject"},
            "body": {"required": True, "label": "text", "multiline": True},
            "cc": {"label": "cc"},
            "reply_to": {"label": "reply to"},
            "in_reply_to": {"label": "in reply to (message id)"},
            "attachments": {"label": "attachments (file names of the case)"},
        },
        "outputs": {"to": "recipients", "cc": "copy", "subject": "subject sent"},
    },
    "record": {
        "group": "data",
        "effect": "internal",
        "config": {"collection": {"type": "collection", "required": True, "label": "collection"}},
        "inputs": {},  # the collection's fields - filled in by ``catalog``
        "inputs_from": "collection",
        "outputs": {"collection": "collection", "row_id": "row written", "version": "row version"},
    },
    "rows": {
        "group": "data",
        "effect": "internal",
        "config": {
            "collection": {"type": "collection", "required": True, "label": "collection"},
            "limit": {"type": "int", "label": "at most", "default": 50},
        },
        "inputs": {},
        "outputs": {"collection": "collection", "count": "rows found", "items": "the rows (list)"},
    },
    "knowledge": {
        "group": "data",
        "effect": "internal",
        "config": {"space": {"type": "space", "required": True, "label": "knowledge space"}},
        "inputs": {
            "text": {"required": True, "label": "text", "multiline": True},
            "title": {"label": "title"},
            "name": {"label": "file name"},
        },
        "outputs": {"space": "space", "document": "document id", "name": "file name"},
    },
    "links": {
        "group": "data",
        "effect": "internal",
        "config": {"max": {"type": "int", "label": "at most", "default": 5}},
        "inputs": {"text": {"label": "text (empty = the case's documents)"}},
        "outputs": {"urls": "addresses found (list)", "count": "how many", "note": "note"},
    },
    "pdf": {
        "group": "files",
        "effect": "internal",
        "config": {},
        "inputs": {
            "title": {"required": True, "label": "title"},
            "text": {"required": True, "label": "text", "multiline": True},
            "name": {"label": "file name without extension"},
        },
        "outputs": {"file": "the PDF's file name in the case"},
    },
    "file": {
        "group": "files",
        "effect": "internal",
        "config": {"media": {"type": "bool", "label": "file what the case made (PDF) instead"}},
        "inputs": {
            "folder": {"required": True, "label": "folder (year/area/…)"},
            "name": {"label": "file name without extension"},
        },
        "outputs": {"filed": "paths in the filing folder (list)", "folder": "folder used"},
    },
    "figures": {
        "group": "data",
        "effect": "internal",
        "config": {"days": {"type": "int", "label": "days back", "default": 7}},
        "inputs": {},
        "outputs": {
            "cases": "case figures",
            "gates": "decisions",
            "errors": "errors",
            "models": "model calls and cost",
            "per_process": "per process",
        },
    },
    "archive": {
        "group": "files",
        "effect": "internal",
        "config": {},
        "inputs": {},
        "outputs": {"archived_to": "archive folder"},
    },
    "export": {
        "group": "files",
        "effect": "internal",
        "config": {},
        "inputs": {},
        "outputs": {"exported_to": "export folder"},
    },
}

TRIGGERS: dict[str, dict] = {
    "manual": {"config": {}},
    "schedule": {
        "config": {
            "every": {
                "type": "choice",
                "choices": ["day", "week", "month"],
                "required": True,
                "label": "every",
            },
            "at": {"type": "time", "label": "at (HH:MM)", "default": "08:00"},
            "weekday": {"type": "int", "label": "weekday (1 = Monday)"},
            "day": {"type": "int", "label": "day of month (1-28)"},
        }
    },
    "mailbox": {"config": {}},
    "folder": {"config": {}},
    "chain": {"config": {}},
}

GATE_CONFIG = {
    "approving": {"type": "text", "label": "outcomes that release (comma separated)"},
    "rule": {"type": "text", "label": "facts that make two decisions the same (for rules)"},
}

OUTCOMES = {
    "ai": ("done", "failed"),
    "gate": ("approved", "rejected"),
    "action": ("done", "failed"),
    "decision": ("done",),
    "call": ("done", "failed"),
    "timer": ("elapsed",),
}


def _text(value, lang: str) -> str:
    if isinstance(value, dict):
        return str(value.get(lang) or value.get("de") or next(iter(value.values()), ""))
    return str(value or "")


def _in_sight(app, attr: str, apps: dict) -> dict:
    """The solution's own elements plus the root elements of its
    dependencies (they provide them, ADR-0035)."""
    out: dict = {}
    for dep in getattr(app.manifest, "dependencies", {}) or {}:
        other = apps.get(dep)
        if other is not None:
            out.update(getattr(other, attr, {}) or {})
    out.update(getattr(app, attr, {}) or {})
    return out


def _briefing_outputs(briefing) -> dict:
    """What an AI step's result carries: its declared output, and with
    cited knowledge the hits and passages (ADR-0056)."""
    out = dict(getattr(briefing, "output", {}) or {})
    context = getattr(briefing, "context", {}) or {}
    extra = getattr(briefing, "extra", {}) or {}
    if context.get("knowledge") and extra.get("knowledge_cite"):
        out.setdefault("knowledge_hits", "passages found")
        out.setdefault("knowledge_sources", "passages retrieved (n, title, url, text, score)")
    return out


def _tool_kind(tool) -> str:
    if tool.exec == "client":
        return "client"
    return tool.connector or tool.action or "core"


def catalog(
    app, *, apps: dict, records: dict, spaces: list, processes: dict, lang: str = "de"
) -> dict:
    """Every building block the designer offers for this solution."""
    collections = {
        sid: {
            "title": schema.title_in(lang),
            "fields": [
                {"name": name, "required": spec.required, "label": _text(spec.label, lang) or name}
                for name, spec in schema.fields.items()
            ],
        }
        for sid, schema in records.items()
        if getattr(schema, "kind", "") == "collection"
    }
    actions = []
    for action_id, schema in ACTION_SCHEMAS.items():
        actions.append({"id": action_id, **schema})
    tools = []
    for tool_id, tool in sorted(_in_sight(app, "tools", apps).items()):
        tools.append(
            {
                "id": tool_id,
                "name": _text(tool.name, lang) or tool_id,
                "description": _text(tool.description, lang),
                "kind": _tool_kind(tool),
                "effect": tool.effect,
                "inputs": {k: {"label": v, "required": True} for k, v in tool.inputs.items()},
                "outputs": dict(tool.outputs),
                "on_error": tool.on_error,
            }
        )
    briefings = []
    for bid, briefing in sorted(_in_sight(app, "briefings", apps).items()):
        briefings.append(
            {
                "id": bid,
                "role": _text(getattr(briefing, "role", ""), lang)[:160],
                "model_role": getattr(briefing, "model_role", "") or "",
                "outputs": _briefing_outputs(briefing),
            }
        )
    decisions = []
    for did, decision in sorted(_in_sight(app, "decisions", apps).items()):
        decisions.append(
            {
                "id": did,
                "name": getattr(decision, "name", did) or did,
                "inputs": [c.expression for c in getattr(decision, "inputs", ())],
                "outputs": [c.name for c in getattr(decision, "outputs", ())],
            }
        )
    called = []
    for pid, definition in sorted(processes.items()):
        contract = getattr(definition, "contract", {}) or {}
        called.append(
            {
                "id": pid,
                "title": _text((getattr(definition, "meta", {}) or {}).get("title"), lang) or pid,
                "inputs": dict(contract.get("input") or {}),
            }
        )
    return {
        "triggers": [{"id": k, **v} for k, v in TRIGGERS.items()],
        "actions": actions,
        "tools": tools,
        "briefings": briefings,
        "decisions": decisions,
        "processes": called,
        "collections": collections,
        "spaces": [
            {"id": s.id, "title": _text(getattr(s, "title", ""), lang) or s.id} for s in spaces
        ],
        "constants": _constant_names(app),
        "gate": GATE_CONFIG,
        "outcomes": {k: list(v) for k, v in OUTCOMES.items()},
    }


def _constant_names(app) -> list[str]:
    names: list[str] = []
    for key, value in (getattr(app, "constants", {}) or {}).items():
        _flatten(key, value, names)
    return names[:200]


def upstream(graph: dict, node_id: str) -> list[str]:
    """Every step a flow can come from before ``node_id``, nearest first."""
    preds: dict[str, list[str]] = {}
    for edge in graph.get("edges", []):
        preds.setdefault(edge["target"], []).append(edge["source"])
    seen: list[str] = []
    queue = deque(preds.get(node_id, []))
    while queue:
        current = queue.popleft()
        if current in seen or current == node_id:
            continue
        seen.append(current)
        queue.extend(preds.get(current, []))
    return seen


def _flatten(prefix: str, value, out: list[str], depth: int = 0) -> None:
    if isinstance(value, dict) and depth < 3:
        for key, inner in value.items():
            _flatten(f"{prefix}.{key}", inner, out, depth + 1)
    else:
        out.append(prefix)


def paths(
    graph: dict, node_id: str, *, app, apps: dict, records: dict, processes: dict, lang: str = "de"
) -> list[dict]:
    """The values a step can bind: ``path`` is what goes into an input
    (``trigger.x``, ``steps.y.z``, ``const:a.b``, ``case.id``); a
    template uses it in braces."""
    out: list[dict] = []
    current = next((n for n in graph.get("nodes", []) if n["id"] == node_id), None)
    if current is not None and (current.get("loop") or {}).get("over"):
        out.append(
            {
                "path": "each",
                "group": "each",
                "label": "the current element",
                "description": f"one element of {current['loop']['over']}",
            }
        )
    for entry in graph.get("contract") or []:
        out.append(
            {
                "path": f"trigger.{entry['name']}",
                "group": "start",
                "label": entry["name"],
                "description": entry.get("description", ""),
            }
        )
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    tools = _in_sight(app, "tools", apps)
    briefings = _in_sight(app, "briefings", apps)
    decisions = _in_sight(app, "decisions", apps)
    for sid in upstream(graph, node_id):
        node = nodes.get(sid)
        if node is None or node["kind"] in ("start", "xor", "parallel", "end"):
            continue
        group = f"step:{sid}"
        title = node.get("label") or sid
        fields: dict[str, str] = {}
        kind, ref = node["kind"], node.get("ref", "")
        if kind == "ai" and ref in briefings:
            fields = _briefing_outputs(briefings[ref])
        elif kind == "action" and ref in ACTION_SCHEMAS:
            fields = dict(ACTION_SCHEMAS[ref]["outputs"])
        elif kind == "action" and ref in tools:
            fields = dict(tools[ref].outputs) or {"text": "answer text", "data": "answer data"}
        elif kind == "decision" and ref in decisions:
            fields = {c.name: "decision output" for c in getattr(decisions[ref], "outputs", ())}
        elif kind == "gate":
            fields = {"comment": "the person's comment", "actor": "who decided"}
        loop = node.get("loop") or {}
        for name, description in fields.items():
            out.append(
                {
                    "path": f"steps.{sid}.{name}",
                    "group": group,
                    "label": f"{title} → {name}",
                    "description": str(description),
                    **({"list": True} if loop else {}),
                }
            )
        out.append(
            {
                "path": f"steps.{sid}.outcome",
                "group": group,
                "label": f"{title} → outcome",
                "description": "which way the step went",
            }
        )
    for name in _constant_names(app):
        out.append(
            {"path": f"const:{name}", "group": "constants", "label": name, "description": ""}
        )
    out.append({"path": "case.id", "group": "case", "label": "case id", "description": ""})
    return out
