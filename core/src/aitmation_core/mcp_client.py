"""A minimal MCP client (ADR-0006, ADR-0061) - JSON-RPC 2.0 over the two
standard transports, no SDK:

* streamable HTTP: POST each message; the answer is JSON or an SSE stream
  (``text/event-stream``) carrying it; the session id from ``initialize``
  (``Mcp-Session-Id``) goes along with every later request;
* stdio: a local process, one JSON message per line on stdin/stdout.

Each call opens a session, initialises, does one thing (``tools/list`` or
``tools/call``) and closes - no long-lived connections, nothing shared
between cases. Like ``connectors.py`` this is a place where the core talks
to foreign hosts; the host policy, the body cap and the "no redirects"
rule apply the same way.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from .connectors import MAX_BODY_BYTES, host_policy
from .mcp_servers import McpServer

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "aitmation", "version": "1"}
_SECRET = re.compile(r"\{secret:([a-zA-Z0-9_]+)\}")


class McpError(RuntimeError):
    pass


def _fill(value: str, secrets: Callable[[str], str | None]) -> str:
    def sub(m) -> str:
        found = secrets(m.group(1))
        if found is None:
            raise McpError(f"secret {m.group(1)!r} is not set")
        return found

    return _SECRET.sub(sub, value)


class _HttpSession:
    def __init__(
        self, server: McpServer, secrets, *, transport=None, allow_private=False, timeout: int
    ) -> None:
        refused = host_policy(server.url, allow_private=allow_private)
        if refused:
            raise McpError(refused)
        self.url = server.url
        self.headers = {k: _fill(v, secrets) for k, v in server.headers.items()}
        self.headers.update(
            {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "User-Agent": "aitmation-mcp/1",
            }
        )
        self.client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)
        self.session_id = ""
        self.next_id = 0

    def request(self, method: str, params: dict | None = None) -> Any:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        return self._send(msg, expect=self.next_id)

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method}, expect=None)

    def _send(self, msg: dict, expect: int | None) -> Any:
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        try:
            with self.client.stream("POST", self.url, json=msg, headers=headers) as r:
                if r.headers.get("mcp-session-id"):
                    self.session_id = r.headers["mcp-session-id"]
                if expect is None:
                    if r.status_code >= 400:
                        raise McpError(f"HTTP {r.status_code}")
                    return None
                if 300 <= r.status_code < 400:
                    raise McpError(f"HTTP {r.status_code}: redirects are not followed")
                if r.status_code >= 400:
                    body = r.read()[:300].decode("utf-8", "replace")
                    raise McpError(f"HTTP {r.status_code}: {body}")
                kind = r.headers.get("content-type", "")
                if "text/event-stream" in kind:
                    return _from_sse(r.iter_lines(), expect)
                size, chunks = 0, []
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY_BYTES:
                        raise McpError("answer too large")
                    chunks.append(chunk)
                return _unwrap(json.loads(b"".join(chunks) or b"{}"), expect)
        except httpx.HTTPError as exc:
            raise McpError(f"unreachable: {exc.__class__.__name__}") from exc
        except ValueError as exc:
            raise McpError("answer is not JSON-RPC") from exc

    def close(self) -> None:
        if self.session_id:
            with contextlib.suppress(httpx.HTTPError):
                self.client.delete(
                    self.url, headers={**self.headers, "Mcp-Session-Id": self.session_id}
                )
        self.client.close()


def _from_sse(lines, expect: int) -> Any:
    data: list[str] = []
    size = 0
    for line in lines:
        size += len(line)
        if size > MAX_BODY_BYTES:
            raise McpError("answer too large")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line == "" and data:
            payload = json.loads("\n".join(data))
            data = []
            for msg in payload if isinstance(payload, list) else [payload]:
                if isinstance(msg, dict) and msg.get("id") == expect:
                    return _unwrap(msg, expect)
    if data:
        payload = json.loads("\n".join(data))
        for msg in payload if isinstance(payload, list) else [payload]:
            if isinstance(msg, dict) and msg.get("id") == expect:
                return _unwrap(msg, expect)
    raise McpError("the stream ended without an answer")


def _unwrap(msg: Any, expect: int) -> Any:
    if isinstance(msg, list):
        msg = next((m for m in msg if isinstance(m, dict) and m.get("id") == expect), {})
    if not isinstance(msg, dict):
        raise McpError("answer is not JSON-RPC")
    if "error" in msg:
        err = msg["error"] or {}
        raise McpError(f"server error {err.get('code')}: {str(err.get('message'))[:300]}")
    if "result" not in msg:
        raise McpError("answer has no result")
    return msg["result"]


class _StdioSession:
    def __init__(self, server: McpServer, secrets, *, timeout: int) -> None:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
        env.update({k: _fill(v, secrets) for k, v in server.env.items()})
        try:
            self.proc = subprocess.Popen(
                server.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise McpError(f"cannot start {server.command[0]!r}: {exc}") from exc
        self.timeout = timeout
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.next_id = 0

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _write(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except OSError as exc:
            raise McpError(f"server process gone: {exc}") from exc

    def request(self, method: str, params: dict | None = None) -> Any:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        deadline = time.monotonic() + self.timeout
        received = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise McpError(f"no answer within {self.timeout}s")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                raise McpError("server process ended")
            received += len(line)
            if received > MAX_BODY_BYTES:
                raise McpError("answer too large")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # a server that logs to stdout - ignore the noise
            if isinstance(msg, dict) and msg.get("id") == self.next_id and "method" not in msg:
                return _unwrap(msg, self.next_id)

    def notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


def _open(
    server: McpServer,
    secrets,
    *,
    transport=None,
    allow_private=False,
    allow_stdio=False,
    timeout: int | None = None,
):
    timeout = timeout or server.timeout_seconds
    if server.transport == "stdio":
        if not allow_stdio:
            raise McpError(
                f"server {server.name!r} runs a local command - the operator has not allowed"
                " stdio servers (AITMATION_MCP_ALLOW_STDIO)"
            )
        session = _StdioSession(server, secrets, timeout=timeout)
    else:
        session = _HttpSession(
            server, secrets, transport=transport, allow_private=allow_private, timeout=timeout
        )
    try:
        session.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        session.notify("notifications/initialized")
    except Exception:
        session.close()
        raise
    return session


def list_tools(server: McpServer, secrets, **kw) -> list[dict]:
    """The server's tools (name, description, inputSchema, annotations) -
    all of them, the allowlist marks what may be called."""
    session = _open(server, secrets, **kw)
    try:
        tools: list[dict] = []
        cursor = None
        for _ in range(20):
            result = session.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in (result or {}).get("tools") or [] if isinstance(t, dict))
            cursor = (result or {}).get("nextCursor")
            if not cursor:
                break
        return tools
    finally:
        session.close()


def call_tool(server: McpServer, tool: str, arguments: dict, secrets, **kw) -> dict:
    """``tools/call`` - only for a tool on the server's allowlist. Returns
    ``{text, data, is_error}``: the text parts joined, the structured
    content (or the text parsed as JSON when it is JSON), the error flag."""
    if tool not in server.tools:
        raise McpError(f"tool {tool!r} is not on the allowlist of server {server.name!r}")
    session = _open(server, secrets, **kw)
    try:
        result = session.request("tools/call", {"name": tool, "arguments": arguments}) or {}
    finally:
        session.close()
    texts = [
        str(c.get("text", ""))
        for c in result.get("content") or []
        if isinstance(c, dict) and c.get("type") == "text"
    ]
    text = "\n".join(t for t in texts if t)
    data = result.get("structuredContent")
    if data is None and text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    return {"text": text[:200_000], "data": data, "is_error": bool(result.get("isError"))}
