"""Connectors with outside effect (ADR-0041): the one place in the core
that calls foreign hosts on behalf of a declared tool.

A tool's ``[http]`` table is rendered with the step's bound inputs
(``{input:name}``) and, in headers only, with secrets (``{secret:NAME}``);
the JSON answer is mapped onto the tool's outputs. Nothing here decides
whether a call *may* happen - the runtime checks the configuration
(stub/log/live) and the engine guards the gate before this runs.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import httpx

from .solution import FetchSpec, HttpSpec, SearchSpec

_PLACEHOLDER = re.compile(r"\{(input|secret|const):([a-zA-Z0-9_.-]+)\}")
_INPUT_REF_ONLY = re.compile(r"\{input:([a-zA-Z0-9_]+)\}")
MAX_RESULT_CHARS = 4000
MAX_BODY_BYTES = 4 * 1024 * 1024


class ConnectorError(RuntimeError):
    pass


_LOCAL_NAMES = ("localhost",)
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home")


def host_policy(url: str, *, allow_private: bool) -> str | None:
    """Why this URL may not be called, or None. Public https hosts only by
    default: tools are declarations from a shop, the instance decides what
    they may reach. Loopback/private/link-local addresses (and plain http
    to them) need the operator's ``tools_allow_private_hosts``. Names are
    judged by shape, not resolved - rebinding is out of scope here."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return "no host"
    private = host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        if re.fullmatch(r"[0-9a-fx.:]+", host):
            # "127.1", "2130706433", "0x7f000001": resolver shorthands for
            # addresses - not a name we can judge, so refused.
            return f"host {host!r} is not a plain address or name"
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_multicast or address.is_unspecified
    ):
        private = True
    if private and not allow_private:
        return f"host {host!r} is private/local and tools_allow_private_hosts is off"
    if parts.scheme != "https" and not private:
        return "plain http is only allowed for private/local hosts"
    return None


@dataclass
class HttpOutcome:
    ok: bool
    status: int | None
    outputs: dict[str, Any]
    error: str = ""
    body_excerpt: str = ""


def _render(
    value: Any,
    inputs: dict,
    secrets: Callable[[str], str | None] | None,
    constants: dict | None = None,
    *,
    quote_inputs: bool = False,
) -> Any:
    """Placeholders in strings (and recursively in tables/lists). A whole
    string that is exactly one ``{input:x}`` keeps the input's type, so a
    number or a list travels as such in a JSON body. In the URL
    (``quote_inputs``) an input is percent-encoded so it can fill one path
    segment or query value and never rewrite path, query or fragment."""
    if isinstance(value, str):
        match = _PLACEHOLDER.fullmatch(value)
        if match and match.group(1) == "input" and not quote_inputs:
            name = match.group(2)
            if name not in inputs:
                raise ConnectorError(f"input {name!r} is not bound")
            return inputs[name]

        def sub(m: re.Match) -> str:
            kind, name = m.group(1), m.group(2)
            if kind == "input":
                if name not in inputs:
                    raise ConnectorError(f"input {name!r} is not bound")
                text = "" if inputs[name] is None else str(inputs[name])
                return quote(text, safe="") if quote_inputs else text
            if kind == "const":
                found = _lookup(constants or {}, name)
                if found is None:
                    raise ConnectorError(f"constant {name!r} is not defined")
                return str(found)
            if secrets is None:
                raise ConnectorError(f"secret {name!r} is not allowed here")
            found = secrets(name)
            if found is None:
                raise ConnectorError(f"secret {name!r} is not configured")
            return found

        return _PLACEHOLDER.sub(sub, value)
    if isinstance(value, dict):
        return {k: _render(v, inputs, secrets, constants) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, inputs, secrets, constants) for v in value]
    return value


def _lookup(data: Any, path: str) -> Any:
    if not path:
        return data  # "" = the whole answer
    node = data
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit():
            node = node[int(part)] if int(part) < len(node) else None
        else:
            return None
        if node is None:
            return None
    return node


