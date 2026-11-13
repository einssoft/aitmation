"""Solution format 2 building blocks (ADR-0035).

A solution is the catalog unit in declaration format 2: ``solution.toml``
plus type folders. This module holds the parsers for the artefact kinds
that format 1 did not know - tools, model roles, configurations, sidecar
test cases, constants, plain types - and the layout rules. The bundle
validator in :mod:`aitmation_core.apps` composes them into one
:class:`~aitmation_core.apps.App`; it stays THE validator (ADR-0023).

Layout (ADR-0035 §2)::

    <solution-id>/
      solution.toml
      types/           record schemas (ADR-0026) and plain types/enums
      constants/       parameters no process may change
      tools/           tool declarations: name, description, effect, binding
      skills/<id>/     skill.toml (+ skill.test.toml)
      knowledge/       knowledge space declarations (ADR-0031)
      doctypes/
      configurations/  standard | test | shadow | <own>
      processes/       *.bpmn (+ <name>.test.toml)
      decisions/       *.dmn  (+ <name>.test.toml)
      cases/           *.cmmn (reserved, not executed)
      ai/              <id>.aimn.toml (+ <id>.test.toml)
      tests/           suites, end-to-end cases, recordings/
      samples/
      packages/<name>/ the same layout, scoped (§4)
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .data_classes import DataClassError, rank
from .i18n import TextError, normalize
from .secrets import is_tool_secret

RESERVED_CONFIGURATIONS = ("standard", "test", "shadow")
# ``read``: a read-only call to the outside (connectors ``fetch`` and
# ``search``) - no secrets of ours leave, nothing is written anywhere,
# what comes back is foreign content; such a step may run without a
# passed gate (ADR-0042). ``external`` needs a gate or a mandate (ADR-0007).
TOOL_EFFECTS = ("internal", "read", "external")
READ_CONNECTORS = ("fetch", "search")
TOOL_MODES = ("live", "stub", "log")
_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
SEMVER_RANGE = re.compile(r"^[\^~=]?\d+(\.\d+){0,2}$")

TYPE_DIRS = frozenset(
    {
        "types",
        "constants",
        "tools",
        "skills",
        "knowledge",
        "doctypes",
        "configurations",
        "processes",
        "decisions",
        "cases",
        "ai",
        "tests",
        "samples",
        "packages",
    }
)
TOP_FILES = frozenset({"solution.toml", "README.md", "LICENSE"})
SAMPLE_SUFFIXES = frozenset(
    {".txt", ".md", ".pdf", ".eml", ".csv", ".json", ".toml", ".png", ".jpg", ".xml"}
)


class SolutionError(ValueError):
    """A format-2 artefact cannot be parsed."""


# -- model roles (§3) ------------------------------------------------------------


@dataclass(frozen=True)
class ModelRole:
    """What a solution needs from a model, never which model (ADR-0035 §3)."""

    name: str
    structured_output: bool = True
    vision: bool = False
    max_data_class: str = "internal"
    description: dict[str, str] = field(default_factory=dict)


_ROLE_KEYS = frozenset({"structured_output", "vision", "max_data_class", "description"})


def parse_model_roles(raw: object, where: str) -> dict[str, ModelRole]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SolutionError(f"{where}: model_roles must be a table of tables")
    out: dict[str, ModelRole] = {}
    for name, spec in raw.items():
        if not re.match(r"^[a-z][a-z0-9_]*$", str(name)):
            raise SolutionError(f"{where}: model role {name!r} must be a lowercase identifier")
        if not isinstance(spec, dict):
            raise SolutionError(f"{where}: model role {name!r} must be a table")
        unknown = set(spec) - _ROLE_KEYS
        if unknown:
            raise SolutionError(f"{where}: model role {name!r}: unknown keys {sorted(unknown)}")
        data_class = str(spec.get("max_data_class", "internal"))
        try:
            rank(data_class)
        except DataClassError as exc:
            raise SolutionError(f"{where}: model role {name!r}: {exc}") from exc
        try:
            description = (
                normalize(spec["description"], "description") if spec.get("description") else {}
            )
        except TextError as exc:
            raise SolutionError(f"{where}: model role {name!r}: {exc}") from exc
        out[str(name)] = ModelRole(
            name=str(name),
            structured_output=bool(spec.get("structured_output", True)),
            vision=bool(spec.get("vision", False)),
            max_data_class=data_class,
            description=description,
        )
    return out


def parse_dependencies(raw: object, where: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SolutionError(f"{where}: dependencies must be a table")
    out: dict[str, str] = {}
    for solution_id, spec in raw.items():
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", str(solution_id)):
            raise SolutionError(f"{where}: dependency id {solution_id!r} is invalid")
        if not isinstance(spec, str) or not SEMVER_RANGE.match(spec):
            raise SolutionError(
                f"{where}: dependency {solution_id!r} needs a version like '0.2.0', '^0.2' or"
                f" '~0.2.1' (got {spec!r})"
            )
        out[str(solution_id)] = spec
    return out


# -- tools (§2) ---------------------------------------------------------------------


@dataclass(frozen=True)
class HttpSpec:
    """A declarative HTTP call (ADR-0041 §1): placeholders ``{input:x}``
    in url and body, ``{secret:NAME}`` only in headers; ``response``
    maps tool outputs to paths in the JSON answer."""

    method: str = "POST"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: dict = field(default_factory=dict)
    response: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 30
    # ``files``: form field -> ``{input:x}`` whose value is a delivered file;
    # the call becomes multipart (body fields as form fields), the file
    # travels as content, never as a path.
    files: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchSpec:
    """A read-only page fetch (ADR-0041 §1, connector ``fetch``): the URL
    comes from an input - the one connector whose host is *not* fixed,
    because fetching a link a person handed in is its whole purpose. No
    headers, no secrets, GET only, public https hosts only, the answer
    comes back as text and is foreign content (ADR-0038)."""

    url: str = ""  # text: exactly one {input:x}; json: a fixed https address
    max_chars: int = 20000
    # ADR-0059: ``format = "json"`` reads a public JSON endpoint instead of
    # a page - the address is fixed in the declaration (host and path),
    # only query parameters come from inputs; ``response`` maps outputs to
    # paths, ``items`` turns a nested structure into a list of rows.
    format: str = "text"  # text | json
    query: dict[str, str] = field(default_factory=dict)
    response: dict[str, str] = field(default_factory=dict)
    items: dict = field(default_factory=dict)  # {path, fields, max}


@dataclass(frozen=True)
class SearchSpec:
    """A web search (ADR-0042, connector ``search``): the query comes
    from an input, the provider is declared, a key - when the provider
    needs one - is a tool secret sent only as a header. Results are a
    list of title/url/snippet; every URL passes the host policy, the
    snippets are foreign content (ADR-0038)."""

    query: str = ""  # exactly one {input:x}
    provider: str = "duckduckgo"  # duckduckgo | brave | tavily
    max: int = 5
    secret: str = ""  # tool secret name (brave, tavily); "" for duckduckgo


SEARCH_PROVIDERS = ("duckduckgo", "brave", "tavily")


@dataclass(frozen=True)
class McpSpec:
    """A tool of an MCP server (ADR-0006, ADR-0061, connector ``mcp``).
    The bundle names the server only by a role-like name; where it runs
    (address, command, key) and which of its tools may be called is the
    instance's configuration - like a model role, never in the bundle.
    The effect is always ``external``: a server's ``readOnlyHint`` is a
    claim, not a guarantee. ``arguments`` fills the tool's parameters
    (``{input:x}``/``{const:x}``; default: every input under its own
    name); ``response`` maps outputs to paths in the structured answer."""

    server: str = ""
    tool: str = ""
    arguments: dict = field(default_factory=dict)
    response: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 60


@dataclass(frozen=True)
class ClientSpec:
    """A task for an actuator on the user's device (ADR-0041 §1/§4):
    what to do, and whether a file or data comes back."""

    instruction: dict[str, str] = field(default_factory=dict)
    # Where the actuator (or the person) does the work: label -> https URL,
    # shown as buttons on the task card. Declaration only, never visited
    # by the core.
    links: dict[str, str] = field(default_factory=dict)
    expects: str = "file"  # file | data
    kind: str = "text"  # image | video | audio | text, or {input:name}

    def kind_for(self, inputs: dict) -> str:
        """The kind for one task: literal, or read from the bound input;
        an unexpected value falls back to ``text`` (the task still shows
        what it got)."""
        bound = _INPUT_REF.fullmatch(self.kind)
        if bound is None:
            return self.kind
        value = str(inputs.get(bound.group(1), "")).strip().lower()
        return value if value in CLIENT_KINDS else "text"


@dataclass(frozen=True)
class ToolDeclaration:
    """A tool a process may call: what it is for the model (name and
    description are prompt material, ADR-0034 §4), what it touches
    (``effect``) and what runs it (a core action, an HTTP connector or
    an actuator on the client, ADR-0041)."""

    id: str
    name: dict[str, str]
    description: dict[str, str]
    effect: str = "external"
    action: str = ""  # core action id (export, archive, forward, …)
    connector: str = ""  # http | mcp (ADR-0006, later)
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    on_error: str = "failed"  # the outcome a failing call must route to
    exec: str = ""  # "" (core/connector) | "client" (actuator on the device)
    http: HttpSpec | None = None
    fetch: FetchSpec | None = None
    search: SearchSpec | None = None
    client: ClientSpec | None = None
    mcp: McpSpec | None = None


_TOOL_KEYS = frozenset(
    {
        "id", "name", "description", "effect", "action", "connector", "inputs", "outputs",
        "on_error", "exec", "http", "client", "fetch", "search", "mcp",
    }
)
_HTTP_KEYS = frozenset({"method", "url", "headers", "body", "response", "timeout_seconds", "files"})
_CLIENT_KEYS = frozenset({"instruction", "expects", "kind", "links"})
CLIENT_KINDS = ("image", "video", "audio", "text")
_INPUT_REF = re.compile(r"\{input:([a-zA-Z0-9_]+)\}")
_PLACEHOLDER = re.compile(r"\{(input|secret|const):([a-zA-Z0-9_.-]+)\}")
CONNECTORS = ("http", "fetch", "search", "mcp")


def parse_tool(text: str, source: str, default_id: str = "") -> ToolDeclaration:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SolutionError(f"{source}: invalid TOML: {exc}") from exc
    unknown = set(data) - _TOOL_KEYS
    if unknown:
        raise SolutionError(f"{source}: unknown keys {sorted(unknown)}")
    tool_id = str(data.get("id") or default_id)
    if not _ID.match(tool_id):
        raise SolutionError(f"{source}: id {tool_id!r} must be a lowercase identifier")
    effect = str(data.get("effect", "external"))
    if effect not in TOOL_EFFECTS:
        raise SolutionError(f"{source}: effect must be one of {list(TOOL_EFFECTS)}")
    action = str(data.get("action", ""))
    connector = str(data.get("connector", ""))
    exec_ = str(data.get("exec", ""))
    if exec_ not in ("", "client"):
        raise SolutionError(f"{source}: exec must be 'client' or absent")
    if exec_ == "client":
        if action or connector:
            raise SolutionError(f"{source}: a client tool has neither action nor connector")
        action = f"client:{tool_id}"
    elif bool(action) == bool(connector):
        raise SolutionError(f"{source}: a tool binds to exactly one of 'action' or 'connector'")
    if connector and connector not in CONNECTORS:
        raise SolutionError(f"{source}: unknown connector {connector!r} (known: {CONNECTORS})")
    http = _parse_http(data.get("http"), source) if connector == "http" else None
    if connector == "http" and http is None:
        raise SolutionError(f"{source}: connector = \"http\" needs an [http] table")
    if data.get("http") and connector != "http":
        raise SolutionError(f"{source}: [http] belongs to connector = \"http\"")
    fetch = (
        _parse_fetch(data.get("fetch"), source, dict(data.get("inputs") or {}))
        if connector == "fetch" else None
    )
    if data.get("fetch") and connector != "fetch":
        raise SolutionError(f"{source}: [fetch] belongs to connector = \"fetch\"")
    search = (
        _parse_search(data.get("search"), source, dict(data.get("inputs") or {}))
        if connector == "search" else None
    )
    if data.get("search") and connector != "search":
        raise SolutionError(f"{source}: [search] belongs to connector = \"search\"")
    mcp = (
        _parse_mcp(data.get("mcp"), source, dict(data.get("inputs") or {}))
        if connector == "mcp" else None
    )
    if data.get("mcp") and connector != "mcp":
        raise SolutionError(f"{source}: [mcp] belongs to connector = \"mcp\"")
    if effect == "read" and connector not in READ_CONNECTORS:
        raise SolutionError(
            f"{source}: effect = \"read\" is for the connectors {list(READ_CONNECTORS)} only"
        )
    client = (
        _parse_client(data.get("client"), source, dict(data.get("inputs") or {}))
        if exec_ == "client" else None
    )
    if data.get("client") and exec_ != "client":
        raise SolutionError(f"{source}: [client] belongs to exec = \"client\"")
    try:
        name = normalize(data.get("name", tool_id), "name")
        description = (
            normalize(data["description"], "description") if data.get("description") else {}
        )
    except TextError as exc:
        raise SolutionError(f"{source}: {exc}") from exc
    if not description:
        raise SolutionError(f"{source}: a tool needs a description - the model reads it")
    for key in ("inputs", "outputs"):
        value = data.get(key, {})
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise SolutionError(f"{source}: {key} must be a table of field descriptions")
    on_error = str(data.get("on_error", "failed"))
    if not re.match(r"^[a-z][a-z0-9_]*$", on_error):
        raise SolutionError(f"{source}: on_error must be an outcome name")
    return ToolDeclaration(
        id=tool_id,
        name=name,
        description=description,
        effect=effect,
        action=action,
        connector=connector,
        inputs=dict(data.get("inputs", {})),
        outputs=dict(data.get("outputs", {})),
        on_error=on_error,
        exec=exec_,
        http=http,
        fetch=fetch,
        search=search,
        client=client,
        mcp=mcp,
    )


def _parse_http(raw, source: str) -> HttpSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SolutionError(f"{source}: http must be a table")
    unknown = set(raw) - _HTTP_KEYS
    if unknown:
        raise SolutionError(f"{source}: http: unknown keys {sorted(unknown)}")
    method = str(raw.get("method", "POST")).upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise SolutionError(f"{source}: http.method {method!r} is not supported")
    url = str(raw.get("url", ""))
    if not url.startswith(("https://", "http://")):
        raise SolutionError(f"{source}: http.url must be an absolute http(s) URL")
    parts = urlsplit(url)
    if not parts.hostname or _PLACEHOLDER.search(parts.netloc):
        # The host is fixed in the declaration: an input may fill a path
        # segment, never choose where the headers (and their secret) go.
        raise SolutionError(f"{source}: http.url needs a fixed host (no placeholder in it)")
    headers = raw.get("headers", {})
    body = raw.get("body", {})
    response = raw.get("response", {})
    for key, value in (("headers", headers), ("response", response)):
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise SolutionError(f"{source}: http.{key} must be a table of strings")
    if not isinstance(body, dict):
        raise SolutionError(f"{source}: http.body must be a table")
    for kind, name in _PLACEHOLDER.findall(url):
        if kind == "secret":
            raise SolutionError(
                f"{source}: http.url may not carry a secret ({name}) - use a header"
            )
    _check_placeholders(body, source, "http.body", allow_secret=False)
    for value in headers.values():
        for kind, name in _PLACEHOLDER.findall(value):
            if kind == "const":
                raise SolutionError(f"{source}: http.headers: {{const:}} is not resolved here")
            if kind == "secret" and not is_tool_secret(name):
                raise SolutionError(
                    f"{source}: http.headers: secret {name!r} is not a tool secret"
                    " (a-z, digits, _; the instance's own keys are out of reach)"
                )
    timeout = raw.get("timeout_seconds", 30)
    if not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise SolutionError(f"{source}: http.timeout_seconds must be 1..300")
    files = raw.get("files", {})
    if not isinstance(files, dict) or not all(
        isinstance(v, str) and _INPUT_REF.fullmatch(v) for v in files.values()
    ):
        raise SolutionError(f"{source}: http.files must map form fields to {{input:x}}")
    if files and method == "GET":
        raise SolutionError(f"{source}: http.files needs POST/PUT/PATCH")
    if files and any(not isinstance(v, str) for v in body.values()):
        raise SolutionError(f"{source}: with http.files the body fields must be strings (form)")
    return HttpSpec(method, url, dict(headers), dict(body), dict(response), timeout, dict(files))


def _check_placeholders(value, source: str, where: str, *, allow_secret: bool) -> None:
    if isinstance(value, str):
        for kind, name in _PLACEHOLDER.findall(value):
            if kind == "secret" and not allow_secret:
                raise SolutionError(f"{source}: {where} may not carry a secret ({name})")
    elif isinstance(value, dict):
        for v in value.values():
            _check_placeholders(v, source, where, allow_secret=allow_secret)
    elif isinstance(value, list):
        for v in value:
            _check_placeholders(v, source, where, allow_secret=allow_secret)


def _parse_fetch_json(raw: dict, source: str, inputs: dict) -> FetchSpec:
    """ADR-0059: a public JSON endpoint, address fixed in the declaration."""
    unknown = set(raw) - {"url", "format", "query", "response", "items"}
    if unknown:
        raise SolutionError(f"{source}: fetch: unknown keys {sorted(unknown)}")
    url = str(raw.get("url", ""))
    if not url.startswith("https://") or "{" in url or "?" in url:
        raise SolutionError(
            f"{source}: fetch.url (json) must be a fixed https address without placeholders"
            " or query - parameters go into [fetch.query]"
        )
    query = raw.get("query", {})
    if not isinstance(query, dict):
        raise SolutionError(f"{source}: fetch.query must be a table")
    for key, value in query.items():
        value = str(value)
        for name in _INPUT_REF.findall(value):
            if name not in inputs:
                raise SolutionError(f"{source}: fetch.query.{key} names undeclared input {name!r}")
        if "{" in _INPUT_REF.sub("", value):
            raise SolutionError(f"{source}: fetch.query.{key}: only {{input:x}} placeholders")
    response = raw.get("response", {})
    if not isinstance(response, dict):
        raise SolutionError(f"{source}: fetch.response must be a table")
    items = raw.get("items", {})
    if not isinstance(items, dict):
        raise SolutionError(f"{source}: fetch.items must be a table")
    if items:
        if not isinstance(items.get("path"), str) or not items["path"]:
            raise SolutionError(f"{source}: fetch.items needs a path")
        fields_ = items.get("fields", {})
        if not isinstance(fields_, dict) or not fields_:
            raise SolutionError(f"{source}: fetch.items.fields must name the row fields")
        limit = items.get("max", 50)
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise SolutionError(f"{source}: fetch.items.max must be 1..500")
        extra = sorted(set(items) - {"path", "fields", "max"})
        if extra:
            raise SolutionError(f"{source}: fetch.items: unknown keys {extra}")
    if not response and not items:
        raise SolutionError(f"{source}: fetch (json) needs [fetch.response] or [fetch.items]")
    return FetchSpec(url=url, format="json", query={str(k): str(v) for k, v in query.items()},
                     response={str(k): str(v) for k, v in response.items()}, items=dict(items))


def _parse_fetch(raw, source: str, inputs: dict) -> FetchSpec:
    if not isinstance(raw, dict):
        raise SolutionError(f"{source}: connector = \"fetch\" needs a [fetch] table")
    if raw.get("format", "text") == "json":
        return _parse_fetch_json(raw, source, inputs)
    if raw.get("format", "text") != "text":
        raise SolutionError(f"{source}: fetch.format must be text or json")
    unknown = set(raw) - {"url", "max_chars", "format"}
    if unknown:
        raise SolutionError(f"{source}: fetch: unknown keys {sorted(unknown)}")
    url = str(raw.get("url", ""))
    bound = _INPUT_REF.fullmatch(url)
    if bound is None or bound.group(1) not in inputs:
        raise SolutionError(
            f"{source}: fetch.url must be exactly one {{input:x}} naming a declared input"
        )
    max_chars = raw.get("max_chars", 20000)
    if not isinstance(max_chars, int) or not 500 <= max_chars <= 200000:
        raise SolutionError(f"{source}: fetch.max_chars must be 500..200000")
    return FetchSpec(url, max_chars)


def _parse_search(raw, source: str, inputs: dict) -> SearchSpec:
    if not isinstance(raw, dict):
        raise SolutionError(f"{source}: connector = \"search\" needs a [search] table")
    unknown = set(raw) - {"query", "provider", "max", "secret"}
    if unknown:
        raise SolutionError(f"{source}: search: unknown keys {sorted(unknown)}")
    query = str(raw.get("query", ""))
    bound = _INPUT_REF.fullmatch(query)
    if bound is None or bound.group(1) not in inputs:
        raise SolutionError(
            f"{source}: search.query must be exactly one {{input:x}} naming a declared input"
        )
    provider = str(raw.get("provider", "duckduckgo"))
    if provider not in SEARCH_PROVIDERS:
        raise SolutionError(f"{source}: search.provider must be one of {list(SEARCH_PROVIDERS)}")
    maximum = raw.get("max", 5)
    if not isinstance(maximum, int) or not 1 <= maximum <= 20:
        raise SolutionError(f"{source}: search.max must be 1..20")
    secret = str(raw.get("secret", ""))
    if provider == "duckduckgo" and secret:
        raise SolutionError(f"{source}: search.secret: duckduckgo takes no key")
    if provider != "duckduckgo":
        if not secret:
            raise SolutionError(f"{source}: search.provider {provider!r} needs search.secret")
        if not is_tool_secret(secret):
            raise SolutionError(
                f"{source}: search.secret {secret!r} is not a tool secret name"
                " (lowercase, digits, underscore)"
            )
    return SearchSpec(query, provider, maximum, secret)


def _parse_mcp(raw, source: str, inputs: dict) -> McpSpec:
    if not isinstance(raw, dict):
        raise SolutionError(f"{source}: connector = \"mcp\" needs an [mcp] table")
    unknown = set(raw) - {"server", "tool", "arguments", "response", "timeout_seconds"}
    if unknown:
        raise SolutionError(f"{source}: mcp: unknown keys {sorted(unknown)}")
    server = str(raw.get("server", ""))
    if not _ID.match(server):
        raise SolutionError(f"{source}: mcp.server must name a server (lowercase identifier)")
    tool = str(raw.get("tool", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", tool):
        raise SolutionError(f"{source}: mcp.tool must be the server's tool name")
    arguments = raw.get("arguments", None)
    if arguments is None:
        arguments = {name: f"{{input:{name}}}" for name in inputs}
    if not isinstance(arguments, dict):
        raise SolutionError(f"{source}: mcp.arguments must be a table")
    _check_placeholders(arguments, source, "mcp.arguments", allow_secret=False)
    for name in _INPUT_REF.findall(json.dumps(arguments)):
        if name not in inputs:
            raise SolutionError(f"{source}: mcp.arguments names undeclared input {name!r}")
    response = raw.get("response", {})
    if not isinstance(response, dict) or not all(isinstance(v, str) for v in response.values()):
        raise SolutionError(f"{source}: mcp.response must be a table of paths")
    timeout = raw.get("timeout_seconds", 60)
    if not isinstance(timeout, int) or not 1 <= timeout <= 600:
        raise SolutionError(f"{source}: mcp.timeout_seconds must be 1..600")
    return McpSpec(server, tool, dict(arguments), dict(response), timeout)


def _parse_client(raw, source: str, inputs: dict) -> ClientSpec:
    if raw is None:
        raise SolutionError(f"{source}: exec = \"client\" needs a [client] table with instruction")
    if not isinstance(raw, dict):
        raise SolutionError(f"{source}: client must be a table")
    unknown = set(raw) - _CLIENT_KEYS
    if unknown:
        raise SolutionError(f"{source}: client: unknown keys {sorted(unknown)}")
    try:
        instruction = normalize(raw.get("instruction", ""), "instruction")
    except TextError as exc:
        raise SolutionError(f"{source}: client.instruction: {exc}") from exc
    if not any(text.strip() for text in instruction.values()):
        raise SolutionError(f"{source}: client.instruction tells the actuator what to do")
    expects = str(raw.get("expects", "file"))
    if expects not in ("file", "data"):
        raise SolutionError(f"{source}: client.expects must be file or data")
    kind = str(raw.get("kind", "text"))
    bound = _INPUT_REF.fullmatch(kind)
    if bound is not None:
        # The kind comes from a bound input at run time (a decision table
        # says whether this post wants an image or a clip).
        if bound.group(1) not in inputs:
            raise SolutionError(
                f"{source}: client.kind refers to input {bound.group(1)!r}, which is not declared"
            )
    elif kind not in CLIENT_KINDS:
        raise SolutionError(f"{source}: client.kind must be image, video, audio or text")
    links = raw.get("links", {})
    if not isinstance(links, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v.startswith("https://")
        for k, v in links.items()
    ) or len(links) > 8:
        raise SolutionError(f"{source}: client.links must map labels to https URLs (max 8)")
    return ClientSpec(instruction, dict(links), expects, kind)


# -- configurations (§7) --------------------------------------------------------


@dataclass(frozen=True)
class ToolSetting:
    mode: str = "live"  # live | stub | log
    outcome: str = "done"
    result: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Configuration:
    """A named configuration (ACTICO's named configurations, ADR-0035 §7).

    ``model_roles`` maps a role to ``"recording"`` (replay from the
    solution's recordings) or to a registry entry id; ``tools`` maps a
    tool id or core action to how it runs; ``now`` pins the clock;
    ``notifications`` is ``send`` or ``log``. A configuration may only
    tighten: it can stub tools and replace models, never remove gates or
    lower data classes - there is simply no field for that.
    """

    name: str
    model_roles: dict[str, str] = field(default_factory=dict)
    tools: dict[str, ToolSetting] = field(default_factory=dict)
    default_tool_mode: str = "live"
    now: str | None = None
    notifications: str = "send"
    description: dict[str, str] = field(default_factory=dict)

    def tool(self, key: str) -> ToolSetting:
        if key in self.tools:
            return self.tools[key]
        return ToolSetting(mode=self.default_tool_mode)

    @property
    def replays_models(self) -> bool:
        return self.model_roles.get("*") == "recording"


BUILTIN_CONFIGURATIONS: dict[str, Configuration] = {
    "standard": Configuration(name="standard"),
    "test": Configuration(
        name="test",
        model_roles={"*": "recording"},
        default_tool_mode="stub",
        now="2026-01-15T09:00:00+01:00",
        notifications="log",
    ),
    "shadow": Configuration(name="shadow", default_tool_mode="log", notifications="log"),
}
"""The reserved names work without a file; a file of the same name
refines them (adds stubs with specific results, pins another clock)."""

_CONFIG_KEYS = frozenset({"name", "description", "model_roles", "tools", "now", "notifications"})
_TOOL_SETTING_KEYS = frozenset({"mode", "outcome", "result"})


def parse_configuration(text: str, source: str, default_name: str = "") -> Configuration:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SolutionError(f"{source}: invalid TOML: {exc}") from exc
    unknown = set(data) - _CONFIG_KEYS
    if unknown:
        raise SolutionError(f"{source}: unknown keys {sorted(unknown)}")
    name = str(data.get("name") or default_name)
    if not _ID.match(name):
        raise SolutionError(f"{source}: configuration name {name!r} is invalid")
    base = BUILTIN_CONFIGURATIONS.get(name, Configuration(name=name))
    roles = dict(base.model_roles)
    raw_roles = data.get("model_roles", {})
    if not isinstance(raw_roles, dict) or not all(isinstance(v, str) for v in raw_roles.values()):
        raise SolutionError(f"{source}: model_roles must map roles to 'recording' or a model id")
    roles.update({str(k): str(v) for k, v in raw_roles.items()})
    tools: dict[str, ToolSetting] = dict(base.tools)
    raw_tools = data.get("tools", {})
    if not isinstance(raw_tools, dict):
        raise SolutionError(f"{source}: tools must be a table of tables")
    default_mode = base.default_tool_mode
    for key, spec in raw_tools.items():
        if not isinstance(spec, dict):
            raise SolutionError(f"{source}: tools.{key} must be a table")
        unknown = set(spec) - _TOOL_SETTING_KEYS
        if unknown:
            raise SolutionError(f"{source}: tools.{key}: unknown keys {sorted(unknown)}")
        mode = str(spec.get("mode", "stub"))
        if mode not in TOOL_MODES:
            raise SolutionError(f"{source}: tools.{key}.mode must be one of {list(TOOL_MODES)}")
        result = spec.get("result", {})
        if not isinstance(result, dict):
            raise SolutionError(f"{source}: tools.{key}.result must be a table")
        setting = ToolSetting(mode=mode, outcome=str(spec.get("outcome", "done")), result=result)
        if key == "*":
            default_mode = mode
        else:
            tools[str(key)] = setting
    now = data.get("now", base.now)
    if now is not None and not isinstance(now, str):
        raise SolutionError(f"{source}: now must be an ISO 8601 string")
    notifications = str(data.get("notifications", base.notifications))
    if notifications not in ("send", "log"):
        raise SolutionError(f"{source}: notifications must be 'send' or 'log'")
    try:
        description = (
            normalize(data["description"], "description") if data.get("description") else {}
        )
    except TextError as exc:
        raise SolutionError(f"{source}: {exc}") from exc
    return Configuration(
        name=name,
        model_roles=roles,
        tools=tools,
        default_tool_mode=default_mode,
        now=now,
        notifications=notifications,
        description=description,
    )


# -- sidecar test cases (§6) ------------------------------------------------------


@dataclass(frozen=True)
class SidecarCase:
    """One case of a ``<element>.test.toml`` sidecar."""

    id: str
    subject_kind: str  # process | decision | ai | skill | tool
    subject_id: str
    description: str
    given: dict
    expect: dict
    config: str = "test"
    repeat: int = 1
    decisions: dict[str, str] = field(default_factory=dict)  # gate -> outcome


_CASE_KEYS = frozenset({"id", "description", "given", "expect", "config", "repeat", "decisions"})


def parse_sidecar(
    text: str, source: str, subject_kind: str, subject_id: str
) -> tuple[SidecarCase, ...]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SolutionError(f"{source}: invalid TOML: {exc}") from exc
    unknown = set(data) - {"case", "subject"}
    if unknown:
        raise SolutionError(f"{source}: unknown top-level keys {sorted(unknown)} (use [[case]])")
    raw_cases = data.get("case", [])
    if not isinstance(raw_cases, list) or not raw_cases:
        raise SolutionError(f"{source}: at least one [[case]] is required")
    cases: list[SidecarCase] = []
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise SolutionError(f"{source}: every [[case]] must be a table")
        unknown = set(raw) - _CASE_KEYS
        if unknown:
            raise SolutionError(f"{source}: case: unknown keys {sorted(unknown)}")
        case_id = str(raw.get("id", ""))
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", case_id):
            raise SolutionError(f"{source}: case id {case_id!r} must be a lowercase identifier")
        if case_id in seen:
            raise SolutionError(f"{source}: duplicate case id {case_id!r}")
        seen.add(case_id)
        given = raw.get("given", {})
        expect = raw.get("expect", {})
        if not isinstance(given, dict):
            raise SolutionError(f"{source}: case {case_id!r}: given must be a table")
        if not isinstance(expect, dict) or not expect:
            raise SolutionError(f"{source}: case {case_id!r}: expect must be a non-empty table")
        repeat = raw.get("repeat", 1)
        if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1:
            raise SolutionError(f"{source}: case {case_id!r}: repeat must be a positive integer")
        config = str(raw.get("config", "test"))
        if not _ID.match(config):
            raise SolutionError(f"{source}: case {case_id!r}: config name {config!r} is invalid")
        decisions = raw.get("decisions", {})
        if not isinstance(decisions, dict) or not all(
            isinstance(v, str) for v in decisions.values()
        ):
            raise SolutionError(f"{source}: case {case_id!r}: decisions must map gates to outcomes")
        cases.append(
            SidecarCase(
                id=case_id,
                subject_kind=subject_kind,
                subject_id=subject_id,
                description=str(raw.get("description", "")),
                given=dict(given),
                expect=dict(expect),
                config=config,
                repeat=repeat,
                decisions={str(k): str(v) for k, v in decisions.items()},
            )
        )
    return tuple(cases)


# -- constants and plain types --------------------------------------------------


def parse_constants(text: str, source: str) -> dict:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SolutionError(f"{source}: invalid TOML: {exc}") from exc
    for key in data:
        if not re.match(r"^[a-z][a-z0-9_]*$", str(key)):
            raise SolutionError(f"{source}: constant {key!r} must be a lowercase identifier")
    return dict(data)


@dataclass(frozen=True)
class TypeDeclaration:
    """A plain type (``kind = "enum"`` with ``values``, or ``kind = "type"``
    with ``fields``) - the vocabulary contracts and tools refer to."""

    id: str
    kind: str
    values: tuple[str, ...] = ()
    fields: dict[str, str] = field(default_factory=dict)
    title: dict[str, str] = field(default_factory=dict)


def parse_type(data: dict, source: str, default_id: str) -> TypeDeclaration:
    unknown = set(data) - {"id", "kind", "values", "fields", "title", "description"}
    if unknown:
        raise SolutionError(f"{source}: unknown keys {sorted(unknown)}")
    type_id = str(data.get("id") or default_id)
    if not _ID.match(type_id):
        raise SolutionError(f"{source}: id {type_id!r} must be a lowercase identifier")
    kind = str(data.get("kind", "type"))
    if kind not in ("enum", "type"):
        raise SolutionError(
            f"{source}: kind must be 'enum' or 'type' (record schemas carry fields)"
        )
    values = data.get("values", [])
    if kind == "enum" and (not isinstance(values, list) or not values):
        raise SolutionError(f"{source}: an enum needs a non-empty values list")
    fields = data.get("fields", {})
    if kind == "type" and (not isinstance(fields, dict) or not fields):
        raise SolutionError(f"{source}: a type needs a non-empty fields table")
    try:
        title = normalize(data.get("title", type_id), "title")
    except TextError as exc:
        raise SolutionError(f"{source}: {exc}") from exc
    return TypeDeclaration(
        id=type_id,
        kind=kind,
        values=tuple(str(v) for v in values) if isinstance(values, list) else (),
        fields={str(k): str(v) for k, v in fields.items()} if isinstance(fields, dict) else {},
        title=title,
    )


def sidecar_for(path: Path) -> Path:
    """``processes/intake.bpmn`` -> ``processes/intake.test.toml``;
    ``ai/read.aimn.toml`` -> ``ai/read.test.toml``."""
    name = path.name
    for suffix in (".aimn.toml", ".bpmn", ".dmn", ".cmmn", ".toml"):
        if name.endswith(suffix):
            return path.with_name(name[: -len(suffix)] + ".test.toml")
    return path.with_suffix(".test.toml")


def subject_of(path: Path) -> str:
    name = path.name
    for suffix in (".test.toml", ".aimn.toml", ".bpmn", ".dmn", ".cmmn", ".toml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def sample_inside(solution_path: Path, ref: str) -> bool:
    """Whether ``ref`` (as written in a sidecar) names a regular file inside
    ``<solution>/samples/`` - resolved, so ``samples/../x`` and symlinks
    out of the solution are refused."""
    if not ref.startswith("samples/") or "\\" in ref or ref.startswith("samples//"):
        return False
    target = solution_path / ref
    try:
        if target.is_symlink():
            return False
        return target.resolve().is_relative_to((solution_path / "samples").resolve())
    except OSError:
        return False
