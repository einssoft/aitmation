"""MCP servers of this instance (ADR-0061).

A solution's tool names a server only by name (``[mcp] server = "github"``);
this file says where that server is and what it may be asked. It lives in
the data directory (``<data>/mcp_servers.toml``), is written by an admin
(Verwaltung -> MCP-Server, or by hand) and is never part of a bundle:

    [servers.github]
    url = "https://api.githubcopilot.com/mcp/"
    headers = { Authorization = "Bearer {secret:github_token}" }
    tools = ["create_issue", "list_issues"]   # allowlist - deny by default

    [servers.files]
    command = ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data/files"]
    env = { TOKEN = "{secret:files_token}" }
    tools = ["read_file"]

* ``url``: streamable HTTP; the host policy applies (https, public hosts
  unless ``tools_allow_private_hosts``). Header values may carry tool
  secrets ``{secret:name}`` only - the instance's own keys stay out.
* ``command``: a local process speaking MCP over stdio. Only when the
  operator allows it (``AITMATION_MCP_ALLOW_STDIO``) - an admin of the
  instance must not be able to run programs on the host by editing this.
* ``tools``: the tools that may be called. Empty = none.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .secrets import is_tool_secret

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
_SECRET_REF = re.compile(r"\{secret:([a-zA-Z0-9_]+)\}")
_ANY_REF = re.compile(r"\{[a-z]+:[^}]*\}")
FILE = "mcp_servers.toml"


class McpConfigError(ValueError):
    pass


@dataclass(frozen=True)
class McpServer:
    name: str
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)
    timeout_seconds: int = 60
    note: str = ""

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def view(self) -> dict:
        """What the API shows: header and env names, never values."""
        return {
            "name": self.name,
            "transport": self.transport,
            "url": self.url,
            "headers": {k: _redact(k, v) for k, v in self.headers.items()},
            "command": self.command,
            "env": {k: _redact(k, v) for k, v in self.env.items()},
            "tools": self.tools,
            "timeout_seconds": self.timeout_seconds,
            "note": self.note,
            "secrets": sorted(
                {
                    m
                    for v in [*self.headers.values(), *self.env.values()]
                    for m in _SECRET_REF.findall(v)
                }
            ),
        }


def _redact(key: str, value: str) -> str:
    """Placeholders are shown (``Bearer {secret:github_token}``), so is a
    plain value under a harmless name (``Accept``); a literal under a name
    that sounds like a credential is masked."""
    rest = _SECRET_REF.sub("", value)
    if _SECRET_REF.search(value) and re.fullmatch(r"[A-Za-z -]{0,12}", rest):
        return value
    if re.search(r"auth|token|key|secret|cookie|pass", key, re.I):
        return "•••"
    return value


def _check_values(values: dict, where: str) -> dict[str, str]:
    if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
        raise McpConfigError(f"{where} must be a table of strings")
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(key)):
            raise McpConfigError(f"{where}: name {key!r} is not allowed")
        if re.search(r"auth|token|key|secret|cookie|pass", key, re.I) and not _SECRET_REF.search(
            value
        ):
            raise McpConfigError(
                f"{where}.{key}: a credential goes in as {{secret:name}} (set the value under"
                " Verwaltung -> Schlüssel), never as text in the configuration"
            )
        for ref in _ANY_REF.findall(value):
            m = _SECRET_REF.fullmatch(ref)
            if not m:
                raise McpConfigError(f"{where}.{key}: only {{secret:name}} placeholders")
            if not is_tool_secret(m.group(1)):
                raise McpConfigError(
                    f"{where}.{key}: {m.group(1)!r} is not a tool secret (the instance's own"
                    " keys stay out of reach)"
                )
    return {str(k): str(v) for k, v in values.items()}


def parse_server(name: str, raw: dict) -> McpServer:
    if not _NAME.match(name):
        raise McpConfigError(f"server name {name!r}: lowercase, digits, - and _")
    if not isinstance(raw, dict):
        raise McpConfigError(f"server {name!r} must be a table")
    unknown = set(raw) - {"url", "headers", "command", "env", "tools", "timeout_seconds", "note"}
    if unknown:
        raise McpConfigError(f"server {name!r}: unknown keys {sorted(unknown)}")
    url = str(raw.get("url") or "")
    command = raw.get("command") or []
    if bool(url) == bool(command):
        raise McpConfigError(f"server {name!r}: exactly one of url or command")
    if url and not url.startswith(("https://", "http://")):
        raise McpConfigError(f"server {name!r}: url must be http(s)")
    if command and (
        not isinstance(command, list) or not all(isinstance(c, str) and c for c in command)
    ):
        raise McpConfigError(f"server {name!r}: command must be a list of strings")
    if any(_ANY_REF.search(c) for c in command):
        raise McpConfigError(f"server {name!r}: no placeholders in command - use env")
    tools = raw.get("tools") or []
    if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools):
        raise McpConfigError(f"server {name!r}: tools must be a list of tool names")
    timeout = raw.get("timeout_seconds", 60)
    if not isinstance(timeout, int) or not 1 <= timeout <= 600:
        raise McpConfigError(f"server {name!r}: timeout_seconds must be 1..600")
    return McpServer(
        name=name,
        url=url,
        headers=_check_values(raw.get("headers") or {}, f"{name}.headers"),
        command=list(command),
        env=_check_values(raw.get("env") or {}, f"{name}.env"),
        tools=list(dict.fromkeys(tools)),
        timeout_seconds=timeout,
        note=str(raw.get("note") or "")[:300],
    )


def load(data_dir: Path) -> dict[str, McpServer]:
    path = Path(data_dir) / FILE
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise McpConfigError(f"{FILE}: invalid TOML: {exc}") from exc
    servers = data.get("servers") or {}
    if not isinstance(servers, dict):
        raise McpConfigError(f"{FILE}: [servers] must be a table")
    return {name: parse_server(name, raw) for name, raw in servers.items()}


def _toml_value(value) -> str:
    import json

    if isinstance(value, dict):
        return (
            "{ " + ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in value.items()) + " }"
        )
    return json.dumps(value, ensure_ascii=False)


def save(data_dir: Path, servers: dict[str, McpServer]) -> None:
    lines = ["# MCP servers of this instance (ADR-0061) - written by Verwaltung -> MCP-Server.", ""]
    for s in sorted(servers.values(), key=lambda x: x.name):
        lines.append(f"[servers.{s.name}]")
        if s.url:
            lines.append(f"url = {_toml_value(s.url)}")
        if s.headers:
            lines.append(f"headers = {_toml_value(s.headers)}")
        if s.command:
            lines.append(f"command = {_toml_value(s.command)}")
        if s.env:
            lines.append(f"env = {_toml_value(s.env)}")
        lines.append(f"tools = {_toml_value(s.tools)}")
        lines.append(f"timeout_seconds = {s.timeout_seconds}")
        if s.note:
            lines.append(f"note = {_toml_value(s.note)}")
        lines.append("")
    path = Path(data_dir) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)