def call_http(
    spec: HttpSpec,
    inputs: dict,
    secrets: Callable[[str], str | None],
    *,
    constants: dict | None = None,
    transport: httpx.BaseTransport | None = None,
    allow_private: bool = False,
    files_root: Path | None = None,
    files_scope: Path | None = None,
) -> HttpOutcome:
    """Run the declared call. Secrets are resolved into headers only and
    never appear in the outcome; the answer is kept as an excerpt. With
    ``files`` the call is multipart: the named inputs must be files under
    ``files_root`` (the instance's delivered media), sent as content."""
    refused = host_policy(spec.url, allow_private=allow_private)
    if refused:
        return HttpOutcome(False, None, {}, refused)
    try:
        url = _render(spec.url, inputs, None, constants, quote_inputs=True)
        headers = {
            k: str(_render(v, inputs, secrets, constants)) for k, v in spec.headers.items()
        }
        body = _render(spec.body, inputs, None, constants) if spec.body else None
        attachments = _attachments(spec, inputs, files_root, files_scope)
    except ConnectorError as exc:
        return HttpOutcome(False, None, {}, str(exc))
    try:
        with httpx.Client(timeout=spec.timeout_seconds, transport=transport) as client:
            if attachments:
                headers.pop("Content-Type", None)  # httpx sets the multipart boundary
                request = client.build_request(
                    spec.method, url, headers=headers, files=attachments,
                    data={k: _form_value(v) for k, v in (body or {}).items()},
                )
            else:
                request = client.build_request(
                    spec.method, url, headers=headers,
                    json=body if spec.method != "GET" else None,
                    params=body if spec.method == "GET" and isinstance(body, dict) else None,
                )
            response = client.send(request, stream=True)
            try:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY_BYTES:
                        break
                    chunks.append(chunk)
                response._content = b"".join(chunks)  # what we kept of the body
            finally:
                response.close()
            if size > MAX_BODY_BYTES:
                return HttpOutcome(
                    False, response.status_code, {}, f"answer exceeds {MAX_BODY_BYTES} bytes"
                )
    except httpx.TimeoutException:
        return HttpOutcome(False, None, {}, f"timeout after {spec.timeout_seconds}s")
    except Exception as exc:  # the step takes its error path, the tick goes on
        return HttpOutcome(False, None, {}, f"request failed: {exc.__class__.__name__}")
    excerpt = response.text[:MAX_RESULT_CHARS]
    if response.status_code >= 300:
        # Redirects are not followed - the declaration names the host.
        return HttpOutcome(
            False, response.status_code, {}, f"HTTP {response.status_code}", excerpt
        )
    data: Any
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        data = None
    if spec.response and data is None:
        return HttpOutcome(False, response.status_code, {}, "answer is not JSON", excerpt)
    outputs = {name: _bounded(_lookup(data, path)) for name, path in spec.response.items()}
    return HttpOutcome(True, response.status_code, outputs, "", excerpt)


MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def _attachments(
    spec: HttpSpec, inputs: dict, files_root: Path | None, files_scope: Path | None = None
) -> dict:
    """Form field -> (name, bytes, type) for each declared file input.
    Relative paths resolve against ``files_root``; only files below
    ``files_scope`` (the running case's delivered media; ``files_root``
    when none is given) may leave the instance - a path from a model
    output or a stray trigger field never names another case's document."""
    out: dict = {}
    for field_name, placeholder in spec.files.items():
        name = _PLACEHOLDER.fullmatch(placeholder).group(2)  # validated at parse time
        if name not in inputs:
            raise ConnectorError(f"input {name!r} is not bound")
        if files_root is None:
            raise ConnectorError("this instance does not send files")
        value = inputs[name]
        if not isinstance(value, str) or not value:
            raise ConnectorError(f"input {name!r} is not a file")
        path = Path(value)
        if not path.is_absolute():
            path = files_root / path
        root = (files_scope or files_root).resolve()
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ConnectorError(f"input {name!r} is not a delivered file") from exc
        if resolved.stat().st_size > MAX_UPLOAD_BYTES:
            raise ConnectorError(f"input {name!r} exceeds {MAX_UPLOAD_BYTES} bytes")
        out[field_name] = (
            resolved.name, resolved.read_bytes(),
            mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
        )
    return out


