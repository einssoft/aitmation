"""The workshop's library: agents and tools across all solutions.

Solutions are packages; people think in building blocks. This module
gives the block's view: every agent (briefing) and every tool of the
instance with where it comes from, whether it may be changed here and
which processes use it - and the declarations for new blocks made from
a short form.

Blocks people make themselves land in one solution of the instance,
``own`` ("Your building blocks"), created on first use. It is an
ordinary format-2 solution: validated, tested and exported like any
other; packing a sellable solution later moves blocks out of it.

Nothing here executes anything. An agent stays a declared step: tools
that read it may use itself, a tool with an outside effect runs only in
a process behind a gate (ADR-0007).
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

OWN = "own"
OWN_NAMES = {
    "de": "Eigene Bausteine",
    "fr": "Vos éléments",
    "it": "I suoi elementi",
    "en": "Your building blocks",
}
OWN_DESCRIPTIONS = {
    "de": "Agenten, Werkzeuge und Abläufe, die in dieser Instanz entstanden sind.",
    "fr": "Agents, outils et processus créés dans cette instance.",
    "it": "Agenti, strumenti e processi creati in questa istanza.",
    "en": "Agents, tools and processes made in this instance.",
}

# The one output field a new agent starts with, described in its maker's language.
RESULT_FIELD = {
    "de": "das Ergebnis als Text",
    "fr": "le résultat sous forme de texte",
    "it": "il risultato come testo",
    "en": "the result, as text",
}

# The kinds a person picks a tool by (the workshop's tiles), in the
# order they are offered.
TOOL_KINDS = ("page", "search", "json", "api", "mcp", "client", "action")
FORM_KINDS = ("page", "search", "json", "api")  # made from a form here

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_SECRET = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
_HEADER = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,59}$")


class LibraryError(ValueError):
    """A form that cannot become a declaration."""


def slug(name: str, fallback: str = "") -> str:
    """An element id from what a person typed as the name."""
    plain = (
        name.strip()
        .lower()
        .translate(
            str.maketrans(
                {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "é": "e", "è": "e", "à": "a"}
            )
        )
    )
    out = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:50].strip("-")
    if out and not out[0].isalpha():
        out = f"x-{out}"
    return out or fallback


def tool_kind(tool) -> str:
    """Which tile a declared tool belongs to."""
    if tool.exec == "client":
        return "client"
    if tool.connector == "fetch":
        return "json" if tool.fetch is not None and tool.fetch.format == "json" else "page"
    if tool.connector == "search":
        return "search"
    if tool.connector == "http":
        return "api"
    if tool.connector == "mcp":
        return "mcp"
    return "action"


def _owner(apps: dict, app, attr: str, element_id: str) -> str | None:
    """The solution an id resolves to from ``app``: its own element, else
    the first dependency that provides it."""
    if element_id in getattr(app, attr):
        return app.id
    for dep in app.manifest.dependencies:
        other = apps.get(dep)
        if other is not None and element_id in getattr(other, attr):
            return dep
    return None


def usage(apps: dict) -> dict[tuple[str, str, str], list[dict]]:
    """``(kind, owning solution, id)`` -> where it is used: steps of
    processes, and (for tools) agents that may call them."""
    out: dict[tuple[str, str, str], list[dict]] = {}
    for app in apps.values():
        if getattr(app, "format", 2) != 2:
            continue
        for process_id, definition in app.templates.items():
            title = definition.meta.get("title") or process_id
            for step in definition.steps.values():
                where = {"solution": app.id, "process": process_id, "step": step.id, "title": title}
                briefing = (step.config.get("briefing") or {}).get("id")
                if briefing:
                    owner = _owner(apps, app, "briefings", briefing)
                    if owner:
                        out.setdefault(("ai", owner, briefing), []).append(where)
                tool = step.config.get("tool")
                if tool:
                    owner = _owner(apps, app, "tools", tool)
                    if owner:
                        out.setdefault(("tool", owner, tool), []).append(where)
        for briefing in app.briefings.values():
            for tool in briefing.tools:
                owner = _owner(apps, app, "tools", tool)
                if owner:
                    out.setdefault(("tool", owner, tool), []).append(
                        {"solution": app.id, "agent": briefing.id}
                    )
    return out


def agents(apps: dict, writable, names) -> list[dict]:
    """Every briefing of every format-2 solution, as the library lists it."""
    used = usage(apps)
    rows = []
    for app in apps.values():
        if getattr(app, "format", 2) != 2:
            continue
        for briefing in app.briefings.values():
            context = briefing.context or {}
            rows.append(
                {
                    "id": briefing.id,
                    "solution": app.id,
                    "solution_name": names(app),
                    "writable": writable(app),
                    "own": app.id == OWN,
                    "name": dict(briefing.name),
                    "description": dict(briefing.description) or dict(briefing.role),
                    "model_role": briefing.model_role,
                    "tools": list(briefing.tools),
                    "knowledge": [str(k) for k in context.get("knowledge") or []],
                    "outputs": list(briefing.output),
                    "used_in": used.get(("ai", app.id, briefing.id), []),
                }
            )
    rows.sort(key=lambda r: (not r["own"], r["solution"], r["id"]))
    return rows


def tools(apps: dict, writable, names) -> list[dict]:
    used = usage(apps)
    rows = []
    for app in apps.values():
        if getattr(app, "format", 2) != 2:
            continue
        for tool in app.tools.values():
            rows.append(
                {
                    "id": tool.id,
                    "solution": app.id,
                    "solution_name": names(app),
                    "writable": writable(app),
                    "own": app.id == OWN,
                    "name": dict(tool.name),
                    "description": dict(tool.description),
                    "kind": tool_kind(tool),
                    "effect": tool.effect,
                    "inputs": dict(tool.inputs),
                    "outputs": dict(tool.outputs),
                    "used_in": used.get(("tool", app.id, tool.id), []),
                }
            )
    rows.sort(key=lambda r: (not r["own"], r["solution"], r["id"]))
    return rows


# -- declarations from a form -----------------------------------------------------


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _one_line(text: str, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def _table(values: dict[str, str]) -> str:
    return "{ " + ", ".join(f"{_key(k)} = {_q(v)}" for k, v in values.items()) + " }"


def _key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _q(name)


def _https(url: str, what: str = "address") -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise LibraryError(f"{what}: a full https address")
    return url


def _names(values: list[str], what: str) -> list[str]:
    out = []
    for raw in values:
        name = str(raw).strip()
        if not name:
            continue
        if not _NAME.match(name):
            raise LibraryError(f"{what} {name!r}: letters, digits and _, starting with a letter")
        if name not in out:
            out.append(name)
    return out


def _mapping(values: dict[str, str], what: str) -> dict[str, str]:
    out = {}
    for raw, path in values.items():
        name = str(raw).strip()
        if not name:
            continue
        if not _NAME.match(name):
            raise LibraryError(f"{what} {name!r}: letters, digits and _, starting with a letter")
        out[name] = _one_line(path, 200)
    return out


def tool_toml(kind: str, tool_id: str, name: str, description: str, lang: str, form: dict) -> str:
    """A tool declaration from one of the workshop's forms.

    ``page``   reads one public web page (address comes as an input)
    ``search`` searches the web (``provider``, ``secret``, ``max``)
    ``json``   reads a public JSON address (``url``, ``params``, ``response``)
    ``api``    calls an API (``method``, ``url``, ``params``, ``header``,
               ``secret``, ``response``) - an outside effect, gated
    """
    if kind not in FORM_KINDS:
        raise LibraryError(f"kind must be one of {', '.join(FORM_KINDS)}")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,59}", tool_id):
        raise LibraryError("id: a-z, 0-9, - and _, starting with a letter")
    name = _one_line(name, 80)
    if not name:
        raise LibraryError("name is required")
    description = _one_line(description, 400) or name
    lang = lang if lang in ("de", "fr", "it", "en") else "de"
    lines = [
        f"id = {_q(tool_id)}",
        f"name = {_table({lang: name})}",
        f"description = {_table({lang: description})}",
    ]
    if kind == "page":
        lines += [
            'effect = "read"',
            'connector = "fetch"',
            'on_error = "failed"',
            "[inputs]",
            'url = "the address to read"',
            "[outputs]",
            'url = "the address actually read"',
            'title = "the page title"',
            'text = "the readable text"',
            "[fetch]",
            'url = "{input:url}"',
            "max_chars = 20000",
        ]
    elif kind == "search":
        provider = str(form.get("provider") or "duckduckgo")
        if provider not in ("duckduckgo", "brave", "tavily"):
            raise LibraryError("provider: duckduckgo, brave or tavily")
        secret = str(form.get("secret") or "").strip()
        if provider != "duckduckgo" and not _SECRET.match(secret):
            raise LibraryError("this provider needs the name of a tool secret (a-z, 0-9, _)")
        try:
            most = max(1, min(int(form.get("max") or 5), 10))
        except (TypeError, ValueError):
            raise LibraryError("max: a number from 1 to 10") from None
        lines += [
            'effect = "read"',
            'connector = "search"',
            'on_error = "failed"',
            "[inputs]",
            'query = "the search query, one line"',
            "[outputs]",
            'items = "list of hits: title, url, snippet"',
            'count = "number of hits"',
            "[search]",
            'query = "{input:query}"',
            f"provider = {_q(provider)}",
            f"max = {most}",
        ]
        if provider != "duckduckgo":
            lines.append(f"secret = {_q(secret)}")
    else:
        url = _https(str(form.get("url") or ""))
        params = _names(list(form.get("params") or []), "input")
        response = _mapping(dict(form.get("response") or {}), "output")
        inputs = ["[inputs]"] + [f"{p} = {_q(p)}" for p in params]
        if kind == "json":
            if "{" in url or "?" in url:
                raise LibraryError("address: fixed, without ? and without placeholders")
            if not response:
                raise LibraryError("say what comes back: at least one output with its path")
            lines += [
                'effect = "read"',
                'connector = "fetch"',
                'on_error = "failed"',
                *inputs,
                "[outputs]",
                *[f"{k} = {_q(v or k)}" for k, v in response.items()],
                "[fetch]",
                'format = "json"',
                f"url = {_q(url)}",
            ]
            if params:
                lines.append("query = " + _table({p: f"{{input:{p}}}" for p in params}))
            lines.append("response = " + _table(response))
        else:
            method = str(form.get("method") or "POST").upper()
            if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                raise LibraryError("method: GET, POST, PUT, PATCH or DELETE")
            in_url = set(re.findall(r"\{input:([A-Za-z0-9_]+)\}", url))
            if in_url - set(params):
                raise LibraryError(
                    f"address names inputs that are not listed: {sorted(in_url - set(params))}"
                )
            if re.search(r"\{(?!input:)", url):
                raise LibraryError("address: only {input:name} placeholders")
            outputs = response or {"status": "status"}
            lines += [
                'effect = "external"',
                'connector = "http"',
                'on_error = "failed"',
                *inputs,
                "[outputs]",
                *[f"{k} = {_q(v or k)}" for k, v in outputs.items()],
                "[http]",
                f"method = {_q(method)}",
                f"url = {_q(url)}",
            ]
            header, secret = (
                str(form.get("header") or "").strip(),
                str(form.get("secret") or "").strip(),
            )
            if secret:
                if not _SECRET.match(secret):
                    raise LibraryError("secret: the name of a tool secret (a-z, 0-9, _)")
                header = header or "Authorization"
                if not _HEADER.match(header):
                    raise LibraryError("header: a header name")
                prefix = "Bearer " if header.lower() == "authorization" else ""
                lines.append("headers = " + _table({header: f"{prefix}{{secret:{secret}}}"}))
            body = [p for p in params if p not in in_url]
            if body:
                lines.append("body = " + _table({p: f"{{input:{p}}}" for p in body}))
            if response:
                lines.append("response = " + _table(response))
            lines.append("timeout_seconds = 30")
    return "\n".join(lines) + "\n"


def retitle(text: str, old_id: str, new_id: str) -> str:
    """A declaration under another id (a copy into ``own``)."""
    pattern = re.compile(rf'^(id\s*=\s*)"{re.escape(old_id)}"\s*$', re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(rf'\1"{new_id}"', text, count=1)
    return f'id = "{new_id}"\n' + text