def _form_value(value: Any) -> str:
    """Form fields are strings: lists join with commas, tables as JSON."""
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _bounded(value: Any) -> Any:
    """Outputs land in the step result: scalars as they are, structures
    only up to the same cap as the excerpt."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_RESULT_CHARS]
    text = json.dumps(value, ensure_ascii=False)
    return value if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS]


class _TextExtractor(HTMLParser):
    """HTML to readable text: drops script/style/nav chrome, keeps the
    title and the visible text with line breaks at block elements."""

    _SKIP = frozenset({"script", "style", "noscript", "svg", "template", "head"})
    _BLOCK = frozenset({
        "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section",
        "article", "header", "footer", "blockquote", "pre",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _rows(data: Any, path: str, fields: dict[str, str], limit: int) -> list[dict]:
    """``items`` of a JSON read (ADR-0059): ``path`` in dots, ``*`` walks
    every list element or dict value (dict keys in order); each match is a
    row whose ``fields`` are paths below it, ``^key`` the key of the
    innermost ``*`` over a dict (a date in ``days.*.times.*``), and
    ``path|count`` the length of a list."""
    rows: list[dict] = []

    def walk(node: Any, parts: list[str], key: str | None) -> None:
        if len(rows) >= limit:
            return
        if not parts:
            row = {}
            for name, field_path in fields.items():
                if field_path == "^key":
                    row[name] = key
                elif field_path.endswith("|count"):
                    value = _lookup(node, field_path[:-6]) if field_path[:-6] else node
                    row[name] = len(value) if isinstance(value, list | dict) else 0
                else:
                    row[name] = _bounded(_lookup(node, field_path))
            rows.append(row)
            return
        head, rest = parts[0], parts[1:]
        if head == "*":
            if isinstance(node, dict):
                for k in sorted(node):
                    walk(node[k], rest, str(k))
            elif isinstance(node, list):
                for v in node:
                    walk(v, rest, key)
            return
        nxt = _lookup(node, head)
        if nxt is not None:
            walk(nxt, rest, key)

    walk(data, [p for p in path.split(".") if p], None)
    return rows


def fetch_json(
    spec: FetchSpec,
    inputs: dict,
    *,
    transport: httpx.BaseTransport | None = None,
    allow_private: bool = False,
    timeout_seconds: int = 20,
) -> HttpOutcome:
    """GET a public JSON endpoint (ADR-0059, ``fetch`` with ``format =
    "json"``): the address is the declaration's, only query values come
    from inputs; no headers of ours beyond Accept, no secrets, no
    redirects; the body is capped; outputs are mapped paths and rows."""
    params: dict[str, str] = {}
    for key, value in spec.query.items():
        params[key] = _INPUT_REF_ONLY.sub(lambda m: str(inputs.get(m.group(1), "") or ""), value)
    refused = host_policy(spec.url, allow_private=allow_private)
    if refused:
        return HttpOutcome(False, None, {}, refused)
    headers = {"User-Agent": "aitmation-fetch/1 (+read-only)", "Accept": "application/json"}
    chunks: list[bytes] = []
    try:
        with (
            httpx.Client(
                timeout=timeout_seconds, transport=transport, follow_redirects=False,
                headers=headers,
            ) as client,
            client.stream("GET", spec.url, params=params) as response,
        ):
            status = response.status_code
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return HttpOutcome(False, status, {}, "answer too large")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        return HttpOutcome(False, None, {}, f"unreachable: {exc.__class__.__name__}")
    body = b"".join(chunks)
    excerpt = body.decode("utf-8", "replace")[:500]
    if status != 200:
        return HttpOutcome(False, status, {}, f"HTTP {status}", excerpt)
    try:
        data = json.loads(body)
    except ValueError:
        return HttpOutcome(False, status, {}, "answer is not JSON", excerpt)
    outputs = {name: _bounded(_lookup(data, path)) for name, path in spec.response.items()}
    if spec.items:
        rows = _rows(
            data, str(spec.items["path"]), dict(spec.items["fields"]),
            int(spec.items.get("max", 50)),
        )
        outputs["items"] = rows
        outputs["count"] = len(rows)
    return HttpOutcome(True, status, outputs, "")


def fetch_page(
    spec: FetchSpec,
    inputs: dict,
    *,
    transport: httpx.BaseTransport | None = None,
    allow_private: bool = False,
    timeout_seconds: int = 30,
) -> HttpOutcome:
    """GET one public page a person handed in and return its text (ADR-0041
    connector ``fetch``). Read-only, no headers of ours, no secrets; the
    host policy applies to the *resolved* URL; redirects are followed
    only to hosts the policy allows; the body is capped."""
    name = _INPUT_REF_ONLY.fullmatch(spec.url)
    if name is None or name.group(1) not in inputs:
        return HttpOutcome(False, None, {}, "fetch.url is not bound")
    url = str(inputs[name.group(1)] or "").strip()
    if not url.startswith(("http://", "https://")):
        return HttpOutcome(False, None, {}, f"not a web address: {url[:80]!r}")
    refused = host_policy(url, allow_private=allow_private)
    if refused:
        return HttpOutcome(False, None, {}, refused)
    try:
        with httpx.Client(
            timeout=timeout_seconds, transport=transport, follow_redirects=False,
            headers={"User-Agent": "aitmation-fetch/1 (+read-only page fetch)"},
        ) as client:
            hops = 0
            while True:
                response = client.send(client.build_request("GET", url), stream=True)
                if response.status_code in (301, 302, 303, 307, 308) and hops < 3:
                    response.close()
                    target = str(response.headers.get("location", ""))
                    target = str(httpx.URL(url).join(target))
                    refused = host_policy(target, allow_private=allow_private)
                    if refused:
                        return HttpOutcome(False, response.status_code, {}, f"redirect: {refused}")
                    url, hops = target, hops + 1
                    continue
                break
            try:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY_BYTES:
                        break
                    chunks.append(chunk)
                response._content = b"".join(chunks)
            finally:
                response.close()
    except httpx.TimeoutException:
        return HttpOutcome(False, None, {}, f"timeout after {timeout_seconds}s")
    except Exception as exc:
        return HttpOutcome(False, None, {}, f"request failed: {exc.__class__.__name__}")
    if response.status_code >= 300:
        return HttpOutcome(False, response.status_code, {}, f"HTTP {response.status_code}")
    kind = response.headers.get("content-type", "")
    body = response.text
    if "html" in kind or body.lstrip()[:1] == "<":
        parser = _TextExtractor()
        parser.feed(body)
        title, text = parser.title.strip(), parser.text()
    else:
        title, text = "", body
    if len(text) > spec.max_chars:
        text = text[: spec.max_chars] + "\n[… cut at max_chars]"
    outputs = {"url": url, "title": title[:300], "text": text, "content_type": kind[:80]}
    return HttpOutcome(True, response.status_code, outputs, "", text[:MAX_RESULT_CHARS])


_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/124.0 Safari/537.36"
)


class _DuckDuckGoParser(HTMLParser):
    """The HTML endpoint of DuckDuckGo (no key, no account): a result is
    ``a.result__a`` (title, href via a ``uddg`` redirect) followed by
    ``a.result__snippet``. Anything else on the page is ignored."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []
        self._field = ""

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if "result__a" in classes:
            href = attributes.get("href") or ""
            url = parse_qs(urlsplit(href).query).get("uddg", [""])[0] or href
            if url.startswith("//"):
                url = "https:" + url
            self.items.append({"title": "", "url": url, "snippet": ""})
            self._field = "title"
        elif "result__snippet" in classes and self.items:
            self._field = "snippet"

    def handle_endtag(self, tag):
        if tag == "a":
            self._field = ""

    def handle_data(self, data):
        if self._field and self.items:
            self.items[-1][self._field] += data


def _search_request(spec: SearchSpec, query: str, key: str | None) -> httpx.Request:
    if spec.provider == "brave":
        return httpx.Request(
            "GET", "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": spec.max},
            headers={"Accept": "application/json", "X-Subscription-Token": key or ""},
        )
    if spec.provider == "tavily":
        return httpx.Request(
            "POST", "https://api.tavily.com/search",
            json={"query": query, "max_results": spec.max},
            headers={"Authorization": f"Bearer {key or ''}"},
        )
    # The HTML endpoint answers a plain client with an empty "202" page
    # and a browser with results; it is the one place we send a browser's
    # usual identification (no cookies, no script, one GET per search).
    return httpx.Request(
        "GET", "https://html.duckduckgo.com/html/", params={"q": query},
        headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.8"},
    )


def _search_items(spec: SearchSpec, response: httpx.Response) -> list[dict[str, str]]:
    if spec.provider == "duckduckgo":
        parser = _DuckDuckGoParser()
        parser.feed(response.text)
        return parser.items
    data = response.json()
    if spec.provider == "brave":
        raw = ((data.get("web") or {}).get("results") or []) if isinstance(data, dict) else []
        return [
            {"title": str(r.get("title", "")), "url": str(r.get("url", "")),
             "snippet": str(r.get("description", ""))}
            for r in raw if isinstance(r, dict)
        ]
    raw = (data.get("results") or []) if isinstance(data, dict) else []
    return [
        {"title": str(r.get("title", "")), "url": str(r.get("url", "")),
         "snippet": str(r.get("content", ""))}
        for r in raw if isinstance(r, dict)
    ]


def search_web(
    spec: SearchSpec,
    inputs: dict,
    secrets: Callable[[str], str | None] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    allow_private: bool = False,
    timeout_seconds: int = 30,
) -> HttpOutcome:
    """One web search (ADR-0042, connector ``search``): the query from the
    bound input, the provider's fixed endpoint, a key only as a header
    and only for providers that need one. Results come back as
    ``items`` (title, url, snippet) - URLs the host policy refuses are
    dropped, the rest is foreign content for the next step."""
    name = _INPUT_REF_ONLY.fullmatch(spec.query)
    if name is None or name.group(1) not in inputs:
        return HttpOutcome(False, None, {}, "search.query is not bound")
    query = " ".join(str(inputs[name.group(1)] or "").split())[:300]
    if not query:
        return HttpOutcome(False, None, {}, "empty query")
    key = None
    if spec.secret:
        key = secrets(spec.secret) if secrets else None
        if not key:
            return HttpOutcome(False, None, {}, f"secret {spec.secret!r} is not set")
    try:
        with httpx.Client(timeout=timeout_seconds, transport=transport) as client:
            response = client.send(_search_request(spec, query, key))
            if spec.provider == "duckduckgo" and response.status_code == 202:
                # Its "not now" page; one more try after a breath is
                # usually enough, a second 202 is reported as a failure.
                time.sleep(3)
                response = client.send(_search_request(spec, query, key))
    except httpx.TimeoutException:
        return HttpOutcome(False, None, {}, f"timeout after {timeout_seconds}s")
    except Exception as exc:
        return HttpOutcome(False, None, {}, f"request failed: {exc.__class__.__name__}")
    if response.status_code >= 300 or (
        spec.provider == "duckduckgo" and response.status_code == 202
    ):
        # 202 from DuckDuckGo is its "not now" page without results - a
        # refusal to report, not an empty answer to act on.
        return HttpOutcome(
            False, response.status_code, {}, f"HTTP {response.status_code}",
            response.text[:MAX_RESULT_CHARS],
        )
    try:
        found = _search_items(spec, response)
    except (ValueError, AttributeError) as exc:
        return HttpOutcome(False, response.status_code, {}, f"unreadable answer: {exc}")
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in found:
        url = item["url"].strip()
        if not url or url in seen or host_policy(url, allow_private=allow_private):
            continue
        seen.add(url)
        items.append({
            "title": " ".join(item["title"].split())[:200],
            "url": url[:500],
            "snippet": " ".join(item["snippet"].split())[:500],
        })
        if len(items) >= spec.max:
            break
    outputs = {"query": query, "provider": spec.provider, "items": items, "count": len(items)}
    return HttpOutcome(True, response.status_code, outputs, "")


def fetch_bytes(
    url: str,
    limit: int,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: int = 30,
    allow_private: bool = False,
) -> bytes:
    """A plain GET of a public file (the shop's catalog and bundles,
    ADR-0043): host policy, no redirects, no headers of ours, body capped
    at ``limit``. Raises ``ValueError`` with the reason - the one network
    path for file downloads, next to ``call_http``/``fetch_page``."""
    refused = host_policy(url, allow_private=allow_private)
    if refused:
        raise ValueError(refused)
    try:
        with (
            httpx.Client(
                timeout=timeout_seconds, transport=transport, follow_redirects=False
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code} for {url}")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise ValueError(f"{url}: larger than {limit} bytes")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise ValueError(f"{url}: {exc.__class__.__name__}") from exc
    return b"".join(chunks)


def transcribe_remote(
    url: str, token: str | None, path: Path, *, language: str | None, model: str | None,
    timeout: float = 900.0,
) -> dict:
    """The household's transcription service (ADR-0030, backend ``http``):
    ``POST <url>/v1/audio/transcriptions`` in the OpenAI shape, answer as
    ``verbose_json`` (text, language, duration, segments). The only place
    the core sends a recording anywhere - and only to the address the
    operator configured."""
    headers = {"authorization": f"Bearer {token}"} if token else {}
    data = {"response_format": "verbose_json"}
    if language:
        data["language"] = language
    if model:
        data["model"] = model
    with open(path, "rb") as handle:
        try:
            response = httpx.post(
                url.rstrip("/") + "/v1/audio/transcriptions", headers=headers, data=data,
                files={"file": (Path(path).name, handle, "application/octet-stream")},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(
                f"transcription service unreachable: {exc.__class__.__name__}"
            ) from exc
    if response.status_code != 200:
        raise ConnectorError(
            f"transcription service answered HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ConnectorError("transcription service answered with non-JSON") from exc
    return body if isinstance(body, dict) else {}


def transcription_service_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        return httpx.get(url.rstrip("/") + "/healthz", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


class WebReader:
    """The network side of a knowledge crawl (ADR-0045): plain GETs of one
    site with the crawler's own User-Agent, the host policy on every hop,
    redirects followed by hand (at most ``max_redirects``, only where
    ``follow`` allows), and a body that is downloaded only when ``accept``
    wants it and stays below the cap. What to fetch and what it means is
    the crawler's business (knowledge_web); this is the one place that
    talks to the site."""

    def __init__(
        self,
        *,
        user_agent: str,
        transport: httpx.BaseTransport | None = None,
        allow_private: bool = False,
        timeout_seconds: int = 20,
        max_redirects: int = 3,
        before_request: Callable[[], None] | None = None,
        normalize: Callable[[str], str] | None = None,
    ) -> None:
        self.allow_private = allow_private
        self.max_redirects = max_redirects
        self.before_request = before_request or (lambda: None)
        self.normalize = normalize or (lambda url: url)
        self.requests = 0
        self._client = httpx.Client(
            timeout=timeout_seconds, transport=transport, follow_redirects=False,
            headers={"User-Agent": user_agent, "Accept-Language": "de, fr, it, en;q=0.5"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> WebReader:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def get(
        self,
        url: str,
        limit: int,
        *,
        accept: Callable[[str, str], bool] | None = None,
        follow: Callable[[str], bool] | None = None,
    ) -> tuple[int, str, str, bytes | None]:
        """Final status, final URL, content type and body - the body is
        ``None`` when ``accept(final_url, content_type)`` declined it (it
        is then never downloaded) and empty for a non-200 answer. Raises
        ``ValueError`` with the reason for refusals and transport errors."""
        current = url
        for _ in range(self.max_redirects + 1):
            refused = host_policy(current, allow_private=self.allow_private)
            if refused:
                raise ValueError(refused)
            self.before_request()
            self.requests += 1
            try:
                with self._client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = response.headers.get("location", "")
                        nxt = self.normalize(urljoin(current, target))
                        if not nxt:
                            raise ValueError(f"redirect to {target[:80]!r}")
                        if follow is not None and not follow(nxt):
                            raise ValueError(
                                f"redirect leaves the site ({urlsplit(nxt).hostname})"
                            )
                        current = nxt
                        continue
                    kind = response.headers.get("content-type", "")
                    if response.status_code != 200:
                        return response.status_code, current, kind, b""
                    if accept is not None and not accept(current, kind):
                        return 200, current, kind, None
                    megabytes = limit // (1024 * 1024)
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > limit:
                        raise ValueError(f"larger than {megabytes} MB")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise ValueError(f"larger than {megabytes} MB")
                        chunks.append(chunk)
                    return 200, current, kind, b"".join(chunks)
            except httpx.HTTPError as exc:
                raise ValueError(exc.__class__.__name__) from exc
        raise ValueError("too many redirects")
