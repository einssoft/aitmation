"""JSON API for the product UI (ADR-0019/0021) and the MCP channel (ADR-0024).

Everything the React shell in ``ui/`` renders comes from here; the HTML
pages in :mod:`web` are the transitional administration and stay
untouched. Decisions go through the same runtime methods as the HTML
forms - the gate invariant (ADR-0007) is enforced in the engine, not in
either surface.

The shell of a household talks to several instances (ADR-0024). Each
instance therefore answers ``/api/v1/household`` with what it knows: the
household file the launcher wrote (``AITMATION_HOUSEHOLD_FILE``), or
itself as the only member. Cross-origin calls from the shell are allowed
for loopback origins only - instances of one household live on one
machine.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os
import queue
import re
import secrets
import shutil
import tempfile
import threading
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import (
    __version__,
    gateway,
    legal,
    mcp_client,
    mcp_servers,
    metrics,
    model_registry,
    notify,
    operations,
    providers,
    workflow_import,
)
from . import catalog as shop
from . import chat as chat_intents
from . import records as record_store
from .addressees import Addressee, AddresseeError, delete_addressee, save_addressee
from .apps import AppError
from .config import save_config_values
from .definition import StepKind
from .doctypes import Doctype, DoctypeError, save_doctype
from .document import DocumentError, cached_text, extract_text, is_media, media_type_of
from .drafts import DraftError, Drafts
from .drafts import fingerprint as drafts_fingerprint
from .engine import EngineError, origin_trigger
from .i18n import LANGUAGES, pick
from .knowledge import KnowledgeError
from .knowledge_web import SourceError
from .knowledge_web import make_spec as make_source_spec
from .model import ModelError
from .model_registry import (
    PROVIDERS,
    REGIONS,
    RegistryError,
    entry_from_dict,
    save_registry,
)
from .records import RecordError
from .records import history as record_history
from .runtime import ENVELOPE_SUFFIX, Runtime
from .secrets import is_tool_secret, resolve_secret, save_secret, secret_status
from .secrets import is_valid_name as is_valid_secret_name
from .solution import _PLACEHOLDER as _TOOL_PLACEHOLDER
from .template import TemplateError

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


class Decision(BaseModel):
    outcome: str | None = None
    actor: str = "ui"
    comment: str = ""
    # Special gates (ADR-0016 / docs/AGENTIK.md): exactly one of these.
    category: str | None = None
    addressee: str | None = None
    addressees: list[str] | None = None
    discard: bool = False


class ResumeBody(BaseModel):
    breakpoints: list[str] | None = None
    timeout: float = Field(default=30.0, ge=1, le=120)


class RolePin(BaseModel):
    model: str | None = None


class AppSwitch(BaseModel):
    enabled: bool
    actor: str = "ui"


class CatalogInstall(BaseModel):
    """Install or update one catalog entry (ADR-0043 §4): id and version
    as listed; the bundle is fetched, checked and validated before it
    replaces anything."""

    id: str
    version: str


class BundleUpload(BaseModel):
    """A bundle handed in as a file (a solution shared outside the
    catalog, e.g. in the community): base64 of the .tgz."""

    name: str = "bundle.tgz"
    content_base64: str


class ElementBody(BaseModel):
    """A workshop element to write back (ADR-0035): its text as in the file."""

    text: str
    actor: str = "ui"


class BriefingFields(BaseModel):
    """A briefing edited as a form (the workshop's property panel): the
    declared fields, serialized by the core into ``ai/<id>.aimn.toml`` so
    the file keeps its canonical shape."""

    role: dict[str, str] = Field(default_factory=dict)
    instructions: dict[str, str] = Field(default_factory=dict)
    limits: list[str] = Field(default_factory=list)
    model_role: str = ""
    flexibility: str = "strict"
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    output: dict[str, str] = Field(default_factory=dict)
    confidence_threshold: float | None = None
    actor: str = "ui"


class NewSolutionBody(BaseModel):
    """A solution created in the workshop: id plus titles; the scaffold
    writes the smallest valid solution.toml."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=60)
    name: dict[str, str] | str
    description: dict[str, str] | str = ""
    actor: str = "ui"


class NewElementBody(BaseModel):
    """A new process, briefing or decision in a solution. A process names
    the briefing of its AI step (``ai``); a missing briefing is created
    alongside."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=60)
    title: dict[str, str] | str = ""
    ai: str = Field("", pattern=r"^([a-z0-9][a-z0-9_-]*)?$", max_length=60)
    model_role: str = Field("default", pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=40)
    source: str = Field("manual", pattern=r"^(manual|chain|folder|mailbox|schedule)$")
    actor: str = "ui"


class ActorBody(BaseModel):
    actor: str = "ui"


def mcp_tool_declaration(tool_id: str, server: str, tool: dict) -> str:
    """TOML for a tool of an MCP server (ADR-0061): the server's description
    is prompt material for the model, its parameters become inputs."""
    def q(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    names = [n for n in props if re.fullmatch(r"[A-Za-z0-9_]{1,64}", n)]
    description = str(tool.get("description") or tool.get("name"))[:800]
    lines = [
        f"# Tool {tool.get('name')!s} of the MCP server {server!r} (ADR-0061): an outside",
        "# effect - every call waits for a gate; the answer is foreign content.",
        f'id = "{tool_id}"',
        f"name = {q(str(tool.get('title') or tool.get('name')))}",
        f"description = {q(description)}",
        'connector = "mcp"',
        'on_error = "failed"',
        "[inputs]",
    ]
    for n in names:
        p = props.get(n) or {}
        text = str(p.get("description") or p.get("type") or n)[:200]
        lines.append(f"{n} = {q(text + ('' if n in required else ' (optional)'))}")
    lines += ["[outputs]", 'text = "the text the server answered"',
              'data = "the structured answer, when there is one"', "[mcp]",
              f"server = {q(server)}", f"tool = {q(str(tool.get('name')))}"]
    return "\n".join(lines) + "\n"


class McpServerBody(BaseModel):
    """An MCP server of this instance (ADR-0061)."""

    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)
    timeout_seconds: int = 60
    note: str = ""


class McpToolBody(BaseModel):
    """A tool declaration generated from a server's tool (ADR-0061)."""

    server: str
    tool: str
    id: str = ""
    actor: str = "ui"


class ForeignImportBody(BaseModel):
    """A workflow export of another platform (ADR-0060): n8n, Activepieces,
    Make, Zapier. ``zip_base64`` carries a Zapier "Export my data" archive."""

    text: str = Field(default="", max_length=2_000_000)
    zip_base64: str = Field(default="", max_length=8_000_000)
    source: str = ""  # n8n | activepieces | make | zapier; empty = recognise
    id: str = ""
    index: int = Field(default=0, ge=0, le=500)  # which zap of several
    actor: str = "ui"


class ProcessImportBody(BaseModel):
    """A BPMN file brought in from elsewhere (ADR-0034 profile)."""

    name: str = Field(max_length=200)
    text: str = Field(max_length=2_000_000)
    id: str = ""
    draft: bool = True  # ADR-0039: wait for release (false: straight in, validated)
    actor: str = "ui"


class ProcessCopyBody(BaseModel):
    """A new process made from an installed one as its template."""

    source_solution: str = Field(max_length=80)
    source_process: str = Field(max_length=80)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=60)
    draft: bool = True  # ADR-0039: wait for release (false: straight in, validated)
    actor: str = "ui"


class NewCaseBody(BaseModel):
    """A sidecar case for a process, made from a sample (ADR-0035 §6)."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=80)
    sample: str = Field(pattern=r"^samples/[A-Za-z0-9._/-]+$", max_length=200)
    description: str = Field("", max_length=400)
    decisions: dict[str, str] = Field(default_factory=dict)
    status: str = Field("completed", pattern=r"^(completed|running)$")
    outcomes: dict[str, str] = Field(default_factory=dict)
    run: bool = True
    record: bool = False  # record the model's answers through the instance's model
    actor: str = "ui"


class GraphBody(BaseModel):
    """The card canvas's graph written back (ADR-0040)."""

    nodes: list[dict] = Field(default_factory=list, max_length=200)
    edges: list[dict] = Field(default_factory=list, max_length=400)
    contract: list[dict] | None = Field(None, max_length=40)  # start form fields (ADR-0062)
    actor: str = "ui"


class ProvingBody(BaseModel):
    configuration: str = Field("test", pattern=r"^[a-z][a-z0-9_-]*$", max_length=40)
    record: bool = False
    only: str | None = Field(None, max_length=120)
    repeat: int | None = Field(None, ge=1, le=20)


class AppSettings(BaseModel):
    values: dict[str, object]
    actor: str = "ui"


class RecordBody(BaseModel):
    values: dict[str, object]
    actor: str = "ui"


class ModelBody(BaseModel):
    model: str
    provider: str = "anthropic"
    base_url: str = ""
    clearance: str = "internal"
    role: str = "cheap"
    status: str = "experimental"
    cost_in_chf: float = 0.0
    cost_out_chf: float = 0.0
    local: bool = False
    region: str | None = None
    dpa: bool = False
    vision: bool = False
    note: str = ""


class SecretBody(BaseModel):
    value: str = ""


class MailBody(BaseModel):
    imap_host: str = ""
    imap_user: str = ""
    imap_folder: str = "INBOX"
    password: str | None = None  # None = leave as is; "" = remove


class NotifyBody(BaseModel):
    notify_to: str = ""
    escalate_to: str = ""
    remind_after_hours: float = 24.0
    escalate_after_hours: float = 72.0
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_from: str = ""
    smtp_security: str = "starttls"
    public_url: str = ""
    password: str | None = None  # None = leave as is; "" = remove


class SpaceBody(BaseModel):
    title: str | dict[str, str]
    description: str | dict[str, str] | None = None
    data_class: str = "internal"
    owner: str = ""


class KnowledgeSourceBody(BaseModel):
    """A web source of a knowledge space (ADR-0045)."""

    url: str
    title: str = ""
    scope: str = "path"
    doc_types: list[str] = ["html", "pdf"]
    max_pages: int = 200
    max_depth: int = 3
    exclude: list[str] = []
    interval_hours: int = 168
    enabled: bool = True


class KnowledgeDocumentBody(BaseModel):
    name: str
    content_base64: str
    title: str = ""
    replace: str | None = None  # document id whose next version this is


class BudgetBody(BaseModel):
    monthly_budget_chf: float = 0.0
    budget_stop: bool = False


class NotifyTestBody(BaseModel):
    to: str


class FollowUpBody(BaseModel):
    title: str
    due_at: str  # ISO date or date-time, local
    note: str = ""
    about_case: str | None = None
    template: str = "follow-up"
    actor: str = "ui"


class RunNowBody(BaseModel):
    actor: str = "ui"


class StartBody(BaseModel):
    """Start a manual process with the inputs its contract names. A file
    (the contract's ``file`` input) travels base64 and is stored in the
    instance's files before the case starts."""

    payload: dict = Field(default_factory=dict)
    file: IntakeFile | None = None
    actor: str = "ui"


class RunBody(BaseModel):
    """Run a manual process through right away (ADR-0054): the answer
    path for callers that wait for the result - a chat, a form."""

    payload: dict = Field(default_factory=dict)
    actor: str = "api"
    timeout: float = Field(default=30.0, ge=1.0, le=120.0)
    breakpoints: list[str] = Field(default_factory=list)  # test run: stop before these steps
    # ADR-0058: answer as a stream of NDJSON events - live text of steps
    # that stream, then the report as the last line ({"event": "report"}).
    stream: bool = False


class AddresseeBody(BaseModel):
    title: str
    path: str
    channel: str = "folder"
    catch_all: bool = False
    actor: str = "ui"


_ENTRY_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ENV_ENTRY = "this model comes from the environment (LLM_* variables); change it there"


# One proving run at a time per process: each run builds throw-away
# runtimes and the report is only meaningful when runs do not interleave.
CORE_SOLUTION = "core"  # the read-only pseudo-solution of the core templates
_proving_lock = threading.Lock()


def _require_admin(request: Request) -> None:
    """Registry and secrets are the owner's: with authentication on, only
    admins may change them; without users the instance is open anyway."""
    principal = getattr(request.state, "principal", None)
    if principal is not None and not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin role required")


class IntakeFile(BaseModel):
    name: str
    content_base64: str


class FulfilBody(BaseModel):
    """A person delivers what a client task asked for (ADR-0041 §4): a
    file (image, video, audio) and/or data for the tool's outputs."""

    file: IntakeFile | None = None
    data: dict = Field(default_factory=dict)
    actor: str = "ui"


class IntakeBody(BaseModel):
    """What a website form (or any channel client) hands in: an envelope
    plus files. Nothing here is trusted - names are sanitized, the token
    is checked, the document still goes through classification and a gate."""

    sender: str = Field("", alias="from")
    sender_name: str = ""
    subject: str = ""
    text: str = ""
    language: str | None = None
    files: list[IntakeFile] = []

    model_config = {"populate_by_name": True}


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
INTAKE_MAX_FILES = 10
INTAKE_MAX_BYTES = 25 * 1024 * 1024
# Trigger keys with a meaning for the runtime - never accepted from a
# manual start (ADR-0041 review).
_RESERVED_TRIGGER_KEYS = frozenset({
    "source", "file", "files", "attachments", "original_name", "chain_id", "trigger",
    "outputs", "origin", "document",
})
INTAKE_MAX_TEXT = 20_000


class ChatMessage(BaseModel):
    message: str


PREVIEW_CHARS = 4000


class DoctypeBody(BaseModel):
    """A document type as the settings page edits it (ADR-0022 tables)."""

    title: dict[str, str]
    description: dict[str, str] = {}
    features: list[str] = []
    chain: str
    fields: dict[str, str] = {}


class MandateGrant(BaseModel):
    action: str = "forward"
    target: str
    # gate rules compare facts exactly, numbers stay numbers (ADR-0054 §3)
    condition: dict[str, str | int | float | bool] = {}
    actor: str = "ui"
    comment: str = ""


PROXY_HEADER = "x-aitmation-proxy"


def behind_launcher(request: Request | None) -> bool:
    """True when the launcher forwarded this request (ADR-0032): then every
    member is addressed relative to the launcher's origin."""
    return request is not None and bool(request.headers.get(PROXY_HEADER))


def _member_url(member_id: str, port: int, relative: bool) -> str:
    return f"/m/{member_id}" if relative else f"http://127.0.0.1:{port}"


def household_members(runtime: Runtime, request: Request | None = None) -> list[dict]:
    """Members of this household as the launcher declared them, else self.

    Behind the launcher the URLs are origin-relative (``/m/<id>``), so the
    shell works from any device through one origin (ADR-0032); directly on
    the machine they stay ``http://127.0.0.1:<port>``."""
    relative = behind_launcher(request)
    path = runtime.settings.household_path
    if path is not None and path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            data = {}
        members = []
        mailroom = data.get("mailroom", {})
        if isinstance(mailroom, dict) and mailroom.get("port"):
            members.append(
                {
                    "id": "mailroom",
                    "title": pick(mailroom.get("title", "Eingang")),
                    "type": "mailroom",
                    "url": _member_url("mailroom", int(mailroom["port"]), relative),
                }
            )
        for m in data.get("mandants", []) or []:
            if not isinstance(m, dict) or "id" not in m or "port" not in m:
                continue
            members.append(
                {
                    "id": str(m["id"]),
                    "title": pick(m.get("title", m["id"])),
                    "type": str(m.get("type", "company")),
                    "url": _member_url(str(m["id"]), int(m["port"]), relative),
                }
            )
        if members:
            return members
    s = runtime.settings
    return [
        {
            "id": s.instance_id,
            "title": s.instance_title,
            "type": s.instance_type,
            "url": "" if relative else f"http://{s.host}:{s.port}",
        }
    ]


def launcher_url(runtime: Runtime, request: Request | None = None) -> str | None:
    """Where the launcher answers (household file ``[launcher] port``);
    ``""`` (= same origin) when it forwarded this request (ADR-0032)."""
    path = runtime.settings.household_path
    if path is None or not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return None
    launcher = data.get("launcher", {})
    if isinstance(launcher, dict) and launcher.get("port"):
        if behind_launcher(request):
            return ""
        return f"http://127.0.0.1:{int(launcher['port'])}"
    return None


def _gate_view(runtime: Runtime, gate: dict) -> dict:
    """A waiting gate plus everything the UI needs to render a decision."""
    definition = runtime.engine.definitions.get(gate["template_id"])
    step = definition.steps.get(gate["step_id"]) if definition else None
    config = step.config if step else {}
    kind = "generic"
    options: list[dict] = []
    suggested = None
    if config.get("addressing"):
        kind = "addressing"
        options = [
            {"id": a.id, "title": a.title}
            for a in sorted(runtime.addressees.values(), key=lambda a: a.title)
        ]
        suggested = runtime.forward_assignment(gate["case_id"]).get("addressee")
    elif config.get("categorization"):
        kind = "categorization"
        options = [
            {"id": d.id, "title": d.title, "titles": d.titles}
            for d in sorted(runtime.doctypes.values(), key=lambda d: d.title)
        ]
    outcomes = list(step.next) if step else []
    title = (
        pick(definition.meta.get("title", gate["template_id"]))
        if definition
        else gate["template_id"]
    )
    titles = definition.meta.get("title") if definition else None
    return {
        **gate,
        "gate_kind": kind,
        "outcomes": outcomes,
        "options": options,
        "suggested": suggested,
        "template_title": title,
        "template_titles": titles if isinstance(titles, dict) else {"de": title},
        "app": runtime.template_app.get(gate["template_id"]),
        "instance": runtime.settings.instance_id,
        "document": _case_document(runtime, gate["case_id"]),
        # Which notification stages the gate has reached (new/reminder/escalation).
        "notified": notify.state_of(runtime.conn, gate["case_id"], gate["step_id"]),
        # A hand-set follow-up carries its own title and note in the trigger.
        "subject": _trigger_field(runtime, gate["case_id"], "title"),
        "note": _trigger_field(runtime, gate["case_id"], "note"),
        # What happened before the gate: compact step results and delivered
        # media - the reviewer decides on what the pipeline produced
        # (ADR-0041 §4; a draft and its image at "release").
        "results": _case_results(runtime, gate["case_id"]),
        "stub_steps": _stub_steps(runtime, gate["case_id"]),
        "media": _case_media(runtime, gate["case_id"]),
    }


_RESULT_HIDE = frozenset(
    {"stub", "note", "configuration", "tool_mode", "matched_rules", "decision", "prompt_hash"}
)


def _case_results(runtime: Runtime, case_id: str, limit: int = 4000) -> dict[str, dict]:
    """Completed steps' results, trimmed for a gate card: strings cut to
    ``limit``, bookkeeping keys dropped, nested values flattened to text."""
    _trigger, completed = runtime.engine.step_outputs(case_id)
    out: dict[str, dict] = {}
    for sid, entry in completed.items():
        result = entry.get("result")
        if isinstance(result, list):
            # A multi-instance step: one compact row per instance - the
            # tool's inputs and outputs side by side, long texts cut.
            rows = []
            for item in result[:20]:
                if not isinstance(item, dict):
                    rows.append({"value": str(item)[:300]})
                    continue
                row: dict = {}
                for part in ("inputs", "outputs"):
                    for key, value in (item.get(part) or {}).items():
                        row[key] = value[:300] if isinstance(value, str) else value
                if "error" in item:
                    row["error"] = str(item["error"])[:300]
                rows.append(row)
            if rows:
                out[sid] = {"items": json.dumps(rows, ensure_ascii=False, default=str)[:limit]}
            continue
        if not isinstance(result, dict):
            continue
        fields: dict = {}
        for key, value in result.items():
            if key in _RESULT_HIDE or key.startswith("_"):
                continue
            if isinstance(value, str):
                fields[key] = value if len(value) <= limit else value[:limit] + " […]"
            elif isinstance(value, bool | int | float) or value is None:
                fields[key] = value
            elif isinstance(value, list) and all(isinstance(v, str | int | float) for v in value):
                fields[key] = value[:50]
            else:
                fields[key] = json.dumps(value, ensure_ascii=False, default=str)[:limit]
        if fields:
            out[sid] = fields
    return out


def _stub_steps(runtime: Runtime, case_id: str) -> list[str]:
    """Agent steps that ran as stubs (no model configured): a gate after
    them shows no content, and the person must learn why rather than
    stare at an empty card."""
    _trigger, completed = runtime.engine.step_outputs(case_id)
    return [
        sid for sid, entry in completed.items()
        if isinstance(entry.get("result"), dict) and entry["result"].get("stub") is True
    ]


def _case_media(runtime: Runtime, case_id: str) -> list[dict]:
    folder = runtime.settings.files_dir / "media" / case_id
    if not re.match(r"^[a-f0-9]{32}$", case_id) or not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        out.append(
            {"name": path.name, "bytes": path.stat().st_size,
             "kind": media_type_of(path.name) or "file"}
        )
    return out


def _trigger_field(runtime: Runtime, case_id: str, key: str) -> str | None:
    row = runtime.conn.execute(
        "SELECT result FROM steps WHERE case_id = ? AND kind = 'trigger'", (case_id,)
    ).fetchone()
    if not row or not row["result"]:
        return None
    try:
        payload = json.loads(row["result"])
    except json.JSONDecodeError:
        return None
    value = payload.get(key) if isinstance(payload, dict) else None
    return str(value) if value else None


def _starters(runtime: Runtime) -> dict[str, frozenset[str]]:
    """Processes a person may start from the chat: manual and scheduled
    ones (the same set as "Vorgang starten" in the workshop), keyed by
    the words that name them."""
    out: dict[str, frozenset[str]] = {}
    for definition in runtime.engine.definitions.values():
        if not definition.sources() & {"manual", "schedule"}:
            continue
        if str(definition.meta.get("startable", "true")).lower() in ("false", "0", "no"):
            continue  # the process says it is not meant to be started by hand
        out[definition.id] = chat_intents.starter_words(
            definition.id, definition.meta.get("title")
        )
    return out


def _start_view(runtime: Runtime, process_id: str) -> dict:
    definition = runtime.engine.definitions[process_id]
    return {
        "process_id": process_id,
        "titles": _titles(definition.meta.get("title"), process_id),
        "contract": dict(definition.contract.get("input", {}))
        if isinstance(definition.contract.get("input"), dict) else {},
        "defaults": dict(definition.contract.get("defaults", {}))
        if isinstance(definition.contract.get("defaults"), dict) else {},
    }


def _as_text(value) -> str:
    """A default as the start form shows it (lists and tables as JSON)."""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _titles(titles: object, fallback: str) -> dict[str, str]:
    if isinstance(titles, dict):
        return titles
    return {"de": pick(titles if isinstance(titles, str) else fallback)}


def _case_document(runtime: Runtime, case_id: str) -> dict | None:
    row = runtime.conn.execute(
        "SELECT result FROM steps WHERE case_id = ? AND kind = 'trigger'", (case_id,)
    ).fetchone()
    if not row or not row["result"]:
        return None
    payload = json.loads(row["result"])
    # A chain-started case (ADR-0014) carries the original envelope nested;
    # the person wants to see the document, not "chain".
    if payload.get("source") in ("chain", "call") and isinstance(payload.get("trigger"), dict):
        payload = {**origin_trigger(payload), "chained": True}
    keep = (
        "source",
        "original_name",
        "from",
        "to",
        "subject",
        "sender",
        "sender_name",
        "recipient",
        "chained",
        "language",
        "channel",
    )
    view = {k: payload[k] for k in keep if payload.get(k) is not None}
    # Whether there is a file to open: a scheduled or called case has an
    # envelope but no document, and the view must not offer one.
    view["has_file"] = bool(runtime._case_files(case_id))
    return view


def _case_relatives(runtime: Runtime, row) -> dict:
    """Parent and children of a case (callActivity, ADR-0034): the person
    following a scheduled slot wants to reach the post it started."""
    children = runtime.conn.execute(
        "SELECT id, template_id, status FROM cases WHERE parent_case_id = ? ORDER BY created_at",
        (row["id"],),
    ).fetchall()

    def view(r) -> dict:
        definition = runtime.engine.definitions.get(r["template_id"])
        titles = definition.meta.get("title") if definition else None
        return {
            "id": r["id"], "template_id": r["template_id"], "status": r["status"],
            "template_titles": _titles(titles, r["template_id"]),
        }

    parent = None
    if row["parent_case_id"]:
        prow = runtime.conn.execute(
            "SELECT id, template_id, status FROM cases WHERE id = ?", (row["parent_case_id"],)
        ).fetchone()
        parent = view(prow) if prow else None
    return {"parent": parent, "children": [view(c) for c in children]}


def _case_summary(runtime: Runtime, case_id: str) -> dict | None:
    """Latest structured agent result (summary/anomalies) if any."""
    row = runtime.conn.execute(
        "SELECT result FROM steps WHERE case_id = ? AND kind = 'agent_step'"
        " AND status = 'completed' ORDER BY finished_at DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if not row or not row["result"]:
        return None
    try:
        data = json.loads(row["result"])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _is_intake(definition) -> bool:
    return any(step.config.get("classify") for step in definition.steps.values())


def actor_of(request: Request, claimed: str) -> str:
    """The signed-in principal is the actor (ADR-0025 §5); the client's
    claim only counts while the instance runs without authentication."""
    principal = getattr(request.state, "principal", None)
    return principal.sub if principal is not None else claimed


def _open_count(runtime: Runtime) -> int:
    """Open gates plus client tasks, read under the runtime lock."""
    with runtime.lock:
        return len(runtime.open_gates()) + len(runtime.open_client_tasks())


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter(prefix=API_PREFIX)
    settings = runtime.settings

    # -- instance & household --------------------------------------------

    @router.get("/instance")
    def instance() -> dict:
        return {
            "id": settings.instance_id,
            "title": settings.instance_title,
            "type": settings.instance_type,
            "version": __version__,
            "languages": list(LANGUAGES),
            "default_language": settings.language,
            "model_configured": runtime.model_client is not None,
            "mail_configured": runtime.mail_source is not None,
            "templates": sorted(runtime.engine.definitions),
            "apps": sorted(runtime.apps),
            "open_gates": _open_count(runtime),
            "country": runtime.country(),
            # Local capabilities (ADR-0028/0030): what this machine can read
            # on its own, so the shell can say what is missing.
            "capabilities": {
                "ocr": runtime.ocr_local.name if runtime.ocr_local else None,
                "transcription": runtime.transcriber_local.name
                if runtime.transcriber_local
                else None,
                "ffmpeg": runtime.ffmpeg is not None,
            },
        }

    @router.get("/household")
    def household(request: Request) -> dict:
        return {
            "self": settings.instance_id,
            "members": household_members(runtime, request),
            "launcher": launcher_url(runtime, request),
            "auth_required": bool(getattr(request.app.state, "auth_secret", None)),
        }

    # -- model gateway (ADR-0047) -------------------------------------------

    @router.get("/gateway/models")
    def gateway_models(request: Request) -> dict:
        """What a caller may ask for: the released deployments."""
        _gateway_auth(request)
        return gateway.model_list(runtime.registry, runtime.country())

    @router.post("/gateway/chat/completions")
    async def gateway_chat(request: Request) -> dict:
        """OpenAI-compatible chat completion through this instance's
        registry, rules, budget and log (gateway.py). ``model`` is an
        entry id or a role; the data class travels in
        ``X-Aitmation-Data-Class`` (default internal) and decides which
        deployments qualify."""
        _gateway_auth(request)
        body = await request.json()
        try:
            req = gateway.parse_request(
                body,
                data_class=request.headers.get("x-aitmation-data-class", "internal")
                .strip()
                .lower(),
                task="gateway:" + request.headers.get("x-aitmation-task", "").strip(),
            )
            entry = gateway.resolve_entry(runtime.registry, req.model)
            refused = gateway.admission(entry, req.data_class, runtime.country())
            if refused:
                raise gateway.GatewayError(403, refused)
            client = runtime.gateway_client(entry)
            budget = runtime._budget_refusal(client)
            if budget:
                raise gateway.GatewayError(402, budget)
            flags, blocked = gateway.pre_check(list(settings.guardrails_pre), req.messages)
            if blocked:
                raise gateway.GatewayError(422, blocked)
        except gateway.GatewayError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        started = time.monotonic()
        try:
            result = await run_in_threadpool(gateway.call, client, req)
        except gateway.GatewayError as exc:
            runtime._log_llm(
                runtime.conn, task=req.task, payload={"messages": req.messages}, status="error",
                data_class=req.data_class, model_ref=entry.model,
                duration_ms=int((time.monotonic() - started) * 1000), error=str(exc),
            )
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        runtime._log_llm(
            runtime.conn, task=req.task, payload={"messages": req.messages}, status="ok",
            response={"text": result.text, "flags": flags}, model_ref=result.model_ref,
            data_class=req.data_class, tokens_in=result.tokens_in, tokens_out=result.tokens_out,
            cost_chf=entry.cost_chf(result.tokens_in, result.tokens_out),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        out = gateway.openai_response(result, entry.id)
        if flags:
            out["aitmation"] = {"flags": flags}
        return out

    def _gateway_auth(request: Request) -> None:
        """A bearer gateway token (secret ``gateway_token``) for programs,
        or a signed-in person; an open instance without users is open here
        too, like everywhere else."""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            expected = resolve_secret(settings.data_dir, "gateway_token")
            if gateway.token_ok(auth[7:].strip(), expected):
                return
        if getattr(request.state, "principal", None) is not None:
            return
        if getattr(request.state, "access_key", False):
            return  # the container's shared door (AITMATION_ACCESS_KEY) was passed
        if getattr(request.app.state, "auth_required", False):
            raise HTTPException(status_code=401, detail="gateway token or sign-in required")

    # -- web form channel (docs/VERMITTLER.md, Eingangskanäle) -------------

    @router.post("/intake")
    def intake(body: IntakeBody, request: Request) -> dict:
        """Accept a document plus envelope from a website form. The channel
        is off until AITMATION_INTAKE_TOKEN is set; the token is the form
        handler's credential (header X-Intake-Token), not a user session.
        Files land in the inbox with an envelope sidecar and take the
        normal path: classify → gate → dispatch. Nothing is executed here."""
        expected = getattr(request.app.state, "intake_token", None) or resolve_secret(
            settings.data_dir, "intake_token"
        )
        if not expected:
            raise HTTPException(status_code=404, detail="web intake channel is not enabled")
        given = request.headers.get("x-intake-token", "")
        if not secrets.compare_digest(given, expected):
            raise HTTPException(status_code=401, detail="bad intake token")
        envelope = {
            "source": "web",
            "channel": "web-form",
            "from": body.sender[:200],
            "sender_name": body.sender_name[:200],
        }
        return _accept(body, envelope, "web_form_received")

    @router.post("/upload")
    def upload(body: IntakeBody, request: Request) -> dict:
        """The same hand-in for a signed-in person (or the open instance):
        drag a file into the shell, dictate a note, paste a text. Rung 0 of
        the Vermittler ladder - the sender is known, the document lands in
        this instance's own inbox."""
        principal = getattr(request.state, "principal", None)
        who = principal.sub if principal is not None else (body.sender or "ui")
        envelope = {
            "source": "upload",
            "channel": "ui",
            "from": who[:200],
            "sender_name": (body.sender_name or who)[:200],
        }
        return _accept(body, envelope, "upload_received")

    def _accept(body: IntakeBody, envelope: dict, event: str) -> dict:
        if len(body.files) > INTAKE_MAX_FILES:
            raise HTTPException(status_code=413, detail=f"at most {INTAKE_MAX_FILES} files")
        if not body.files and not body.text.strip():
            raise HTTPException(status_code=422, detail="a file or a text is needed")
        envelope = {
            **envelope,
            "subject": body.subject[:500],
            "text": body.text[:INTAKE_MAX_TEXT],
            "language": body.language if body.language in LANGUAGES else None,
        }
        inbox = settings.inbox_dir
        accepted = []
        for item in body.files or [
            IntakeFile(
                name=(_SAFE_NAME.sub("-", body.subject.strip()) or "nachricht")[:60] + ".txt",
                content_base64=base64.b64encode(body.text.encode("utf-8")).decode(),
            )
        ]:
            try:
                data = base64.b64decode(item.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"{item.name}: not base64") from exc
            if len(data) > INTAKE_MAX_BYTES:
                raise HTTPException(status_code=413, detail=f"{item.name}: too large")
            safe = _SAFE_NAME.sub("-", Path(item.name).name).strip("-.") or "dokument"
            target = inbox / f"{secrets.token_hex(4)}_{safe[:120]}"
            target.write_bytes(data)
            target.with_name(target.name + ENVELOPE_SUFFIX).write_text(
                json.dumps({**envelope, "original_name": safe}, ensure_ascii=False),
                encoding="utf-8",
            )
            accepted.append(target.name)
        runtime.diag("info", "intake", event, f"{len(accepted)} file(s)", sender=envelope["from"])
        return {"accepted": len(accepted), "files": accepted}

    @router.post("/chat")
    def chat(body: ChatMessage) -> dict:
        """Chat as an input path (ZIELBILD §9): recognize which view is
        meant and answer with data the shell renders - open decisions get
        their normal gate controls, so deciding still happens at the gate
        (ADR-0007). Answers are keys plus parameters, never German text."""
        intent = chat_intents.parse(body.message, _starters(runtime))
        if intent.kind == chat_intents.KIND_START:
            # The chat brings the start form, it does not start anything by
            # itself: the person fills the contract and confirms.
            if intent.process_id is None:
                options = [_start_view(runtime, pid) for pid in intent.candidates]
                return {
                    "intent": intent.kind,
                    "answer": "chat.answer.startWhich",
                    "params": {"count": len(options)},
                    "start_options": options,
                }
            view = _start_view(runtime, intent.process_id)
            prefill = {k: _as_text(v) for k, v in view["defaults"].items()}
            if "weekday" in view["contract"]:
                prefill["weekday"] = str(datetime.now().astimezone().isoweekday())
            for field in ("topic", "note", "text"):
                if intent.remainder and field in view["contract"]:
                    prefill[field] = intent.remainder
                    break
            return {
                "intent": intent.kind,
                "answer": "chat.answer.start",
                "params": {"process": pick(view["titles"])},
                "start": {**view, "prefill": prefill},
            }
        if intent.kind == chat_intents.KIND_PENDING:
            with runtime.lock:
                gates = [_gate_view(runtime, g) for g in runtime.open_gates()]
            return {
                "intent": intent.kind,
                "answer": "chat.answer.pending" if gates else "chat.answer.noPending",
                "params": {"count": len(gates)},
                "gates": gates,
            }
        if intent.kind == chat_intents.KIND_CASES:
            # cases()/case() are defined below in this router; by the time a
            # request arrives they exist (closures resolve at call time).
            rows = cases(intent.status, limit=10)
            return {
                "intent": intent.kind,
                "answer": "chat.answer.cases" if rows else "chat.answer.noCases",
                "params": {"count": len(rows), "status": intent.status or "all"},
                "cases": rows,
            }
        if intent.kind == chat_intents.KIND_CASE:
            row = runtime.conn.execute(
                "SELECT id FROM cases WHERE id LIKE ? LIMIT 2", (f"{intent.case_id}%",)
            ).fetchall()
            if len(row) != 1:
                return {
                    "intent": intent.kind,
                    "answer": (
                        "chat.answer.caseNotFound" if not row else "chat.answer.caseAmbiguous"
                    ),
                    "params": {"id": intent.case_id or ""},
                }
            return {
                "intent": intent.kind,
                "answer": "chat.answer.case",
                "params": {"id": row[0]["id"]},
                "case": case(row[0]["id"]),
            }
        return {
            "intent": intent.kind,
            "answer": (
                "chat.answer.help"
                if intent.kind == chat_intents.KIND_HELP
                else "chat.answer.unknown"
            ),
            "params": {},
        }

    # -- pending decisions (Pendenzen) ------------------------------------

    @router.get("/client-tasks")
    def client_tasks_open() -> list[dict]:
        """Open client tasks with their case context - the "deliver this"
        entries of the inbox (ADR-0041 §4)."""
        out = []
        for task in runtime.open_client_tasks(include_claimed=True):
            case = runtime.conn.execute(
                "SELECT template_id FROM cases WHERE id = ?", (task["case_id"],)
            ).fetchone()
            definition = runtime.engine.definitions.get(case["template_id"]) if case else None
            out.append(
                {
                    **task,
                    "template_id": case["template_id"] if case else None,
                    "template_title": _titles(definition.meta.get("title"), definition.id)
                    if definition
                    else {},
                    "app": runtime.template_app.get(case["template_id"]) if case else None,
                }
            )
        return out

    @router.post("/client-tasks/{task_id}/fulfil")
    def client_task_fulfil(task_id: str, body: FulfilBody, request: Request) -> dict:
        content = None
        name = None
        if body.file is not None:
            try:
                content = base64.b64decode(body.file.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(status_code=422, detail="file is not base64") from exc
            if len(content) > 200 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="file too large")
            name = body.file.name
        try:
            result = runtime.fulfil_client_task(
                task_id, actor_of(request, body.actor), data=body.data,
                file_name=name, content=content,
            )
        except EngineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        runtime.tick(wait=False)
        return {"ok": True, "result": {k: v for k, v in result.items() if k != "file"}}

    @router.get("/gates")
    def gates() -> list[dict]:
        # under the lock: the tick shares the connection (a read racing a
        # write on it can come back with a broken row)
        with runtime.lock:
            return [_gate_view(runtime, g) for g in runtime.open_gates()]

    @router.post("/cases/{case_id}/steps/{step_id}/decide")
    def decide(case_id: str, step_id: str, body: Decision, request: Request) -> dict:
        actor = actor_of(request, body.actor)
        try:
            categorizing = body.discard and _is(runtime, case_id, step_id, "categorization")
            if body.category is not None or categorizing:
                runtime.categorize(
                    case_id,
                    step_id,
                    None if body.discard else body.category,
                    actor_id=actor,
                    comment=body.comment,
                )
            elif (
                body.addressees is not None
                or body.addressee is not None
                or (body.discard and _is(runtime, case_id, step_id, "addressing"))
            ):
                target = body.addressees if body.addressees is not None else body.addressee
                runtime.address(
                    case_id,
                    step_id,
                    None if body.discard else target,
                    actor_id=actor,
                    comment=body.comment,
                )
            elif body.outcome:
                runtime.decide(case_id, step_id, body.outcome, actor_id=actor, comment=body.comment)
            else:
                raise HTTPException(status_code=422, detail="no decision given")
        except EngineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    # -- cases (Vorgänge) ---------------------------------------------------

    @router.get("/cases")
    def cases(status: str | None = None, limit: int = 100) -> list[dict]:
        sql = (
            "SELECT id, template_id, template_version, status, current_step_id,"
            " created_at, updated_at, closed_at FROM cases"
        )
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        rows = runtime.conn.execute(sql, (*params, max(1, min(limit, 500)))).fetchall()
        out = []
        for r in rows:
            definition = runtime.engine.definitions.get(r["template_id"])
            titles = definition.meta.get("title") if definition else None
            out.append(
                {
                    **dict(r),
                    "template_title": pick(titles or r["template_id"]),
                    "template_titles": _titles(titles, r["template_id"]),
                    "app": runtime.template_app.get(r["template_id"]),
                    "instance": settings.instance_id,
                    "document": _case_document(runtime, r["id"]),
                }
            )
        return out

    @router.get("/cases/{case_id}")
    def case(case_id: str) -> dict:
        row = runtime.conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown case")
        steps = runtime.conn.execute(
            "SELECT step_id, kind, status, outcome, result, model_ref, deadline_at,"
            " started_at, finished_at FROM steps WHERE case_id = ? ORDER BY started_at",
            (case_id,),
        ).fetchall()
        audit = runtime.conn.execute(
            "SELECT seq, at, actor_type, actor_id, event, details, app_version, model_ref"
            " FROM audit_log WHERE case_id = ? ORDER BY seq",
            (case_id,),
        ).fetchall()
        definition = runtime.engine.definitions.get(row["template_id"])
        titles = definition.meta.get("title") if definition else None
        return {
            **dict(row),
            "template_title": pick(titles or row["template_id"]),
            "template_titles": _titles(titles, row["template_id"]),
            "app": runtime.template_app.get(row["template_id"]),
            "instance": settings.instance_id,
            "document": _case_document(runtime, case_id),
            "results": _case_results(runtime, case_id),
            "stub_steps": _stub_steps(runtime, case_id),
            "media": _case_media(runtime, case_id),
            "summary": _case_summary(runtime, case_id),
            **_case_relatives(runtime, row),
            "steps": [
                {**dict(s), "result": json.loads(s["result"]) if s["result"] else None}
                for s in steps
            ],
            "audit": [
                {**dict(a), "details": json.loads(a["details"]) if a["details"] else None}
                for a in audit
            ],
        }

    # -- catalog: shop v0 (ADR-0043 §4, Etappe A) --------------------------
    #
    # The catalog is a signed file on a plain https host; the instance
    # verifies the platform's signature, shows entries with their trust
    # level and installs after digest, validator and trust check. Nothing
    # here needs an account; revocations travel in the catalog.

    _catalog_cache: dict = {"at": None, "catalog": None, "error": None}

    def _core_definitions() -> dict:
        from .template import load_templates

        templates_dir = runtime.settings.templates_dir
        return load_templates(templates_dir) if templates_dir.is_dir() else {}

    def _load_catalog(force: bool = False) -> tuple[shop.Catalog | None, str | None]:
        url = runtime.settings.catalog_url
        if not url:
            return None, None
        now = datetime.now(UTC)
        cached_at = _catalog_cache["at"]
        if not force and cached_at and (now - cached_at).total_seconds() < 600:
            return _catalog_cache["catalog"], _catalog_cache["error"]
        try:
            catalog = shop.fetch_catalog(
                url, runtime.settings.trust_keys, transport=runtime._http_transport,
                allow_private=runtime.settings.tools_allow_private_hosts,
            )
            error = None
        except shop.CatalogError as exc:
            catalog, error = None, str(exc)
            runtime.diag("warning", "catalog", "catalog_unavailable", error)
        _catalog_cache.update({"at": now, "catalog": catalog, "error": error})
        if catalog is not None:
            for item in catalog.revoked:
                app = runtime.apps.get(str(item.get("id", "")))
                if app is not None and (
                    not item.get("version") or app.version == str(item.get("version"))
                ):
                    runtime.diag(
                        "warning", "catalog", "solution_revoked",
                        f"{app.id} {app.version}: {item.get('reason', 'revoked by the platform')}",
                    )
        return catalog, error

    def _catalog_view(catalog: shop.Catalog | None, error: str | None) -> dict:
        # What is really loaded - a stale installed_apps row of a bundle
        # that is no longer on disk must not read as "installed".
        installed_versions = {app_id: app.version for app_id, app in runtime.apps.items()}
        entries = []
        if catalog is not None:
            for e in catalog.entries:
                revoked = next(
                    (
                        r for r in catalog.revoked
                        if r.get("id") == e.id
                        and (not r.get("version") or str(r.get("version")) == e.version)
                    ),
                    None,
                )
                current = installed_versions.get(e.id)
                entries.append(
                    {
                        "id": e.id, "version": e.version, "name": e.name,
                        "description": e.description, "publisher": e.publisher,
                        "trust_level": e.trust_level, "requires": e.requires,
                        "model_roles": e.model_roles, "dependencies": e.dependencies,
                        "size": e.size,
                        "installed_version": current,
                        "state": (
                            "installed" if current == e.version
                            else "update" if current else "available"
                        ),
                        "installable": e.trust_level in runtime.settings.trusted_levels,
                        "revoked": revoked,
                    }
                )
        revoked_installed = [
            {
                "id": r.get("id"), "version": r.get("version"), "reason": r.get("reason"),
                "installed_version": installed_versions.get(str(r.get("id"))),
            }
            for r in (catalog.revoked if catalog else [])
            if str(r.get("id")) in installed_versions
            and (
                not r.get("version")
                or installed_versions[str(r.get("id"))] == str(r.get("version"))
            )
        ]
        return {
            "url": runtime.settings.catalog_url,
            "generated_at": catalog.generated_at if catalog else None,
            "signed_by": catalog.signed_by if catalog else None,
            "error": error,
            "trusted_levels": list(runtime.settings.trusted_levels),
            "entries": entries,
            "revoked_installed": revoked_installed,
            "fetched_at": (
                _catalog_cache["at"].isoformat(timespec="seconds").replace("+00:00", "Z")
                if _catalog_cache["at"] else None
            ),
        }

    @router.get("/catalog")
    def catalog_get(refresh: bool = False) -> dict:
        catalog, error = _load_catalog(force=refresh)
        return _catalog_view(catalog, error)

    def _install(raw: bytes, request: Request, expected_id: str | None) -> dict:
        _require_admin(request)
        apps_dir = runtime.settings.apps_dir
        try:
            result = shop.install_bundle(
                raw, apps_dir,
                keys=runtime.settings.trust_keys,
                trusted_levels=runtime.settings.trusted_levels,
                core_definitions=_core_definitions(),
                retired_dir=runtime.settings.data_dir / "retired",
                expected_id=expected_id,
            )
        except shop.CatalogError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            runtime.reload_templates()
        except Exception as exc:
            # The new bundle broke the load: put the old one back (never
            # leave the instance worse than before) and report.
            shutil.rmtree(result.path, ignore_errors=True)
            retired = sorted((runtime.settings.data_dir / "retired").glob(f"{result.id}-*"))
            if result.replaced_version and retired:
                shutil.move(str(retired[-1]), str(result.path))
                runtime.reload_templates()
            raise HTTPException(
                status_code=422, detail=f"bundle installed but the reload failed: {exc}"
            ) from exc
        runtime.diag(
            "info", "catalog", "solution_installed",
            f"{result.id} {result.version} ({result.trust.level})"
            + (f", replaced {result.replaced_version}" if result.replaced_version else ""),
            actor=actor_of(request, "admin"),
        )
        return {
            "ok": True, "id": result.id, "version": result.version,
            "replaced_version": result.replaced_version,
            "trust": {
                "level": result.trust.level, "verified": result.trust.verified,
                "reason": result.trust.reason, "signed_by": result.trust.signed_by,
            },
        }

    @router.post("/catalog/install")
    def catalog_install(body: CatalogInstall, request: Request) -> dict:
        _require_admin(request)
        catalog, error = _load_catalog()
        if catalog is None:
            raise HTTPException(status_code=503, detail=error or "no catalog")
        entry = next(
            (e for e in catalog.entries if e.id == body.id and e.version == body.version), None
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="no such catalog entry")
        try:
            raw = shop.fetch_bundle(
                catalog, entry, transport=runtime._http_transport,
                allow_private=runtime.settings.tools_allow_private_hosts,
            )
        except shop.CatalogError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _install(raw, request, entry.id)

    @router.post("/catalog/install-file")
    def catalog_install_file(body: BundleUpload, request: Request) -> dict:
        _require_admin(request)
        try:
            raw = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail="content is not base64") from exc
        if len(raw) > shop.MAX_BUNDLE_BYTES:
            raise HTTPException(status_code=413, detail="bundle too large")
        return _install(raw, request, None)

    @router.get("/solutions/{app_id}/export")
    def solution_export(app_id: str, request: Request) -> FileResponse:
        """The bundle of an installed solution as a file - what a publisher
        hands in to the shop or shares in the community (ADR-0043). The
        validator runs first; the signature is the platform's job."""
        _require_admin(request)
        app = runtime.apps.get(app_id)
        if app is None or app_id == CORE_SOLUTION:
            raise HTTPException(status_code=404, detail="unknown solution")
        from .apps import validate_app

        report = validate_app(app.path, core_definitions=_core_definitions(), provided=runtime.apps)
        if not report.ok:
            raise HTTPException(
                status_code=422,
                detail="solution fails validation: " + "; ".join(str(f) for f in report.errors),
            )
        out_dir = Path(tempfile.mkdtemp(prefix="aitmation-export-"))
        bundle = shop.pack_solution(app.path, out_dir)
        return FileResponse(
            bundle, media_type="application/gzip", filename=bundle.name,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    # -- apps ---------------------------------------------------------------

    def _declared_dependencies(app_id: str, app) -> list[str]:
        if app is not None:
            return sorted(app.manifest.dependencies)
        manifest = runtime.settings.apps_dir / app_id / "solution.toml"
        try:
            raw = tomllib.loads(manifest.read_text(encoding="utf-8")).get("dependencies")
        except (OSError, tomllib.TOMLDecodeError):
            return []
        return sorted(str(k) for k in raw) if isinstance(raw, dict) else []

    def _app_view(row: dict) -> dict:
        app = runtime.apps.get(row["id"])
        view = {**row, "enabled": bool(row["enabled"]), "loaded": app is not None}
        try:
            view["settings_values"] = json.loads(row.get("settings_json") or "{}")
        except json.JSONDecodeError:
            view["settings_values"] = {}
        verdict = runtime.app_trust.get(row["id"])
        if verdict is not None:
            view["trust"] = {
                "level": verdict.level, "verified": verdict.verified,
                "reason": verdict.reason, "signed_by": verdict.signed_by,
            }
        # What the bundle builds on - also for a switched-off one, which is
        # not loaded: who switches solutions on and off needs the order.
        view["dependencies"] = _declared_dependencies(row["id"], app)
        if app is not None:
            m = app.manifest
            view.update(
                {
                    "name": m.name,
                    "description": m.description,
                    "license": m.license,
                    "author": m.author,
                    "publisher": m.publisher,
                    "requires": {
                        "core": m.requires.core,
                        "data_class": m.requires.data_class,
                        "connectors": list(m.requires.connectors),
                        "guardrails": list(m.requires.guardrails),
                    },
                    "settings": [
                        {
                            "name": s.name,
                            "type": s.type,
                            "label": s.label,
                            "help": s.help,
                            "required": s.required,
                            "default": s.default,
                            "choices": list(s.choices),
                            "binds": list(s.binds),
                        }
                        for s in m.settings.values()
                    ],
                    "templates": [
                        {"id": t.id, "title": t.meta.get("title", t.id), "steps": len(t.steps)}
                        for t in app.templates.values()
                    ],
                    "doctypes": [
                        {"id": d.id, "title": d.titles, "chain": d.chain}
                        for d in app.doctypes.values()
                    ],
                    "tests": [c.id for c in app.tests],
                }
            )
        return view

    # -- the document itself ------------------------------------------------
    #
    # A decision needs the document, not just its name (first Windows
    # run): the file is served inline to the signed-in person, and a text
    # preview lets the gate be decided without leaving the page. Both stay
    # inside the instance - nothing here reaches a model or another host.

    def _case_file(case_id: str, index: int) -> tuple[Path, str]:
        files = runtime._case_files(case_id)
        if not files:
            raise HTTPException(status_code=404, detail="this case has no document")
        if index < 0 or index >= len(files):
            raise HTTPException(status_code=404, detail="no such attachment")
        path = files[index]
        if not path.is_file():
            raise HTTPException(
                status_code=410, detail="document file is gone (forwarded or archived)"
            )
        doc = _case_document(runtime, case_id) or {}
        name = str(doc.get("original_name") or path.name)
        return path, name

    @router.get("/cases/{case_id}/document")
    def case_document(case_id: str, index: int = 0) -> FileResponse:
        path, name = _case_file(case_id, index)
        media = (
            "application/pdf" if path.suffix.lower() == ".pdf" else media_type_of(path)
        ) or "text/plain; charset=utf-8"
        return FileResponse(
            path, media_type=media, filename=name, content_disposition_type="inline"
        )

    @router.get("/cases/{case_id}/media/{name}")
    def case_media_file(case_id: str, name: str) -> FileResponse:
        """A medium a person or actuator delivered for this case (ADR-0041)."""
        if not re.match(r"^[a-f0-9]{32}$", case_id) or name != Path(name).name:
            raise HTTPException(status_code=404, detail="unknown medium")
        path = runtime.settings.files_dir / "media" / case_id / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="unknown medium")
        # Only media render inline; anything else (an .html delivered by an
        # actuator, say) is a download, never a page on this origin.
        media = media_type_of(path) or "application/octet-stream"
        inline = media.startswith(("image/", "video/", "audio/"))
        response = FileResponse(
            path, media_type=media if inline else "application/octet-stream", filename=name,
            content_disposition_type="inline" if inline else "attachment",
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @router.get("/cases/{case_id}/document/text")
    def case_document_text(case_id: str, index: int = 0) -> dict:
        path, name = _case_file(case_id, index)
        # A recording is transcribed by the processing tick, never inside a
        # request (minutes of CPU, ADR-0030); until then the preview waits.
        if is_media(path) and cached_text(path) is None:
            return {"name": name, "text": None, "pending": True, "truncated": False}
        try:
            extracted = extract_text(
                path, ocr=runtime.ocr_local, transcriber=runtime.transcriber_local,
                ffmpeg=runtime.ffmpeg,
            )
        except DocumentError as exc:
            return {"name": name, "text": None, "error": str(exc), "truncated": False}
        text = extracted.text
        return {
            "name": name,
            "text": text[:PREVIEW_CHARS],
            "truncated": len(text) > PREVIEW_CHARS,
            "page_count": extracted.page_count,
            "method": extracted.method,
        }

    @router.get("/apps")
    def apps() -> list[dict]:
        rows = runtime.conn.execute(
            "SELECT id, version, format, enabled, installed_at, updated_at, settings_json"
            " FROM installed_apps ORDER BY id"
        ).fetchall()
        return [_app_view(dict(r)) for r in rows]

    @router.get("/apps/{app_id}")
    def app_detail(app_id: str) -> dict:
        row = runtime.conn.execute(
            "SELECT id, version, format, enabled, installed_at, updated_at, settings_json"
            " FROM installed_apps WHERE id = ?",
            (app_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown app")
        return _app_view(dict(row))

    @router.post("/apps/{app_id}/enable")
    def app_enable(app_id: str, body: AppSwitch, request: Request) -> dict:
        try:
            runtime.set_app_enabled(app_id, body.enabled, actor_id=actor_of(request, body.actor))
        except (AppError, TemplateError, DoctypeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "enabled": body.enabled}

    @router.put("/apps/{app_id}/settings")
    def app_settings(app_id: str, body: AppSettings, request: Request) -> dict:
        app = runtime.apps.get(app_id)
        if app is None:
            raise HTTPException(status_code=404, detail="unknown or disabled app")
        unknown = set(body.values) - set(app.manifest.settings)
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown settings {sorted(unknown)}")
        missing = [
            s.name
            for s in app.manifest.settings.values()
            if s.required and body.values.get(s.name) in (None, "")
        ]
        if missing:
            raise HTTPException(status_code=422, detail=f"required settings missing {missing}")
        runtime.save_app_settings(app_id, dict(body.values), actor_id=actor_of(request, body.actor))
        return {"ok": True}

    # -- settings: models, logs, doctypes, addressees, mandates -------------

    # -- models, secrets, legal space (ADR-0017 / ADR-0027) --------------

    def _model_view(e) -> dict:
        country = runtime.country()
        return {
            **e.as_dict(),
            "permitted_up_to": legal.permitted_up_to(country, e),
            "missing_secret": providers.missing_secret(e, settings),
            "from_env": _from_env(e.id),
        }

    def _from_env(entry_id: str) -> bool:
        """The entry the LLM_* variables describe: it wins over the file,
        so it is changed where it is set (Coolify, .env), not here."""
        try:
            env = model_registry.env_entry(dict(os.environ))
        except RegistryError:
            return False
        return env is not None and env.id == entry_id

    @router.get("/models")
    def models() -> list[dict]:
        return [_model_view(e) for e in runtime.registry.values()]

    @router.get("/models/jurisdiction")
    def models_jurisdiction() -> dict:
        country = runtime.country()
        jurisdiction = legal.jurisdiction_for(country)
        return {
            "country": country,
            "jurisdiction": jurisdiction.as_dict() if jurisdiction else None,
            "known": sorted(legal.JURISDICTIONS),
            "providers": list(PROVIDERS),
            "regions": list(REGIONS),
            "secrets": secret_status(settings.data_dir),
        }

    @router.put("/models/{entry_id}")
    def model_save(entry_id: str, body: ModelBody, request: Request) -> dict:
        """Create or change one registry entry; the registry file is the
        record of the owner's release decision. Admin only when the
        household has users. Clients are rebuilt afterwards."""
        _require_admin(request)
        if not _ENTRY_ID.match(entry_id):
            raise HTTPException(status_code=422, detail="entry id: lowercase letters, digits, -")
        if _from_env(entry_id):
            raise HTTPException(status_code=409, detail=_ENV_ENTRY)
        try:
            entry = entry_from_dict(entry_id, body.model_dump(exclude_none=True))
        except RegistryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        entries = dict(runtime.registry)
        entries[entry_id] = entry
        _write_registry(entries)
        runtime.diag("info", "models", "model_saved", entry_id, actor=actor_of(request, "ui"))
        return _model_view(runtime.registry[entry_id])

    @router.delete("/models/{entry_id}")
    def model_delete(entry_id: str, request: Request) -> dict:
        _require_admin(request)
        if entry_id not in runtime.registry:
            raise HTTPException(status_code=404, detail=f"unknown model {entry_id!r}")
        if _from_env(entry_id):
            raise HTTPException(status_code=409, detail=_ENV_ENTRY)
        entries = dict(runtime.registry)
        del entries[entry_id]
        _write_registry(entries)
        runtime.diag("info", "models", "model_removed", entry_id, actor=actor_of(request, "ui"))
        return {"ok": True}

    @router.post("/models/{entry_id}/test")
    def model_test(entry_id: str, request: Request) -> dict:
        """Connection test for one deployment: a trivial structured call
        (Ollama: list of pulled models first, so a missing pull is named)."""
        _require_admin(request)
        entry = runtime.registry.get(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown model {entry_id!r}")
        client = providers.client_for(entry, settings)
        if client is None:
            return {"ok": False, "reason": "missing_secret",
                    "secret": providers.missing_secret(entry, settings)}
        try:
            check = getattr(client, "check", None)
            if check is not None:
                available = check()
                if entry.model not in available and not any(
                    a.startswith(entry.model + ":") for a in available
                ):
                    return {"ok": False, "reason": "model_not_pulled", "available": available}
            task = 'Reply with {"ok": true, "confidence": 1.0} and nothing else.'
            started = time.monotonic()
            response = client.complete_structured(task, {"ping": True})
        except ModelError as exc:
            raw = getattr(exc, "raw", None)
            runtime._log_llm(
                runtime.conn, task="model-test", payload={"model": entry.model, "task": task},
                status="error", model_ref=entry.model, error=str(exc),
                response={"raw": raw} if raw else None,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return {"ok": False, "reason": "call_failed", "detail": str(exc), "raw": raw}
        runtime._log_llm(
            runtime.conn, task="model-test", payload={"model": entry.model, "task": task},
            status="ok", model_ref=response.model_ref, response=response.data,
            tokens_in=response.tokens_in, tokens_out=response.tokens_out,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return {"ok": True, "model_ref": response.model_ref}

    def _write_registry(entries: dict) -> None:
        path = settings.models_path
        previous = path.read_text(encoding="utf-8") if path.is_file() else None
        # the LLM_* entry is added at every load; it never goes into the file
        save_registry(path, {k: v for k, v in entries.items() if not _from_env(k)})
        try:
            runtime.reconfigured()
        except (RegistryError, TemplateError, AppError, DoctypeError) as exc:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(previous, encoding="utf-8")
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/secrets")
    def secrets_overview(request: Request) -> dict:
        """Which secrets are set and where from - never the values."""
        _require_admin(request)
        return secret_status(settings.data_dir)

    @router.put("/secrets/{name}")
    def secret_set(name: str, body: SecretBody, request: Request) -> dict:
        """Store (or clear, with an empty value) one secret in the owner-only
        file; an environment-provided secret cannot be changed here."""
        _require_admin(request)
        if not is_valid_secret_name(name):
            raise HTTPException(status_code=404, detail=f"invalid secret name {name!r}")
        if secret_status(settings.data_dir).get(name) == "env":
            raise HTTPException(
                status_code=409, detail="this secret comes from the environment; change it there"
            )
        save_secret(settings.data_dir, name, body.value.strip())
        runtime.diag(
            "info", "secrets", "secret_set" if body.value.strip() else "secret_cleared", name,
            actor=actor_of(request, "ui"),
        )
        runtime.reconfigured()
        return secret_status(settings.data_dir)

    # -- MCP servers (ADR-0061) ---------------------------------------------

    def _mcp_servers() -> dict:
        try:
            return mcp_servers.load(settings.data_dir)
        except mcp_servers.McpConfigError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    def _mcp_kw() -> dict:
        return {
            "transport": runtime._http_transport,
            "allow_private": settings.tools_allow_private_hosts,
            "allow_stdio": settings.mcp_allow_stdio,
        }

    def _tool_secret(name: str):
        return resolve_secret(settings.data_dir, name) if is_tool_secret(name) else None

    @router.get("/mcp/servers")
    def mcp_server_list(request: Request) -> dict:
        """The instance's MCP servers - addresses, allowlists, which secrets
        they need (set or not); never a secret's value."""
        _require_admin(request)
        status = secret_status(settings.data_dir)
        out = []
        for server in _mcp_servers().values():
            view = server.view()
            view["missing_secrets"] = [n for n in view["secrets"] if not status.get(n)]
            out.append(view)
        return {"stdio_allowed": settings.mcp_allow_stdio, "servers": out}

    @router.put("/mcp/servers/{name}")
    def mcp_server_put(name: str, body: McpServerBody, request: Request) -> dict:
        """Add or change a server. A local command (stdio) only when the
        operator allowed it - an admin must not run programs on the host."""
        _require_admin(request)
        if body.command and not settings.mcp_allow_stdio:
            raise HTTPException(
                status_code=403,
                detail="stdio servers are not allowed here (operator: AITMATION_MCP_ALLOW_STDIO)",
            )
        try:
            server = mcp_servers.parse_server(name, body.model_dump(exclude_defaults=False))
        except mcp_servers.McpConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        servers = _mcp_servers()
        servers[name] = server
        mcp_servers.save(settings.data_dir, servers)
        runtime.diag("info", "mcp", "server_saved", name, actor=actor_of(request, "ui"))
        return server.view()

    @router.delete("/mcp/servers/{name}")
    def mcp_server_delete(name: str, request: Request) -> dict:
        _require_admin(request)
        servers = _mcp_servers()
        if servers.pop(name, None) is None:
            raise HTTPException(status_code=404, detail=f"no MCP server {name!r}")
        mcp_servers.save(settings.data_dir, servers)
        runtime.diag("info", "mcp", "server_removed", name, actor=actor_of(request, "ui"))
        return {"ok": True}

    @router.post("/mcp/servers/{name}/tools")
    def mcp_server_tools(name: str, request: Request) -> dict:
        """Ask the server which tools it offers (live); ``allowed`` marks
        the ones on this instance's allowlist."""
        _require_admin(request)
        server = _mcp_servers().get(name)
        if server is None:
            raise HTTPException(status_code=404, detail=f"no MCP server {name!r}")
        try:
            tools = mcp_client.list_tools(server, _tool_secret, **_mcp_kw())
        except mcp_client.McpError as exc:
            raise HTTPException(status_code=424, detail=str(exc)) from exc
        return {"server": name, "tools": [
            {"name": t.get("name"), "title": t.get("title") or "",
             "description": str(t.get("description") or "")[:1000],
             "input_schema": t.get("inputSchema") or {}, "annotations": t.get("annotations") or {},
             "allowed": t.get("name") in server.tools}
            for t in tools
        ]}

    @router.post("/solutions/{app_id}/elements/tool/from-mcp", status_code=201)
    def solution_tool_from_mcp(app_id: str, body: McpToolBody, request: Request) -> dict:
        """A tool declaration for one tool of a server: inputs from its
        input schema, every input passed as an argument of the same name.
        The allowlist stays the admin's separate decision."""
        _require_admin(request)
        app = _solution(app_id)
        if not _writable(app):
            raise HTTPException(status_code=409, detail="solution directory is not writable")
        server = _mcp_servers().get(body.server)
        if server is None:
            raise HTTPException(status_code=404, detail=f"no MCP server {body.server!r}")
        try:
            tools = mcp_client.list_tools(server, _tool_secret, **_mcp_kw())
        except mcp_client.McpError as exc:
            raise HTTPException(status_code=424, detail=str(exc)) from exc
        found = next((t for t in tools if t.get("name") == body.tool), None)
        if found is None:
            raise HTTPException(status_code=404, detail=f"server has no tool {body.tool!r}")
        tool_id = body.id or re.sub(
            r"[^a-z0-9-]+", "-", f"{body.server}-{body.tool}".lower()
        ).strip("-")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,59}", tool_id):
            raise HTTPException(status_code=422, detail="tool id: a-z, 0-9, - and _")
        rel = f"tools/{tool_id}.toml"
        if (app.path / rel).exists():
            raise HTTPException(status_code=409, detail=f"{rel} already exists")
        files = {rel: mcp_tool_declaration(tool_id, body.server, found)}
        warnings = _write_validated(
            app, files, actor_of(request, body.actor),
            f"tool {tool_id} from MCP {body.server}/{body.tool} in {app_id}",
        )
        if body.tool not in server.tools:
            warnings.append(f"{body.tool!r} is not on the allowlist of {body.server!r} yet")
        return {"ok": True, "id": tool_id, "files": [rel], "warnings": warnings}

    # -- knowledge spaces (ADR-0031) ----------------------------------------

    @router.get("/knowledge")
    def knowledge_spaces() -> dict:
        return {
            "spaces": runtime.knowledge.overview(),
            "embeddings": runtime.knowledge.embedder.name if runtime.knowledge.embedder else None,
        }

    @router.get("/knowledge/{space_id}")
    def knowledge_space(space_id: str) -> dict:
        space = runtime.knowledge.space(space_id)
        if space is None:
            raise HTTPException(status_code=404, detail=f"unknown space {space_id!r}")
        view = space.as_dict()
        view["documents"] = runtime.knowledge.documents(space_id)
        view["sources"] = _sources_view(space_id)
        return view

    @router.put("/knowledge/{space_id}")
    def knowledge_space_save(space_id: str, body: SpaceBody, request: Request) -> dict:
        _require_admin(request)
        try:
            space = runtime.knowledge.save_space(
                space_id,
                title=body.title,
                description=body.description,
                data_class=body.data_class,
                owner=body.owner.strip(),
            )
        except KnowledgeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.diag(
            "info", "knowledge", "space_saved", space_id, actor=actor_of(request, "ui")
        )
        return space.as_dict()

    @router.delete("/knowledge/{space_id}")
    def knowledge_space_archive(space_id: str, request: Request) -> dict:
        """Archive: the space and its documents stay (nothing is deleted),
        but no step may bind to it any more - fail-closed from now on."""
        _require_admin(request)
        try:
            runtime.knowledge.archive_space(space_id)
        except KnowledgeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        runtime.diag(
            "info", "knowledge", "space_archived", space_id, actor=actor_of(request, "ui")
        )
        return {"ok": True}

    @router.post("/knowledge/{space_id}/documents")
    def knowledge_document_add(
        space_id: str, body: KnowledgeDocumentBody, request: Request
    ) -> dict:
        """Upload = new document or (with ``replace``) the next version of
        one. Read with the local OCR / transcription only - a vision model
        never sees knowledge on the way in."""
        _require_admin(request)
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=422, detail="content_base64 is not base64") from exc
        if len(content) > INTAKE_MAX_BYTES:
            raise HTTPException(status_code=413, detail="document too large")
        try:
            doc = runtime.add_knowledge_document(
                space_id,
                body.name,
                content,
                title=body.title,
                actor=actor_of(request, "ui"),
                replace=body.replace,
            )
        except KnowledgeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        runtime.diag(
            "info", "knowledge", "document_indexed" if doc["status"] == "indexed"
            else "document_failed", f"{space_id}/{doc['id']} v{doc['version']}",
            actor=actor_of(request, "ui"),
        )
        return doc

    @router.get("/knowledge/{space_id}/documents/{document_id}/file")
    def knowledge_document_file(space_id: str, document_id: str) -> FileResponse:
        doc = runtime.knowledge.document(document_id)
        if doc is None or doc["space"] != space_id:
            raise HTTPException(status_code=404, detail="unknown document")
        path = runtime.knowledge.document_path(document_id)
        return FileResponse(
            path,
            media_type=media_type_of(path)
            or ("application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream"),
            filename=doc["file_name"],
            content_disposition_type="inline",
        )

    @router.delete("/knowledge/{space_id}/documents/{document_id}")
    def knowledge_document_delete(space_id: str, document_id: str, request: Request) -> dict:
        _require_admin(request)
        try:
            with runtime.lock:
                runtime.knowledge.delete_document(space_id, document_id)
        except KnowledgeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        runtime.diag(
            "info", "knowledge", "document_deleted", f"{space_id}/{document_id}",
            actor=actor_of(request, "ui"),
        )
        return {"ok": True}

    @router.get("/knowledge/{space_id}/search")
    def knowledge_search(space_id: str, q: str = "", top_k: int = 5) -> list[dict]:
        if runtime.knowledge.space(space_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown space {space_id!r}")
        return [h.as_dict() for h in runtime.knowledge.search([space_id], q, top_k)]

    # -- web sources of a space (ADR-0045) ---------------------------------

    def _sources_view(space_id: str) -> list[dict]:
        out = runtime.knowledge_web.list(space_id)
        for view in out:
            view["running"] = runtime.crawling(view["id"])
        return out

    def _source_of(space_id: str, source_id: str) -> dict:
        view = runtime.knowledge_web.get(source_id)
        if view is None or view["space"] != space_id:
            raise HTTPException(status_code=404, detail="unknown source")
        view["running"] = runtime.crawling(source_id)
        return view

    def _save_source(
        space_id: str, body: KnowledgeSourceBody, request: Request, source_id: str | None
    ) -> dict:
        _require_admin(request)
        space = runtime.knowledge.space(space_id)
        if space is None or space.archived_at is not None:
            raise HTTPException(status_code=404, detail=f"unknown space {space_id!r}")
        try:
            spec = make_source_spec(
                body.url, scope=body.scope, doc_types=body.doc_types,
                max_pages=body.max_pages, max_depth=body.max_depth, exclude=body.exclude,
                allow_private=runtime.settings.tools_allow_private_hosts,
            )
            with runtime.lock:
                view = runtime.knowledge_web.save(
                    space_id, spec, title=body.title, interval_hours=body.interval_hours,
                    enabled=body.enabled, actor=actor_of(request, "ui"), source_id=source_id,
                )
        except SourceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.diag(
            "info", "knowledge", "source_saved", f"{space_id}/{view['id']} {spec.url}",
            actor=actor_of(request, "ui"),
        )
        view["running"] = runtime.crawling(view["id"])
        return view

    @router.get("/knowledge/{space_id}/sources")
    def knowledge_sources(space_id: str) -> list[dict]:
        if runtime.knowledge.space(space_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown space {space_id!r}")
        return _sources_view(space_id)

    @router.post("/knowledge/{space_id}/sources")
    def knowledge_source_add(space_id: str, body: KnowledgeSourceBody, request: Request) -> dict:
        """A new source is due at once: the next tick starts its first crawl."""
        return _save_source(space_id, body, request, None)

    @router.put("/knowledge/{space_id}/sources/{source_id}")
    def knowledge_source_save(
        space_id: str, source_id: str, body: KnowledgeSourceBody, request: Request
    ) -> dict:
        _source_of(space_id, source_id)
        return _save_source(space_id, body, request, source_id)

    @router.delete("/knowledge/{space_id}/sources/{source_id}")
    def knowledge_source_delete(space_id: str, source_id: str, request: Request) -> dict:
        """Remove the source and the documents it brought in (soft, like
        every knowledge deletion - the originals stay on disk)."""
        _require_admin(request)
        _source_of(space_id, source_id)
        with runtime.lock:
            documents = runtime.knowledge_web.delete(source_id)
            for document_id in documents:
                with contextlib.suppress(KnowledgeError):
                    runtime.knowledge.delete_document(space_id, document_id)
        runtime.diag(
            "info", "knowledge", "source_deleted",
            f"{space_id}/{source_id} ({len(documents)} document(s))",
            actor=actor_of(request, "ui"),
        )
        return {"ok": True, "documents_removed": len(documents)}

    @router.post("/knowledge/{space_id}/sources/{source_id}/run")
    def knowledge_source_run(space_id: str, source_id: str, request: Request) -> dict:
        """Crawl now (in the background); 409 while a crawl is running."""
        _require_admin(request)
        _source_of(space_id, source_id)
        with runtime.lock:
            started = runtime.start_crawl(source_id, actor=actor_of(request, "ui"))
        if not started:
            raise HTTPException(status_code=409, detail="a crawl of this source is running")
        return _source_of(space_id, source_id)

    # -- key figures (ADR-0029) -------------------------------------------

    def _approving_map() -> dict[tuple[str, str], set[str]]:
        out: dict[tuple[str, str], set[str]] = {}
        for definition in runtime.engine.definitions.values():
            for step in definition.steps.values():
                if step.kind is StepKind.HUMAN_GATE:
                    out[(definition.id, step.id)] = step.approving_outcomes()
        return out

    @router.get("/metrics")
    def metrics_overview(days: int = 30) -> dict:
        """Costs, gate decisions and case counts over the last ``days``
        (7 | 30 | 90 | 0 = all) plus the state of the monthly budget."""
        data = metrics.overview(
            runtime.conn,
            days,
            approving=_approving_map(),
            monthly_budget_chf=float(runtime.budget_config.get("monthly_budget_chf") or 0.0),
            budget_stop=bool(runtime.budget_config.get("budget_stop")),
        )
        titles = {d.id: d.meta.get("title", d.id) for d in runtime.engine.definitions.values()}
        for row in data["llm"]["per_template"] + data["cases"]["per_template"] + data["gates"]:
            row["template_title"] = titles.get(row["template_id"])
        return data

    @router.get("/operations")
    def operations_overview(days: int = 7) -> dict:
        """The operations page (Verwaltung → Betrieb): running cases and
        what they wait on, recent runs, upcoming schedules, device tasks,
        errors of the last 24 h, tokens and cost per day and per process."""
        data = operations.overview(
            runtime.conn,
            days,
            schedules=runtime.schedules(),
            monthly_budget_chf=float(runtime.budget_config.get("monthly_budget_chf") or 0.0),
            budget_stop=bool(runtime.budget_config.get("budget_stop")),
        )
        titles = {d.id: d.meta.get("title", d.id) for d in runtime.engine.definitions.values()}
        for row in (
            data["running"] + data["recent"] + data["cases"]["per_template"]
            + data["tokens"]["per_template"]
        ):
            row["template_title"] = titles.get(row["template_id"])
        return data

    @router.put("/metrics/budget")
    def metrics_budget(body: BudgetBody, request: Request) -> dict:
        _require_admin(request)
        if body.monthly_budget_chf < 0:
            raise HTTPException(status_code=422, detail="monthly_budget_chf must be >= 0")
        values = {
            "monthly_budget_chf": float(body.monthly_budget_chf),
            "budget_stop": bool(body.budget_stop),
        }
        runtime.budget_config.update(values)
        save_config_values(settings, values)
        runtime.diag(
            "info", "budget", "budget_set",
            f"{values['monthly_budget_chf']} CHF/month, stop={values['budget_stop']}",
            actor=actor_of(request, "ui"),
        )
        return runtime.budget_state()

    @router.get("/log/llm")
    def llm_log(limit: int = 50) -> list[dict]:
        rows = runtime.conn.execute(
            "SELECT id, at, case_id, step_id, task, model_ref, datenklasse AS data_class,"
            " tokens_in, tokens_out, kosten_chf AS cost_chf, duration_ms, status, error"
            " FROM llm_interactions ORDER BY at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [dict(r) for r in rows]

    @router.get("/log/llm/{interaction_id}")
    def llm_log_entry(interaction_id: str, request: Request) -> dict:
        """One model call in full: the task text, the (redacted) input,
        the parsed response or the raw text of a failed call, and the
        error. Admin only - the input may quote the document."""
        _require_admin(request)
        row = runtime.conn.execute(
            "SELECT id, at, case_id, step_id, task, input, response, model_ref,"
            " datenklasse AS data_class, tokens_in, tokens_out, kosten_chf AS cost_chf,"
            " duration_ms, status, error FROM llm_interactions WHERE id = ?",
            (interaction_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown interaction")
        view = dict(row)
        for key in ("input", "response"):
            if view.get(key):
                with contextlib.suppress(ValueError):
                    view[key] = json.loads(view[key])
        return view

    @router.get("/log/diagnostics")
    def diagnostics(limit: int = 50) -> list[dict]:
        rows = runtime.conn.execute(
            "SELECT * FROM diagnostic_events ORDER BY at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [dict(r) for r in rows]

    def _doctype_view(d: Doctype) -> dict:
        return {
            "id": d.id,
            "title": d.titles,
            "description": d.descriptions,
            "features": list(d.features),
            "chain": d.chain,
            "fields": dict(d.fields),
            # Where it comes from: an app bundle (read-only, but may be
            # overlaid) or the instance's local bundle (editable).
            "source": runtime.doctype_source.get(d.id, "core"),
            "overrides": runtime.doctype_overridden.get(d.id),
        }

    # -- records (ADR-0026) ---------------------------------------------

    @router.get("/records")
    def records() -> list[dict]:
        return runtime.record_overview()

    @router.get("/records/{schema_id}")
    def record(schema_id: str) -> dict:
        try:
            return runtime.record(schema_id)
        except RecordError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.put("/records/{schema_id}")
    def record_save(schema_id: str, body: RecordBody, request: Request) -> dict:
        """Store a new version of the record's values. Structural validation
        (required, type, choices, pattern) happens here; the answer is the
        record as it now stands, resolved for its variant."""
        if schema_id not in runtime.records:
            raise HTTPException(status_code=404, detail=f"unknown record {schema_id!r}")
        try:
            runtime.save_record(schema_id, body.values, actor_of(request, body.actor))
        except RecordError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return runtime.record(schema_id)

    # Collections (ADR-0026 extension): rows of contacts, accounts, ...

    @router.get("/records/{schema_id}/rows")
    def record_rows(schema_id: str, q: str = "", limit: int = 500) -> list[dict]:
        try:
            schema = runtime.collection_schema(schema_id)
        except RecordError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return record_store.list_rows(runtime.conn, schema, query=q, limit=max(1, min(limit, 2000)))

    @router.post("/records/{schema_id}/rows")
    def record_row_create(schema_id: str, body: RecordBody, request: Request) -> dict:
        try:
            schema = runtime.collection_schema(schema_id)
            with runtime.lock:
                row = record_store.save_row(
                    runtime.conn, schema, body.values, actor_of(request, body.actor)
                )
        except RecordError as exc:
            status = 404 if "unknown collection" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        runtime.diag("info", "records", "row_saved", schema_id, actor=actor_of(request, body.actor))
        return row

    @router.get("/records/{schema_id}/rows/{row_id}")
    def record_row(schema_id: str, row_id: str) -> dict:
        try:
            schema = runtime.collection_schema(schema_id)
        except RecordError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        row = record_store.get_row(runtime.conn, schema, row_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown row")
        return {**row, "history": record_store.row_history(runtime.conn, row_id)}

    @router.put("/records/{schema_id}/rows/{row_id}")
    def record_row_save(schema_id: str, row_id: str, body: RecordBody, request: Request) -> dict:
        try:
            schema = runtime.collection_schema(schema_id)
        except RecordError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        current = record_store.get_row(runtime.conn, schema, row_id)
        if current is None or current["deleted"]:
            raise HTTPException(status_code=404, detail="unknown row")
        try:
            with runtime.lock:
                row = record_store.save_row(
                    runtime.conn, schema, body.values, actor_of(request, body.actor), row_id
                )
        except RecordError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.diag("info", "records", "row_saved", schema_id, actor=actor_of(request, body.actor))
        return row

    @router.delete("/records/{schema_id}/rows/{row_id}")
    def record_row_delete(schema_id: str, row_id: str, request: Request) -> dict:
        try:
            schema = runtime.collection_schema(schema_id)
        except RecordError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        with runtime.lock:
            removed = record_store.delete_row(
                runtime.conn, schema, row_id, actor_of(request, "ui")
            )
        if not removed:
            raise HTTPException(status_code=404, detail="unknown row")
        runtime.diag("info", "records", "row_removed", schema_id, actor=actor_of(request, "ui"))
        return {"ok": True}

    @router.get("/records/{schema_id}/history")
    def record_versions(schema_id: str, limit: int = 20) -> list[dict]:
        if schema_id not in runtime.records:
            raise HTTPException(status_code=404, detail=f"unknown record {schema_id!r}")
        return record_history(runtime.conn, schema_id, limit)

    @router.get("/doctypes")
    def doctypes() -> list[dict]:
        return [_doctype_view(d) for d in runtime.doctypes.values()]

    @router.put("/doctypes/{doctype_id}")
    def doctype_save(doctype_id: str, body: DoctypeBody, request: Request) -> dict:
        """Create or overlay a document type in the instance's local bundle
        (Stammdaten-Pflege). Validation happens on reload; a rejected
        declaration is removed again so the running set never degrades."""
        titles = {k: v.strip() for k, v in body.title.items() if k in LANGUAGES and v.strip()}
        if "de" not in titles:
            raise HTTPException(status_code=422, detail="a German title (de) is required")
        descriptions = {
            k: v.strip() for k, v in body.description.items() if k in LANGUAGES and v.strip()
        }
        if body.chain not in runtime.engine.definitions:
            raise HTTPException(status_code=422, detail=f"unknown chain {body.chain!r}")
        if _is_intake(runtime.engine.definitions[body.chain]):
            raise HTTPException(
                status_code=422, detail=f"{body.chain!r} is an intake template, not a chain"
            )
        doctype = Doctype(
            id=doctype_id,
            title=titles["de"],
            chain=body.chain,
            description=descriptions.get("de", ""),
            features=tuple(f.strip() for f in body.features if f.strip()),
            fields={k.strip(): v.strip() for k, v in body.fields.items() if k.strip()},
            titles=titles,
            descriptions=descriptions or {"de": ""},
        )
        try:
            doctype.validate()
        except DoctypeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        directory = settings.doctypes_dir
        path = directory / f"{doctype.id}.toml"
        previous = path.read_text(encoding="utf-8") if path.is_file() else None
        save_doctype(directory, doctype)
        try:
            runtime.reload_templates()
        except (DoctypeError, TemplateError, AppError) as exc:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(previous, encoding="utf-8")
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.diag(
            "info",
            "doctypes",
            "saved",
            doctype.id,
            actor=actor_of(request, "ui"),
        )
        return _doctype_view(runtime.doctypes[doctype.id])

    @router.delete("/doctypes/{doctype_id}")
    def doctype_delete(doctype_id: str, request: Request) -> dict:
        """Remove the local declaration: a household-made type disappears, an
        overlay falls back to the bundle's version. Bundle types themselves
        are never deleted here - switch the app off instead."""
        path = settings.doctypes_dir / f"{doctype_id}.toml"
        if runtime.doctype_source.get(doctype_id) != "local" or not path.is_file():
            raise HTTPException(status_code=404, detail="no local declaration for this type")
        previous = path.read_text(encoding="utf-8")
        path.unlink()
        try:
            runtime.reload_templates()
        except (DoctypeError, TemplateError, AppError) as exc:
            path.write_text(previous, encoding="utf-8")
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        runtime.diag("info", "doctypes", "removed", doctype_id, actor=actor_of(request, "ui"))
        return {"ok": True, "fallback": doctype_id in runtime.doctypes}

    @router.get("/templates/{template_id}")
    def template(template_id: str) -> dict:
        """One template as a graph for the workshop (Designer, Stufe 1):
        steps as nodes, outcomes as edges, ``process:`` targets as edges to
        the next template, ``dispatch`` fanned out to every document type's
        chain. Rendered from the declaration - nothing here is editable."""
        definition = runtime.engine.definitions.get(template_id)
        if definition is None:
            raise HTTPException(status_code=404, detail="unknown template")
        steps = []
        for step in definition.steps.values():
            edges = []
            for outcome, target in step.next.items():
                if target is None or target == "end":
                    edges.append({"outcome": outcome, "kind": "end", "target": None})
                elif str(target).startswith("process:"):
                    edges.append(
                        {"outcome": outcome, "kind": "process", "target": str(target)[8:]}
                    )
                else:
                    edges.append({"outcome": outcome, "kind": "step", "target": target})
            config = step.config
            summary = {
                k: config[k]
                for k in (
                    "source",
                    "classify",
                    "assign",
                    "action",
                    "exec",
                    "mandate",
                    "categorization",
                    "addressing",
                    "deadline_seconds",
                    "seconds",
                    "task",
                    "read_document",
                    "stub_outcome",
                )
                if k in config
            }
            if step.kind.value == "human_gate":
                summary["approving_outcomes"] = sorted(step.approving_outcomes())
            if config.get("action") == "dispatch":
                summary["routes"] = {
                    d.id: d.chain for d in sorted(runtime.doctypes.values(), key=lambda d: d.id)
                }
            steps.append(
                {"id": step.id, "kind": step.kind.value, "config": summary, "next": edges}
            )
        titles = definition.meta.get("title")
        return {
            "id": definition.id,
            "version": definition.version,
            "title": _titles(titles, definition.id),
            "description": _titles(definition.meta.get("description"), ""),
            "app": runtime.template_app.get(definition.id),
            "intake": _is_intake(definition),
            "start": definition.start_step().id,
            "starts": [s.id for s in definition.start_steps()],
            "policies": {k: dict(v) for k, v in definition.policies.items()},
            "parameters": dict(definition.parameters),
            "steps": steps,
        }

    # -- workshop: solutions explorer, elements, proving run (ADR-0035) ---

    def _core_solution():
        """The core's mechanics templates (intake, mailroom) as a read-only
        solution, so the workshop shows every process the instance runs -
        including the ones no solution owns. Files live in templates_dir;
        nothing here is ever written through the API."""
        from .apps import App, Manifest

        core = {
            tid: definition
            for tid, definition in runtime.engine.definitions.items()
            if tid not in runtime.template_app
        }
        manifest = Manifest(
            format=2,
            id=CORE_SOLUTION,
            version=__version__,
            name={"de": "Kern", "fr": "Noyau", "it": "Nucleo", "en": "Core"},
            description={
                "de": "Mechanik-Vorlagen des Kerns (Eingang, Vermittler) - nur lesen.",
                "fr": "Modèles mécaniques du noyau (réception, intermédiaire) - lecture seule.",
                "it": "Modelli meccanici del nucleo (ingresso, intermediario) - sola lettura.",
                "en": "The core's mechanics templates (intake, mailroom) - read-only.",
            },
        )
        return App(path=runtime.settings.templates_dir, manifest=manifest, templates=core)

    def _solution(app_id: str):
        if app_id == CORE_SOLUTION:
            return _core_solution()
        app = runtime.apps.get(app_id)
        if app is None:
            raise HTTPException(status_code=404, detail="unknown or disabled solution")
        return app

    def _element_path(app, kind: str, element_id: str) -> Path:
        """Where an element lives inside the solution (root or package).

        Built from the validated owner map, never by searching the tree:
        a hidden directory or a stray copy cannot shadow the loaded file,
        and symlinks or paths leaving the solution are refused."""
        if not re.match(r"^[a-z0-9][a-z0-9_-]*$", element_id):
            raise HTTPException(status_code=422, detail="invalid element id")
        if app.id == CORE_SOLUTION:
            # Core templates sit flat in templates_dir (ADR-0034), no type folders.
            if kind != "process" or element_id not in app.templates:
                raise HTTPException(status_code=404, detail=f"no {kind} {element_id!r} in core")
            for ext in (".bpmn", ".toml"):
                target = app.path / f"{element_id}{ext}"
                if target.is_file():
                    return target
            raise HTTPException(status_code=404, detail=f"no file for core template {element_id!r}")
        if kind == "skill":
            where = _owners(app, "skills").get(element_id)
            rel = f"skills/{element_id}/skill.toml"
        elif kind == "configuration":
            where = "" if element_id in app.configurations else None
            rel = f"configurations/{element_id}.toml"
        elif kind == "test":
            where, rel = None, ""
            for attr, folder in (("templates", "processes"), ("briefings", "ai"),
                                 ("decisions", "decisions")):
                owner = _owners(app, attr).get(element_id)
                if owner is not None:
                    where, rel = owner, f"{folder}/{element_id}.test.toml"
                    break
        else:
            spec = {
                "process": ("templates", "processes", ".bpmn"),
                "decision": ("decisions", "decisions", ".dmn"),
                "ai": ("briefings", "ai", ".aimn.toml"),
                "tool": ("tools", "tools", ".toml"),
            }.get(kind)
            if spec is None:
                raise HTTPException(status_code=422, detail="unknown element kind")
            attr, folder, ext = spec
            where = _owners(app, attr).get(element_id)
            rel = f"{folder}/{element_id}{ext}"
        if where is None:
            raise HTTPException(status_code=404, detail=f"no {kind} {element_id!r} in solution")
        target = app.path / where / rel
        if kind == "test" and not target.exists():
            raise HTTPException(status_code=404, detail=f"no sidecar for {element_id!r}")
        if target.is_symlink() or any(
            parent.is_symlink() for parent in target.parents
            if parent.is_relative_to(app.path) and parent != app.path
        ):
            raise HTTPException(status_code=422, detail="symlinks are not allowed in a solution")
        try:
            inside = target.resolve().is_relative_to(app.path.resolve())
        except OSError:
            inside = False
        if not inside:
            raise HTTPException(status_code=422, detail="element path leaves the solution")
        return target

    def _solution_view(app) -> dict:
        bindings = runtime.model_role_bindings().get(app.id, {})
        root = {
            "where": "",
            "processes": sorted(
                pid for pid, owner in _owners(app, "templates").items() if owner == ""
            ),
            "decisions": sorted(d for d, o in _owners(app, "decisions").items() if o == ""),
            "ai": sorted(b for b, o in _owners(app, "briefings").items() if o == ""),
            "skills": sorted(s for s, o in _owners(app, "skills").items() if o == ""),
            "tools": sorted(t for t, o in _owners(app, "tools").items() if o == ""),
            "types": sorted(list(app.types) + list(app.records)),
            "knowledge": sorted(app.knowledge),
            "doctypes": sorted(app.doctypes),
            "samples": _samples(app),
            "packages": app.packages,
        }
        return {
            "id": app.id,
            "version": app.version,
            "format": app.format,
            "name": _titles(app.manifest.name, app.id),
            "description": _titles(app.manifest.description, ""),
            "path": _display_path(app.path),
            "writable": _writable(app),
            "configurations": sorted(set(app.configurations) | {"standard", "test", "shadow"}),
            "active_configuration": runtime.configuration.name,
            "model_roles": {
                name: {
                    "max_data_class": role.max_data_class,
                    "vision": role.vision,
                    "structured_output": role.structured_output,
                    **bindings.get(name, {}),
                }
                for name, role in app.manifest.model_roles.items()
            },
            "dependencies": dict(app.manifest.dependencies),
            "sidecars": len(app.sidecars),
            "drafts": len(drafts.list(app.id)),
            "trust": (
                {
                    "level": runtime.app_trust[app.id].level,
                    "verified": runtime.app_trust[app.id].verified,
                    "reason": runtime.app_trust[app.id].reason,
                    "signed_by": runtime.app_trust[app.id].signed_by,
                }
                if app.id in runtime.app_trust else None
            ),
            "tree": root,
        }

    def _samples(app) -> list[str]:
        """Sample files a sidecar case may be made from (ADR-0035 §6)."""
        folder = app.path / "samples"
        if app.id == CORE_SOLUTION or not folder.is_dir():
            return []
        return sorted(
            f"samples/{p.relative_to(folder).as_posix()}"
            for p in folder.rglob("*")
            if p.is_file() and not p.name.startswith(".") and not p.is_symlink()
        )

    def _owners(app, attr: str) -> dict[str, str]:
        """Element id -> package path ('' at the root), from the package
        summaries - so the explorer shows where an element lives."""
        key = {"templates": "processes", "briefings": "ai", "decisions": "decisions",
               "skills": "skills", "tools": "tools"}[attr]
        owners = dict.fromkeys(getattr(app, attr), "")

        def walk(packages: dict, prefix: str) -> None:
            for name, summary in packages.items():
                where = f"{prefix}packages/{name}/"
                for element_id in summary.get(key, []):
                    owners[element_id] = where
                walk(summary.get("packages", {}), where)

        walk(app.packages, "")
        return owners

    def _display_path(path: Path) -> str:
        """The solution's location as members may see it: relative to the
        apps or data directory, never the server's absolute path."""
        for base in (runtime.settings.apps_dir, runtime.settings.data_dir):
            try:
                return path.resolve().relative_to(base.resolve()).as_posix()
            except (OSError, ValueError):
                continue
        return path.name

    def _writable(app) -> bool:
        """Elements may be saved only into a solution under the apps or
        data directory - never into the core's templates."""
        if app.id == CORE_SOLUTION:
            return False
        path = app.path
        try:
            return path.is_dir() and (path / "solution.toml").is_file() and (
                path.resolve().is_relative_to(runtime.settings.apps_dir.resolve())
                or path.resolve().is_relative_to(runtime.settings.data_dir.resolve())
            )
        except OSError:
            return False

    @router.get("/solutions")
    def solutions() -> list[dict]:
        """Installed solutions with their explorer tree (format 2) - the
        workshop's left pane (ADR-0035 §2, ACTICO's Modeling Explorer)."""
        views = [_solution_view(app) for app in runtime.apps.values() if app.format == 2]
        core = _core_solution()
        if core.templates:
            views.insert(0, _solution_view(core))
        return views

    @router.get("/solutions/{app_id}")
    def solution_detail(app_id: str) -> dict:
        app = _solution(app_id)
        if app.format != 2:
            raise HTTPException(status_code=409, detail="format-1 bundle: migrate it first")
        return _solution_view(app)

    @router.get("/solutions/{app_id}/elements/{kind}/{element_id}")
    def solution_element(app_id: str, kind: str, element_id: str) -> dict:
        """One element: its file text plus a parsed summary the panel
        shows (briefing fields, process steps with prompt hashes, live
        statistics per step from the audit log)."""
        app = _solution(app_id)
        path = _element_path(app, kind, element_id)
        text = path.read_text(encoding="utf-8")
        summary: dict = {}
        if kind == "process":
            from .bpmn import ensure_diagram

            text = ensure_diagram(text)  # hand-written files get a layout for the canvas
            definition = app.templates.get(element_id)
            if definition is not None:
                summary["steps"] = [
                    _step_view(definition, step) for step in definition.steps.values()
                ]
                summary["stats"] = _step_stats(element_id)
                summary["history"] = _step_history(element_id)
                summary["title"] = _titles(definition.meta.get("title"), definition.id)
                summary["contract"] = dict(definition.contract)
                summary["source"] = definition.start_step().config.get("source")
                summary["starts"] = [
                    {"id": s.id, "source": s.config.get("source"),
                     "channel": s.config.get("channel")}
                    for s in definition.start_steps()
                ]
        elif kind in ("ai", "skill"):
            table = app.briefings if kind == "ai" else app.skills
            briefing = table.get(element_id)
            if briefing is not None:
                config = briefing.to_config(app.skills) if kind == "ai" else {}
                summary = {
                    "role": briefing.role,
                    "instructions": briefing.instructions,
                    "limits": list(briefing.limits),
                    "model_role": briefing.model_role,
                    "flexibility": briefing.flexibility,
                    "skills": list(briefing.skills),
                    "tools": list(briefing.tools),
                    "output": briefing.output,
                    "context": briefing.context,
                    "task": config.get("task", ""),
                    "prompt_hash": config.get("prompt_hash", ""),
                }
        elif kind == "decision":
            decision = app.decisions.get(element_id)
            if decision is not None:
                summary = {
                    "hit_policy": decision.hit_policy,
                    "inputs": [
                        {"label": i.label, "expression": i.expression} for i in decision.inputs
                    ],
                    "outputs": [o.name for o in decision.outputs],
                    "rules": len(decision.rules),
                }
        elif kind == "tool":
            tool = app.tools.get(element_id)
            if tool is not None:
                summary = {
                    "name": tool.name,
                    "description": tool.description,
                    "effect": tool.effect,
                    "action": tool.action,
                    "connector": tool.connector,
                    "on_error": tool.on_error,
                    "inputs": ", ".join(tool.inputs) or "-",
                    "outputs": ", ".join(tool.outputs) or "-",
                }
                if tool.http is not None:
                    from .connectors import host_policy

                    refused = host_policy(
                        tool.http.url, allow_private=runtime.settings.tools_allow_private_hosts
                    )
                    summary["http"] = f"{tool.http.method} {tool.http.url}"
                    summary["host_policy"] = refused or "ok"
                    if tool.http.files:
                        summary["files"] = ", ".join(
                            f"{k} <- {v}" for k, v in tool.http.files.items()
                        )
                    summary["secrets"] = ", ".join(sorted({
                        name for value in tool.http.headers.values()
                        for kind, name in _TOOL_PLACEHOLDER.findall(value) if kind == "secret"
                    })) or "-"
                if tool.client is not None:
                    summary["kind"] = tool.client.kind
                    summary["expects"] = tool.client.expects
                    if tool.client.links:
                        summary["links"] = ", ".join(tool.client.links)
        sidecar = None
        if kind in ("process", "ai", "decision"):
            candidate = path.with_name(
                path.name.replace(".aimn.toml", "").replace(".bpmn", "").replace(".dmn", "")
                + ".test.toml"
            )
            if candidate.is_file():
                sidecar = candidate.read_text(encoding="utf-8")
        return {
            "id": element_id,
            "kind": kind,
            "file": path.relative_to(app.path).as_posix(),
            "text": text,
            "summary": summary,
            "sidecar": sidecar,
            "writable": _writable(app),
        }

    def _step_view(definition, step) -> dict:
        """What the property panel shows for a step: the declared facts
        the person decides by (model role and its binding, effect and how
        the active configuration runs the tool, data class, deadline)."""
        config = step.config
        briefing = config.get("briefing") or {}
        view: dict = {
            "id": step.id,
            "kind": step.kind.value,
            "outcomes": list(step.next),
            "next": dict(step.next),
            "prompt_hash": config.get("prompt_hash"),
            "briefing": briefing.get("id"),
            "tool": config.get("tool"),
            "action": config.get("action"),
            "decision": config.get("decision"),
        }
        # Parallel structure (ADR-0037): what a split opens, what a join
        # waits for, how a multi-instance step fans out.
        if step.forks:
            view["forks"] = {outcome: list(heads) for outcome, heads in step.forks.items()}
        if step.is_join:
            try:
                _fork, _outcome, heads = definition.joins()[step.id]
                view["join_expects"] = list(heads)
            except (KeyError, Exception):
                view["join_expects"] = []
        if step.for_each is not None:
            view["for_each"] = dict(step.for_each)
        if step.kind is StepKind.AGENT_STEP:
            view["model_role"] = briefing.get("model_role") or None
            view["flexibility"] = briefing.get("flexibility")
            view["skills"] = list(briefing.get("skills") or [])
            view["tools"] = list(briefing.get("tools") or [])
            view["output"] = dict(config.get("output") or {})
            view["context"] = dict(config.get("context") or {})
            view["mechanics"] = "classify" if config.get("classify") else (
                "assign" if config.get("assign") else None
            )
            try:
                view["data_class"] = runtime._need(definition, config)
            except Exception:  # undeclared: the dominance check reports it at run time
                view["data_class"] = None
        elif step.kind is StepKind.HUMAN_GATE:
            view["approving_outcomes"] = list(config.get("approving_outcomes") or [])
            view["deadline_seconds"] = config.get("deadline_seconds")
            view["categorization"] = bool(config.get("categorization"))
        elif step.kind is StepKind.SYSTEM_ACTION:
            view["effect"] = runtime._effect_of(config)
            view["mandate"] = bool(config.get("mandate"))
            view["process"] = config.get("process")
            setting = runtime._tool_setting(config)
            view["tool_mode"] = "internal" if view["effect"] == "internal" else setting.mode
            if config.get("exec") == "client":
                view["exec"] = "client"
            if config.get("inputs"):
                view["inputs"] = dict(config["inputs"])
            if config.get("collection"):
                view["collection"] = str(config["collection"])
        elif step.kind is StepKind.TIMER:
            view["timer"] = {
                k: v for k, v in config.items() if k in ("seconds", "until", "until_from")
            }
        elif step.kind is StepKind.TRIGGER:
            view["source"] = config.get("source")
        return view

    def _step_history(template_id: str) -> dict[str, dict]:
        """Last execution per step (the panel's "Letzte Ausführung"): run
        count, outcome distribution, last run and its outcome, and the
        last error text if the step failed - from the instance's own
        cases, never from the proving run."""
        rows = runtime.conn.execute(
            "SELECT s.step_id AS step_id, s.outcome AS outcome, s.status AS status,"
            " s.finished_at AS finished_at, s.started_at AS started_at, s.result AS result,"
            " s.case_id AS case_id, s.branch AS branch, s.instance_total AS instance_total"
            " FROM steps s JOIN cases c ON c.id = s.case_id WHERE c.template_id = ?"
            " ORDER BY COALESCE(s.finished_at, s.started_at) DESC LIMIT 2000",
            (template_id,),
        ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            entry = out.setdefault(
                r["step_id"],
                {"runs": 0, "outcomes": {}, "last_at": None, "last_outcome": None,
                 "last_status": None, "last_error": None, "last_case": None, "open": 0,
                 "instances": 0, "branches": 0},
            )
            entry["runs"] += 1
            if r["instance_total"] is not None:
                entry["instances"] += 1
            if r["branch"]:
                entry["branches"] += 1
            if r["outcome"]:
                entry["outcomes"][r["outcome"]] = entry["outcomes"].get(r["outcome"], 0) + 1
            if r["status"] in ("pending", "waiting"):
                entry["open"] += 1
            if entry["last_at"] is None:
                entry["last_at"] = r["finished_at"] or r["started_at"]
                entry["last_outcome"] = r["outcome"]
                entry["last_status"] = r["status"]
                entry["last_case"] = r["case_id"]
                if r["outcome"] == "failed" and r["result"]:
                    try:
                        data = json.loads(r["result"])
                        entry["last_error"] = str(data.get("error") or "")[:300] or None
                    except (ValueError, AttributeError):
                        entry["last_error"] = None
        return out

    def _step_stats(template_id: str) -> dict[str, int]:
        rows = runtime.conn.execute(
            "SELECT s.step_id AS step_id, COUNT(*) AS n FROM steps s"
            " JOIN cases c ON c.id = s.case_id WHERE c.template_id = ?"
            " GROUP BY s.step_id",
            (template_id,),
        ).fetchall()
        return {r["step_id"]: int(r["n"]) for r in rows}

    @router.put("/solutions/{app_id}/elements/ai/{element_id}/fields")
    def solution_briefing_save(
        app_id: str, element_id: str, body: BriefingFields, request: Request
    ) -> dict:
        """Save a briefing from the property panel: the fields become the
        canonical TOML (pass-through keys of the current file are kept),
        then the same validated write as for raw text applies."""
        from .aimn import Briefing, BriefingError, briefing_to_toml

        _require_admin(request)
        app = _solution(app_id)
        current = app.briefings.get(element_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"no briefing {element_id!r} in solution")
        try:
            briefing = Briefing(
                id=element_id,
                role={k: v for k, v in body.role.items() if v.strip()},
                instructions={k: v for k, v in body.instructions.items() if v.strip()},
                limits=tuple({"en": text} for text in body.limits if text.strip()),
                model_role=body.model_role.strip(),
                flexibility=body.flexibility,
                skills=tuple(s for s in body.skills if s.strip()),
                tools=tuple(t for t in body.tools if t.strip()),
                output={k: v for k, v in body.output.items() if k.strip()},
                context=current.context,
                confidence_threshold=body.confidence_threshold,
                extra=current.extra,
            )
            text = briefing_to_toml(briefing)
        except (BriefingError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return solution_element_save(
            app_id, "ai", element_id, ElementBody(text=text, actor=body.actor), request
        )

    @router.put("/solutions/{app_id}/elements/{kind}/{element_id}")
    def solution_element_save(
        app_id: str, kind: str, element_id: str, body: ElementBody, request: Request
    ) -> dict:
        """Write an element back and reload - but only if the whole
        solution still validates. A broken save never reaches the
        runtime: the file is restored and the findings returned."""
        _require_admin(request)
        app = _solution(app_id)
        return _save_element(app, kind, element_id, body.text, actor_of(request, body.actor))

    def _save_element(app, kind: str, element_id: str, text: str, actor: str) -> dict:
        if not _writable(app):
            raise HTTPException(status_code=409, detail="solution directory is not writable")
        path = _element_path(app, kind, element_id)
        previous = path.read_text(encoding="utf-8")
        from .apps import validate_app

        # Validate on a scratch copy first: the live directory is touched
        # only with a text that passed, and a reload or proving run in
        # between never sees the candidate.
        with tempfile.TemporaryDirectory(prefix="aitmation-save-") as scratch:
            copy = Path(scratch) / app.path.name
            shutil.copytree(app.path, copy, ignore=shutil.ignore_patterns(".git", "_to_delete"))
            (copy / path.relative_to(app.path)).write_text(text, encoding="utf-8")
            report = validate_app(
                copy,
                core_definitions=_core_definitions(),
                provided={k: v for k, v in runtime.apps.items() if k != app.id},
            )
        if not report.ok:
            raise HTTPException(
                status_code=422,
                detail={"findings": [str(f) for f in report.errors]},
            )
        path.write_text(text, encoding="utf-8")
        try:
            runtime.reload_templates()
        except Exception as exc:  # whatever the reload tripped over, the old text comes back
            path.write_text(previous, encoding="utf-8")
            try:
                runtime.reload_templates()
            except Exception:  # pragma: no cover - the previous state loaded before
                log.exception("restoring %s after a failed save did not reload", path)
            if isinstance(exc, AppError | TemplateError | DoctypeError):
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        runtime.diag(
            "info", "workshop", "element_saved", f"{kind} {element_id} in {app.id}", actor=actor
        )
        return {"ok": True, "warnings": [str(f) for f in report.warnings]}

    @router.get("/solutions/{app_id}/elements/process/{element_id}/graph")
    def solution_graph(
        app_id: str, element_id: str, where: str = "", days: int = 0, lang: str = ""
    ) -> dict:
        """The card canvas's graph of a process (ADR-0040): nodes with
        kind, label, position and binding; edges with their outcome.
        ``lang`` puts the ``aimn:name`` of that language into ``label`` -
        for read-only views; the editor works on the file's own names."""
        from .bpmn_graph import GraphError, localize, read_graph

        app = _solution(app_id)
        path = _element_path(app, "process", element_id)
        try:
            graph = read_graph(path.read_text(encoding="utf-8"))
        except GraphError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if lang[:2] in ("fr", "it", "en"):
            localize(graph, lang[:2])
        graph["writable"] = _writable(app)
        # Which model serves each AI step (like an agent card): the role its
        # briefing names and the entry the instance binds to it - or why
        # none can serve it.
        bindings = runtime.model_role_bindings().get(app.id, {})
        briefings = getattr(app, "briefings", {}) or {}
        for node in graph["nodes"]:
            briefing = briefings.get(node.get("ref", "")) if node.get("kind") == "ai" else None
            if briefing is None or not briefing.model_role:
                continue
            binding = bindings.get(briefing.model_role, {})
            node["model"] = {
                "role": briefing.model_role,
                "entry": binding.get("model"),
                "model_ref": binding.get("model_ref"),
                "pinned": binding.get("pinned"),
                "missing_secret": binding.get("missing_secret"),
                "max_data_class": binding.get("max_data_class"),
                "reason": binding.get("reason"),
            }
            context = briefing.context or {}
            if context.get("knowledge"):
                extra = briefing.extra
                node["knowledge"] = {
                    "spaces": list(context["knowledge"]),
                    "top_k": extra.get("knowledge_top_k"),
                    "per_source": extra.get("knowledge_per_source"),
                    "query": extra.get("knowledge_query"),
                    "if_nothing": extra.get("ungrounded_outcome"),
                }
        definition = runtime.engine.definitions.get(graph["process"])
        graph["data"] = _case_data(graph, briefings, definition)
        if days:
            # Live figures (ADR-0054): how often each step ran and which way
            # it went; an end counts the flows that reached it.
            counts = runtime.node_counts(graph["process"], _where(where), days)
            kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
            for node in graph["nodes"]:
                if node["id"] in counts:
                    node["count"] = counts[node["id"]]["*"]
            for edge in graph["edges"]:
                n = counts.get(edge["source"], {}).get(edge["outcome"] or "", 0)
                if edge["outcome"] and n:
                    edge["count"] = n
                    if kinds.get(edge["target"]) == "end":
                        target = next(x for x in graph["nodes"] if x["id"] == edge["target"])
                        target["count"] = target.get("count", 0) + n
        return graph

    def _where(text: str) -> dict[str, str]:
        out = {}
        for part in text.split(","):
            key, _, value = part.partition(":")
            if key.strip() and value.strip():
                out[key.strip()] = value.strip()
        return out

    def _case_data(graph: dict, briefings: dict, definition) -> list[dict]:
        """The case's data as the process declares it today (ADR-0055,
        derived stage): the inputs of the contract, then per AI step what
        it reads and what it writes. A typed, declared data model with
        cardinalities is the next stage."""
        out: list[dict] = []
        contract = definition.contract if definition is not None else {}
        defaults = contract.get("defaults") or {}
        for name, text in (contract.get("input") or {}).items():
            out.append({"path": f"trigger.{name}", "written_by": "start", "description": text,
                        "required": name not in defaults})
        for node in graph["nodes"]:
            briefing = briefings.get(node.get("ref", "")) if node.get("kind") == "ai" else None
            if briefing is None:
                continue
            context = briefing.context or {}
            reads = [f"trigger.{k}" for k in ("*",) if context.get("trigger", True)]
            reads += [f"steps.{sid}" for sid in context.get("steps") or []]
            reads += [f"knowledge:{k}" for k in context.get("knowledge") or []]
            for name, text in briefing.output.items():
                out.append({"path": f"steps.{node['id']}.{name}", "written_by": node["id"],
                            "description": text, "reads": reads})
            if context.get("knowledge") and briefing.extra.get("knowledge_cite"):
                out.append({"path": f"steps.{node['id']}.knowledge_sources",
                            "written_by": node["id"],
                            "description": "passages retrieved (n, title, url, text, score)",
                            "max": int(briefing.extra.get("knowledge_top_k") or 0) or None})
        return out

    @router.put("/solutions/{app_id}/model-roles/{role}")
    def pin_model_role(app_id: str, role: str, body: RolePin, request: Request) -> dict:
        """Choose the model for one role of a solution ("Tauschen"); null
        returns the role to the automatic choice. Admin only."""
        _require_admin(request)
        try:
            runtime.pin_model_role(app_id, role, body.model or None)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc).strip("'")) from exc
        binding = runtime.model_role_bindings().get(app_id, {}).get(role, {})
        return {"role": role, **binding}

    @router.get("/solutions/{app_id}/designer/catalog")
    def designer_catalog(app_id: str, lang: str = "de") -> dict:
        """The building blocks the process designer offers for this
        solution, each with the settings its form shows (ADR-0062)."""
        from . import designer

        app = _solution(app_id)
        return designer.catalog(
            app, apps=runtime.apps, records=runtime.records,
            spaces=runtime.knowledge.spaces(), processes=runtime.engine.definitions,
            lang=lang[:2] or "de",
        )

    @router.get("/solutions/{app_id}/elements/process/{element_id}/paths")
    def designer_paths(app_id: str, element_id: str, node: str, lang: str = "de") -> dict:
        """The values a step can bind at its place in the process: start
        form fields, outputs of the steps before it, constants, the case."""
        from . import designer
        from .bpmn_graph import GraphError, read_graph

        app = _solution(app_id)
        path = _element_path(app, "process", element_id)
        try:
            graph = read_graph(path.read_text(encoding="utf-8"))
        except GraphError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if node not in {n["id"] for n in graph["nodes"]}:
            raise HTTPException(status_code=404, detail=f"no step {node!r} in {element_id}")
        return {"node": node, "paths": designer.paths(
            graph, node, app=app, apps=runtime.apps, records=runtime.records,
            processes=runtime.engine.definitions, lang=lang[:2] or "de",
        )}

    @router.put("/solutions/{app_id}/elements/process/{element_id}/graph")
    def solution_graph_save(
        app_id: str, element_id: str, body: GraphBody, request: Request
    ) -> dict:
        """The graph applied onto the file (structure and diagram from the
        graph, everything else kept), then the validated save."""
        from .bpmn_graph import GraphError, apply_graph

        _require_admin(request)
        app = _solution(app_id)
        if not _writable(app):
            raise HTTPException(status_code=409, detail="solution directory is not writable")
        path = _element_path(app, "process", element_id)
        try:
            graph = {"nodes": body.nodes, "edges": body.edges}
            if body.contract is not None:
                graph["contract"] = body.contract
            text = apply_graph(path.read_text(encoding="utf-8"), graph)
        except GraphError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        out = _save_element(app, "process", element_id, text, actor_of(request, body.actor))
        out["text"] = text
        return out

    def _core_definitions() -> dict:
        return {
            tid: d for tid, d in runtime.engine.definitions.items()
            if tid not in runtime.template_app
        }

    def _check_with(app, files: dict[str, str]):
        """Validate a solution as it would be with these files added or
        replaced - on a scratch copy; nothing is written."""
        from .apps import validate_app

        with tempfile.TemporaryDirectory(prefix="aitmation-new-") as scratch:
            copy = Path(scratch) / app.path.name
            shutil.copytree(app.path, copy, ignore=shutil.ignore_patterns(".git", "_to_delete"))
            for rel, text in files.items():
                target = copy / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            return validate_app(
                copy,
                core_definitions=_core_definitions(),
                provided={k: v for k, v in runtime.apps.items() if k != app.id},
            )

    drafts = Drafts(runtime.settings.data_dir / "drafts")

    def _as_draft(app, files: dict[str, str], changed: dict[str, str], *, origin: str, title: str,
                  actor: str, report: dict | None = None) -> dict:
        """ADR-0039 stage 1: an import or a template copy waits as a draft
        - checked, with its findings - until an admin releases it."""
        for rel in files:
            if (app.path / rel).exists():
                raise HTTPException(status_code=409, detail=f"{rel} already exists")
        check = _check_with(app, {**files, **changed})
        base = {rel: (app.path / rel).read_text(encoding="utf-8") for rel in changed}
        meta = drafts.create(
            app.id, origin=origin, title=title, files=files, changed=changed, base=base,
            actor=actor,
            findings=[str(f) for f in check.errors], warnings=[str(f) for f in check.warnings],
            report=report,
        )
        runtime.diag("info", "workshop", "draft_created", f"{meta['id']} {origin}: {title}",
                     actor=actor)
        return meta

    def _write_validated(
        app, files: dict[str, str], actor: str, what: str, changed: dict[str, str] | None = None
    ) -> list[str]:
        """Add files to a solution only if the solution still validates
        with them (checked on a scratch copy first) and the runtime
        reloads; otherwise nothing is written. ``changed`` replaces
        existing files (the manifest gaining a model role). Returns the
        warnings."""
        changed = changed or {}
        for rel in files:
            if (app.path / rel).exists():
                raise HTTPException(status_code=409, detail=f"{rel} already exists")
        previous = {rel: (app.path / rel).read_text(encoding="utf-8") for rel in changed}
        files = {**files, **changed}
        report = _check_with(app, files)
        if not report.ok:
            raise HTTPException(
                status_code=422, detail={"findings": [str(f) for f in report.errors]}
            )
        written: list[Path] = []
        try:
            for rel, text in files.items():
                target = app.path / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                written.append(target)
            runtime.reload_templates()
        except Exception as exc:
            for target in written:
                rel = target.relative_to(app.path).as_posix()
                if rel in previous:
                    target.write_text(previous[rel], encoding="utf-8")
                else:
                    target.unlink(missing_ok=True)
            try:
                runtime.reload_templates()
            except Exception:  # pragma: no cover - the previous state loaded before
                log.exception("restoring %s after a failed create did not reload", app.path)
            if isinstance(exc, AppError | TemplateError | DoctypeError):
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            raise
        runtime.diag("info", "workshop", "element_created", what, actor=actor)
        return [str(f) for f in report.warnings]

    @router.post("/solutions", status_code=201)
    def solution_create(body: NewSolutionBody, request: Request) -> dict:
        """A new, empty solution under apps_dir (format 2) - the workshop's
        "new solution". It validates and loads before the call returns."""
        from . import scaffold
        from .apps import validate_app

        _require_admin(request)
        if body.id == CORE_SOLUTION or body.id in runtime.apps:
            raise HTTPException(status_code=409, detail="a solution with this id exists")
        target = runtime.settings.apps_dir / body.id
        if target.exists():
            raise HTTPException(status_code=409, detail="a directory with this id exists")
        try:
            text = scaffold.solution_toml(body.id, body.name, body.description)
        except scaffold.ScaffoldError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.settings.apps_dir.mkdir(parents=True, exist_ok=True)
        target.mkdir()
        try:
            (target / "solution.toml").write_text(text, encoding="utf-8")
            for folder in ("processes", "ai", "decisions", "samples"):
                (target / folder).mkdir()
                (target / folder / ".gitkeep").write_text("", encoding="utf-8")
            report = validate_app(
                target, core_definitions=_core_definitions(), provided=dict(runtime.apps)
            )
            if not report.ok:
                raise HTTPException(
                    status_code=422, detail={"findings": [str(f) for f in report.errors]}
                )
            runtime.reload_templates()
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            try:
                runtime.reload_templates()
            except Exception:  # pragma: no cover
                log.exception("reload after a failed solution create")
            raise
        runtime.diag(
            "info", "workshop", "solution_created", body.id, actor=actor_of(request, body.actor)
        )
        return _solution_view(_solution(body.id))

    @router.post("/solutions/{app_id}/elements/{kind}", status_code=201)
    def solution_element_create(
        app_id: str, kind: str, body: NewElementBody, request: Request
    ) -> dict:
        """A new process, briefing or decision from a scaffold - complete
        and valid, so the canvas and panel have something to edit."""
        from . import scaffold

        _require_admin(request)
        app = _solution(app_id)
        if not _writable(app):
            raise HTTPException(status_code=409, detail="solution directory is not writable")
        files: dict[str, str] = {}
        try:
            if kind == "process":
                ai_ref = body.ai or body.id
                if ai_ref not in app.briefings and ai_ref not in _provided_briefings(app):
                    files[f"ai/{ai_ref}.aimn.toml"] = scaffold.briefing_toml(
                        ai_ref, body.model_role
                    )
                files[f"processes/{body.id}.bpmn"] = scaffold.process_bpmn(
                    body.id, body.title, ai_ref=ai_ref, source=body.source
                )
            elif kind == "ai":
                files[f"ai/{body.id}.aimn.toml"] = scaffold.briefing_toml(body.id, body.model_role)
            elif kind == "decision":
                title = body.title if isinstance(body.title, str) else body.title.get("de", "")
                files[f"decisions/{body.id}.dmn"] = scaffold.decision_dmn(body.id, title)
            else:
                raise HTTPException(status_code=422, detail="kind must be process, ai or decision")
        except scaffold.ScaffoldError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        table = {"process": app.templates, "ai": app.briefings, "decision": app.decisions}[kind]
        if body.id in table:
            raise HTTPException(status_code=409, detail=f"{kind} {body.id!r} already exists")
        warnings = _write_validated(
            app, files, actor_of(request, body.actor), f"{kind} {body.id} in {app_id}"
        )
        return {
            "ok": True, "id": body.id, "kind": kind, "files": sorted(files), "warnings": warnings
        }

    def _provided_briefings(app) -> set[str]:
        out: set[str] = set()
        for dep in app.manifest.dependencies:
            other = runtime.apps.get(dep)
            if other is not None:
                out |= set(other.briefings)
        return out

    def _rename_process(text: str, new_id: str) -> str:
        """A BPMN file under another process id (the process element and
        its diagram plane); everything else stays as it was."""
        match = re.search(r'<(?:bpmn:)?process\b[^>]*\bid="([^"]+)"', text)
        if match is None:
            raise HTTPException(status_code=422, detail="no BPMN process in the file")
        old = match.group(1)
        text = text[: match.start(1)] + new_id + text[match.end(1):]
        return re.sub(rf'bpmnElement="{re.escape(old)}"', f'bpmnElement="{new_id}"', text)

    @router.post("/solutions/{app_id}/elements/process/import", status_code=201)
    def solution_process_import(app_id: str, body: ProcessImportBody, request: Request) -> dict:
        """A BPMN file from elsewhere - another installation's workshop, a
        modeller - as a process of this solution. It must load under the
        profile and validate with what the solution has; nothing is written
        otherwise (the findings say what is missing)."""
        _require_admin(request)
        app = _solution(app_id)
        if not _writable(app):
            raise HTTPException(status_code=409, detail="solution directory is not writable")
        if "<!DOCTYPE" in body.text or "<!ENTITY" in body.text:
            raise HTTPException(status_code=422, detail="DOCTYPE/ENTITY is not accepted")
        new_id = body.id or Path(body.name).stem
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,59}", new_id):
            raise HTTPException(status_code=422, detail="process id: a-z, 0-9, - and _")
        if new_id in app.templates or new_id in runtime.engine.definitions:
            raise HTTPException(status_code=409, detail=f"process {new_id!r} already exists")
        files = {f"processes/{new_id}.bpmn": _rename_process(body.text, new_id)}
        if body.draft:
            meta = _as_draft(app, files, {}, origin="import", title=f"{new_id} ({body.name})",
                             actor=actor_of(request, body.actor))
            return {"ok": True, "id": new_id, "draft": meta, "files": meta["files"],
                    "findings": meta["findings"], "warnings": meta["warnings"]}
        warnings = _write_validated(app, files, actor_of(request, body.actor),
                                    f"process {new_id} imported into {app_id}")
        return {"ok": True, "id": new_id, "files": sorted(files), "warnings": warnings}

    @router.post("/solutions/{app_id}/import/foreign", status_code=201)
    def solution_import_foreign(app_id: str, body: ForeignImportBody, request: Request) -> dict:
        """A workflow of n8n, Activepieces, Make or Zapier, translated into a process
        with briefings and tools (ADR-0060) - always a draft with a report:
        translated, approximated, placeholder; the secrets to set."""
        _require_admin(request)
        app = _solution(app_id)
        if not _writable(app):
            raise HTTPException(status_code=409, detail="solution directory is not writable")
        try:
            text = body.text
            if body.zip_base64:
                import base64
                import binascii

                try:
                    raw = base64.b64decode(body.zip_base64, validate=True)
                except binascii.Error as exc:
                    raise workflow_import.WorkflowImportError("zip_base64 is not base64") from exc
                text = workflow_import.export_text(raw)
            result = workflow_import.translate(
                text, source=body.source, process_id=body.id, index=body.index
            )
        except workflow_import.WorkflowImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if result.process_id in app.templates or result.process_id in runtime.engine.definitions:
            raise HTTPException(
                status_code=409, detail=f"process {result.process_id!r} already exists"
            )
        changed: dict[str, str] = {}
        missing = [r for r in result.model_roles if r not in app.manifest.model_roles]
        if missing:
            manifest = (app.path / "solution.toml").read_text(encoding="utf-8")
            blocks = [
                f"[model_roles.{r}]\nstructured_output = true\n"
                f'description = "imported from {result.source} - bind a model"'
                for r in missing
            ]
            changed["solution.toml"] = manifest.rstrip() + "\n\n" + "\n\n".join(blocks) + "\n"
        summary = result.summary()
        meta = _as_draft(app, result.files, changed, origin=f"import:{result.source}",
                         title=f"{result.process_id} ({result.title}, {result.source})",
                         actor=actor_of(request, body.actor), report=summary)
        return {"ok": True, "id": result.process_id, "draft": meta, "report": summary,
                "files": [*meta["files"], *meta["changed"]],
                "findings": meta["findings"], "warnings": meta["warnings"]}

    @router.post("/solutions/{app_id}/elements/process/copy", status_code=201)
    def solution_process_copy(app_id: str, body: ProcessCopyBody, request: Request) -> dict:
        """A process of any installed solution as the template for a new
        one in this solution: the BPMN under the new id plus the briefings,
        decisions and tools it names (and the model roles they need) that
        this solution does not have yet. The original stays untouched."""
        _require_admin(request)
        app = _solution(app_id)
        if not _writable(app):
            raise HTTPException(status_code=409, detail="solution directory is not writable")
        source = _solution(body.source_solution)
        if body.source_process not in source.templates:
            raise HTTPException(
                status_code=404, detail=f"no process {body.source_process!r} in {source.id}"
            )
        if body.id in app.templates or body.id in runtime.engine.definitions:
            raise HTTPException(status_code=409, detail=f"process {body.id!r} already exists")
        src_file = next((f for f in source.path.rglob(f"{body.source_process}.bpmn")), None)
        if src_file is None:
            raise HTTPException(status_code=422, detail="only BPMN processes serve as a template")
        text = _rename_process(src_file.read_text(encoding="utf-8"), body.id)
        files = {f"processes/{body.id}.bpmn": text}
        roles: set[str] = set()
        for kind, pattern, folder, suffix in (
            ("ai", r'<aimn:aiTask\b[^>]*\bref="([^"]+)"', "ai", ".aimn.toml"),
            ("decision", r'<aimn:decision\b[^>]*\bref="([^"]+)"', "decisions", ".dmn"),
            ("tool", r'<aimn:toolCall\b[^>]*\baction="([^"]+)"', "tools", ".toml"),
        ):
            for ref in sorted(set(re.findall(pattern, text))):
                have = {"ai": app.briefings, "decision": app.decisions}.get(kind, {})
                if ref in have or (kind == "ai" and ref in _provided_briefings(app)):
                    continue
                found = next((f for f in source.path.rglob(f"{ref}{suffix}")
                              if f.parent.name == folder and not f.name.endswith(".test.toml")),
                             None)
                if found is None:
                    continue  # the validator names what is still missing
                rel = f"{folder}/{ref}{suffix}"
                if (app.path / rel).exists():
                    continue
                files[rel] = found.read_text(encoding="utf-8")
                if kind == "ai":
                    role = re.search(r'^model_role\s*=\s*"([^"]+)"', files[rel], re.M)
                    if role:
                        roles.add(role.group(1))
        changed: dict[str, str] = {}
        missing = sorted(r for r in roles if r not in app.manifest.model_roles)
        if missing:
            src_manifest = (source.path / "solution.toml").read_text(encoding="utf-8")
            blocks = []
            for role in missing:
                block = re.search(
                    rf"^\[model_roles\.{re.escape(role)}\]\n(?:(?!\[).*\n?)*", src_manifest, re.M
                )
                blocks.append(block.group(0).rstrip() if block else
                              f"[model_roles.{role}]\nstructured_output = true")
            manifest = (app.path / "solution.toml").read_text(encoding="utf-8")
            changed["solution.toml"] = manifest.rstrip() + "\n\n" + "\n\n".join(blocks) + "\n"
        if body.draft:
            meta = _as_draft(app, files, changed, origin="template",
                             title=f"{body.id} (from {source.id}/{body.source_process})",
                             actor=actor_of(request, body.actor))
            return {"ok": True, "id": body.id, "draft": meta,
                    "files": [*meta["files"], *meta["changed"]],
                    "findings": meta["findings"], "warnings": meta["warnings"]}
        warnings = _write_validated(
            app, files, actor_of(request, body.actor),
            f"process {body.id} from {source.id}/{body.source_process} in {app_id}", changed,
        )
        return {
            "ok": True, "id": body.id, "files": sorted([*files, *changed]), "warnings": warnings
        }

    def _draft(draft_id: str) -> dict:
        try:
            return drafts.get(draft_id)
        except DraftError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/solutions/{app_id}/drafts")
    def solution_drafts(app_id: str, request: Request) -> list[dict]:
        """Imports and template copies waiting for release."""
        _require_admin(request)
        _solution(app_id)
        return drafts.list(app_id)

    @router.get("/drafts/{draft_id}")
    def draft_view(draft_id: str, request: Request, lang: str = "") -> dict:
        """A draft as the workshop shows it: its files, and each process
        as a graph (read only)."""
        _require_admin(request)
        meta = _draft(draft_id)
        files, changed = drafts.texts(draft_id)
        from .bpmn_graph import localize, read_graph

        graphs = {}
        for rel, text in files.items():
            if rel.endswith(".bpmn"):
                try:
                    graph = read_graph(text)
                    if lang:
                        localize(graph, lang[:2])
                    graphs[Path(rel).stem] = graph
                except Exception as exc:  # a broken file is a finding, not a 500
                    graphs[Path(rel).stem] = {"error": str(exc)}
        return {**meta, "texts": {**files, **changed}, "graphs": graphs}

    @router.post("/drafts/{draft_id}/check")
    def draft_check(draft_id: str, request: Request) -> dict:
        """Validate the draft again against the solution as it is now."""
        _require_admin(request)
        meta = _draft(draft_id)
        app = _solution(meta["solution"])
        files, changed = drafts.texts(draft_id)
        report = _check_with(app, {**files, **changed})
        return drafts.update(draft_id, findings=[str(f) for f in report.errors],
                             warnings=[str(f) for f in report.warnings])

    @router.post("/drafts/{draft_id}/release")
    def draft_release(draft_id: str, request: Request, body: ActorBody | None = None) -> dict:
        """The admin gate of the workshop (ADR-0039): the draft's files go
        into the solution - only if it validates now and nothing it
        replaces changed since the draft was made."""
        _require_admin(request)
        meta = _draft(draft_id)
        app = _solution(meta["solution"])
        files, changed = drafts.texts(draft_id)
        current = {rel: (app.path / rel).read_text(encoding="utf-8") for rel in changed
                   if (app.path / rel).is_file()}
        if drafts_fingerprint(current) != meta.get("base"):
            raise HTTPException(
                status_code=409,
                detail="the solution changed since this draft was made - make it again",
            )
        actor = actor_of(request, (body.actor if body else "ui"))
        what = f"draft {draft_id} released ({meta['title']})"
        warnings = _write_validated(app, files, actor, what, changed)
        drafts.delete(draft_id)
        runtime.diag("info", "workshop", "draft_released", f"{draft_id}: {meta['title']}",
                     actor=actor)
        files_out = sorted([*files, *changed])
        return {"ok": True, "solution": app.id, "files": files_out, "warnings": warnings}

    @router.delete("/drafts/{draft_id}")
    def draft_discard(draft_id: str, request: Request) -> dict:
        _require_admin(request)
        meta = _draft(draft_id)
        drafts.delete(draft_id)
        runtime.diag("info", "workshop", "draft_discarded", f"{draft_id}: {meta['title']}",
                     actor=actor_of(request, "ui"))
        return {"ok": True}

    @router.post("/solutions/{app_id}/elements/process/{element_id}/cases", status_code=201)
    def solution_case_create(
        app_id: str, element_id: str, body: NewCaseBody, request: Request
    ) -> dict:
        """Append a sidecar case for a process from a sample under
        samples/, then (by default) run it - "try this process with that
        example" from the panel."""
        from . import scaffold
        from .apps import validate_app
        from .solution import sample_inside
        from .testing import run_solution_tests

        _require_admin(request)
        app = _solution(app_id)
        if not _writable(app):
            raise HTTPException(status_code=409, detail="solution directory is not writable")
        path = _element_path(app, "process", element_id)
        if not sample_inside(app.path, body.sample) or not (app.path / body.sample).is_file():
            raise HTTPException(status_code=422, detail="the sample must be a file under samples/")
        definition = app.templates[element_id]
        for step_id in list(body.decisions) + list(body.outcomes):
            if step_id not in definition.steps:
                raise HTTPException(status_code=422, detail=f"unknown step {step_id!r}")
        if any(c.id == body.id for c in app.sidecars):
            raise HTTPException(status_code=409, detail=f"case {body.id!r} already exists")
        try:
            block = scaffold.sidecar_case(
                body.id, body.sample, description=body.description, decisions=body.decisions,
                status=body.status, outcomes=body.outcomes,
            )
        except scaffold.ScaffoldError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        sidecar = path.with_name(path.name.replace(".bpmn", "") + ".test.toml")
        previous = sidecar.read_text(encoding="utf-8") if sidecar.is_file() else None
        head = previous if previous is not None else "# Sidecar cases (ADR-0035 §6).\n"
        candidate = head.rstrip("\n") + "\n" + block
        with tempfile.TemporaryDirectory(prefix="aitmation-case-") as scratch:
            copy = Path(scratch) / app.path.name
            shutil.copytree(app.path, copy, ignore=shutil.ignore_patterns(".git", "_to_delete"))
            (copy / sidecar.relative_to(app.path)).write_text(candidate, encoding="utf-8")
            report = validate_app(
                copy, core_definitions=_core_definitions(),
                provided={k: v for k, v in runtime.apps.items() if k != app.id},
            )
        if not report.ok:
            raise HTTPException(
                status_code=422, detail={"findings": [str(f) for f in report.errors]}
            )
        sidecar.write_text(candidate, encoding="utf-8")
        try:
            runtime.reload_templates()
        except Exception as exc:
            if previous is None:
                sidecar.unlink(missing_ok=True)
            else:
                sidecar.write_text(previous, encoding="utf-8")
            runtime.reload_templates()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.diag(
            "info", "workshop", "case_created", f"{body.id} for {element_id} in {app_id}",
            actor=actor_of(request, body.actor),
        )
        result: dict = {"ok": True, "id": body.id, "sidecar": candidate}
        if body.run and _proving_lock.acquire(blocking=False):
            try:
                run = run_solution_tests(
                    app.path,
                    configuration="test",
                    only=body.id,
                    record=body.record,
                    inner_client=runtime.model_client if body.record else None,
                    core_definitions=_core_definitions(),
                    core_templates_dir=runtime.settings.templates_dir,
                    dependencies={aid: a.path for aid, a in runtime.apps.items()},
                )
                result["run"] = run.to_dict()
            finally:
                _proving_lock.release()
        return result

    @router.post("/solutions/{app_id}/test")
    def solution_test(app_id: str, body: ProvingBody, request: Request) -> dict:
        """The proving run (ADR-0035 §8) over the installed solution."""
        from .testing import run_solution_tests

        # Workshop work is admin work: a proving run spins up a runtime per
        # case and may record through the instance's model.
        _require_admin(request)
        app = _solution(app_id)
        if app.id == CORE_SOLUTION:
            raise HTTPException(status_code=409, detail="the core has no proving run")
        if not _proving_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="a proving run is already in progress")
        try:
            core = {
                tid: d for tid, d in runtime.engine.definitions.items()
                if tid not in runtime.template_app
            }
            report = run_solution_tests(
                app.path,
                configuration=body.configuration,
                record=body.record,
                inner_client=runtime.model_client if body.record else None,
                only=body.only,
                repeat=body.repeat,
                core_definitions=core,
                core_templates_dir=runtime.settings.templates_dir,
                dependencies={aid: a.path for aid, a in runtime.apps.items()},
            )
        finally:
            _proving_lock.release()
        return report.to_dict()

    @router.get("/templates")
    def templates() -> list[dict]:
        """Loaded process templates, for chain selection and the workshop."""
        out = []
        for definition in runtime.engine.definitions.values():
            titles = definition.meta.get("title")
            out.append(
                {
                    "id": definition.id,
                    "title": _titles(titles, definition.id),
                    "app": runtime.template_app.get(definition.id),
                    "steps": len(definition.steps),
                    # An intake template classifies and dispatches; it is the
                    # entry, never a chain target (a type routed into it
                    # would loop).
                    "intake": _is_intake(definition),
                }
            )
        return out

    # -- time: schedules, deadlines, follow-ups (Stufe 1.3) ---------------

    @router.get("/schedules")
    def schedules() -> list[dict]:
        return runtime.schedules()

    @router.post("/schedules/{template_id}/run")
    def schedule_run_now(template_id: str, body: RunNowBody, request: Request) -> dict:
        try:
            case_id = runtime.run_now(template_id, actor_of(request, body.actor))
        except EngineError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"case_id": case_id}

    @router.post("/templates/{template_id}/start")
    def template_start(template_id: str, body: StartBody, request: Request) -> dict:
        """A person starts a manual process with its contract inputs - the
        workshop's "run this" and the chat's way in (ADR-0041 example:
        make-post with weekday, slot, topic)."""
        if len(json.dumps(body.payload)) > 20_000:
            raise HTTPException(status_code=413, detail="payload too large")
        # Only contract inputs travel; the keys the runtime reads as file
        # references or provenance are never taken from the body (a member
        # could otherwise point `file` at any path the core can read).
        payload = {
            k: v for k, v in body.payload.items() if k not in _RESERVED_TRIGGER_KEYS
        }
        if body.file is not None:
            try:
                data = base64.b64decode(body.file.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(status_code=422, detail="file is not base64") from exc
            if len(data) > INTAKE_MAX_BYTES:
                raise HTTPException(status_code=413, detail="file too large")
            safe = _SAFE_NAME.sub("-", Path(body.file.name).name).strip("-.") or "dokument"
            folder = settings.files_dir / "manual" / secrets.token_hex(6)
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / safe[:120]
            target.write_bytes(data)
            payload["file"] = str(target)
            payload["original_name"] = safe
        try:
            case_id = runtime.run_now(template_id, actor_of(request, body.actor), payload)
        except EngineError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.tick(wait=False)
        return {"case_id": case_id}

    @router.post("/templates/{template_id}/run")
    def template_run(template_id: str, body: RunBody, request: Request):
        """Start a case and drive it to its end - or to what it waits for -
        within the request (ADR-0054). Same engine, gates and audit as
        any case; a gate no rule covers stops the run and the case waits
        for a person as always. Contract inputs only, like ``start``."""
        if len(json.dumps(body.payload)) > 20_000:
            raise HTTPException(status_code=413, detail="payload too large")
        payload = {k: v for k, v in body.payload.items() if k not in _RESERVED_TRIGGER_KEYS}
        try:
            definition = runtime.engine.definitions.get(template_id)
            unknown = [b for b in body.breakpoints if definition and b not in definition.steps]
            if unknown:
                raise EngineError(f"breakpoints name unknown steps: {', '.join(unknown)}")
            if definition is None:
                raise EngineError(f"unknown template {template_id!r}")
            if body.stream:
                return _run_stream(template_id, actor_of(request, body.actor), payload, body)
            report = runtime.run_through(
                template_id, actor_of(request, body.actor), payload, timeout=body.timeout,
                breakpoints=body.breakpoints or None,
            )
        except EngineError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.tick(wait=False)  # whatever is left (a timer, a called process) goes on
        return report

    def _run_stream(template_id: str, actor: str, payload: dict, body: RunBody):
        """The run in a worker thread; its events and finally its report
        go out line by line as they happen (ADR-0058)."""
        events: queue.Queue = queue.Queue()
        done = object()

        def work() -> None:
            try:
                report = runtime.run_through(
                    template_id, actor, payload, timeout=body.timeout,
                    breakpoints=body.breakpoints or None, on_event=events.put,
                )
                events.put({"event": "report", **report})
                runtime.tick(wait=False)
            except EngineError as exc:
                events.put({"event": "error", "detail": str(exc)})
            except Exception as exc:  # the stream must end with a line, not hang
                log.exception("streamed run failed")
                events.put({"event": "error", "detail": exc.__class__.__name__})
            finally:
                events.put(done)

        threading.Thread(target=work, name=f"run-{template_id}", daemon=True).start()

        def lines():
            while True:
                item = events.get()
                if item is done:
                    return
                yield json.dumps(item, ensure_ascii=False, default=str) + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.post("/cases/{case_id}/resume")
    def case_resume(case_id: str, body: ResumeBody) -> dict:
        """Continue a test run that stopped at a breakpoint (ADR-0054)."""
        try:
            report = runtime.resume_run(case_id, breakpoints=body.breakpoints, timeout=body.timeout)
        except EngineError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime.tick(wait=False)
        return report

    @router.get("/cases/{case_id}/trace")
    def case_trace(case_id: str, request: Request) -> dict:
        """What every step of the case got and gave, model calls included.
        Admin only - inputs may quote documents."""
        _require_admin(request)
        try:
            return runtime.case_trace(case_id)
        except EngineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/templates/{template_id}/runs")
    def template_runs(template_id: str, request: Request, where: str = "", limit: int = 20) -> list:
        """The last cases of a process with the path each took (ADR-0056
        run view); ``where=space:townhall`` filters by trigger. Admin only."""
        _require_admin(request)
        if template_id not in runtime.engine.definitions:
            raise HTTPException(status_code=404, detail="unknown process")
        return runtime.case_runs(template_id, _where(where), max(1, min(limit, 50)))

    @router.get("/solutions/{app_id}/elements/process/{element_id}/nodes/{node_id}")
    def process_node(app_id: str, element_id: str, node_id: str, request: Request,
                     where: str = "", limit: int = 5) -> dict:
        """One step of a process as the engine runs it: what it is, the
        prompt its briefing composes, the output it must deliver, the model
        that serves it - and its last executions with input and output.
        ``where=space:townhall`` limits the runs to cases whose trigger
        matches. Admin only."""
        _require_admin(request)
        _solution(app_id)
        definition = runtime.engine.definitions.get(element_id)
        if definition is None or node_id not in definition.steps:
            raise HTTPException(status_code=404, detail="unknown step")
        step = definition.steps[node_id]
        config = step.config
        briefing = config.get("briefing") if isinstance(config.get("briefing"), dict) else None
        view: dict = {"id": node_id, "kind": step.kind.value, "next": dict(step.next)}
        if "task" in config:
            view["prompt"] = config["task"]
            view["output"] = config.get("output") or {}
            view["context"] = config.get("context") or {}
            for key in ("knowledge_query", "knowledge_top_k", "knowledge_per_source",
                        "data_class", "success_outcome", "ungrounded_outcome"):
                if key in config:
                    view[key] = config[key]
        if config.get("action") == "route":
            view["routes"] = config.get("routes") or []
            view["default"] = config.get("default", "")
        if briefing:
            view["briefing"] = briefing
            role = briefing.get("model_role")
            if role:
                app = runtime.template_app.get(element_id) or app_id
                view["model"] = {"role": role,
                                 **runtime.model_role_bindings().get(app, {}).get(role, {})}
        view["runs"] = runtime.step_runs(element_id, node_id, _where(where), max(1, min(limit, 20)))
        return view

    @router.get("/deadlines")
    def deadlines() -> list[dict]:
        return runtime.deadlines()

    @router.post("/followups")
    def followup_create(body: FollowUpBody, request: Request) -> dict:
        """A hand-set deadline: starts the follow-up template with the
        chosen day in the trigger; the timer waits until then."""
        try:
            due = datetime.fromisoformat(body.due_at.strip())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="due_at must be an ISO date") from exc
        if body.about_case and not runtime.conn.execute(
            "SELECT 1 FROM cases WHERE id = ?", (body.about_case,)
        ).fetchone():
            raise HTTPException(status_code=404, detail="unknown case")
        try:
            case_id = runtime.run_now(
                body.template,
                actor_of(request, body.actor),
                {
                    "title": body.title.strip(),
                    "note": body.note.strip(),
                    "due_at": due.isoformat(),
                    "about_case": body.about_case,
                },
            )
        except EngineError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"case_id": case_id}

    # -- channels (Einstellungen → Kanäle) ---------------------------------

    def _catch_all_targets() -> set[str]:
        return {
            row["target"]
            for row in runtime.engine.active_mandates("forward")
            if not (json.loads(row["condition"]) if row["condition"] else {})
        }

    def _addressee_view(a: Addressee, catch_all: set[str]) -> dict:
        return {
            "id": a.id,
            "title": a.title,
            "channel": a.channel,
            "path": a.path,
            "catch_all": a.id in catch_all,
        }

    def _mail_view() -> dict:
        status = secret_status(settings.data_dir)
        cfg = runtime.mail_config
        return {
            "imap_host": cfg.get("imap_host", ""),
            "imap_user": cfg.get("imap_user", ""),
            "imap_folder": cfg.get("imap_folder", "INBOX"),
            "password": status.get("imap_password", "none"),
            "active": runtime.mail_source is not None,
        }

    def _notify_view() -> dict:
        status = secret_status(settings.data_dir)
        return {
            **runtime.notify_config,
            "password": status.get("smtp_password", "none"),
            "active": runtime.notifier.active,
        }

    @router.get("/channels")
    def channels() -> dict:
        """Every way a document reaches this instance, with its state - and
        the way out: who is told about waiting decisions."""
        status = secret_status(settings.data_dir)
        catch_all = _catch_all_targets()
        return {
            "folder": {"inbox_dir": str(settings.inbox_dir)},
            "mail": _mail_view(),
            "notifications": _notify_view(),
            "web": {
                "token": status.get("intake_token", "none"),
                "endpoint": f"{API_PREFIX}/intake",
            },
            "upload": {"enabled": True},
            "addressees": [_addressee_view(a, catch_all) for a in runtime.addressees.values()],
            "mailroom": settings.instance_type == "mailroom",
        }

    @router.put("/channels/mail")
    def channels_mail_save(body: MailBody, request: Request) -> dict:
        """Mailbox settings: host/user/folder into config.toml, the password
        into the secrets file; the source is rebuilt without a restart."""
        _require_admin(request)
        runtime.mail_config.update(
            {
                "imap_host": body.imap_host.strip(),
                "imap_user": body.imap_user.strip(),
                "imap_folder": body.imap_folder.strip() or "INBOX",
            }
        )
        save_config_values(settings, dict(runtime.mail_config))
        if body.password is not None:
            if secret_status(settings.data_dir).get("imap_password") == "env" and body.password:
                raise HTTPException(
                    status_code=409, detail="the mailbox password comes from the environment"
                )
            if body.password or secret_status(settings.data_dir).get("imap_password") == "file":
                save_secret(settings.data_dir, "imap_password", body.password.strip())
        runtime.diag(
            "info", "channels", "mail_saved", body.imap_host, actor=actor_of(request, "ui")
        )
        runtime.reconfigured()
        return _mail_view()

    @router.post("/channels/mail/test")
    def channels_mail_test(request: Request) -> dict:
        _require_admin(request)
        factory = runtime.mail_factory
        source = factory(settings, runtime.mail_config) if factory else runtime.mail_source
        if source is None:
            return {"ok": False, "reason": "incomplete"}
        check = getattr(source, "check", None)
        if check is None:
            return {"ok": False, "reason": "unsupported"}
        try:
            check()
        except Exception as exc:  # the source raises provider-specific errors
            return {"ok": False, "reason": "failed", "detail": str(exc)}
        return {"ok": True}

    @router.put("/channels/notifications")
    def channels_notify_save(body: NotifyBody, request: Request) -> dict:
        """Recipients, thresholds and the outgoing mail server; the SMTP
        password goes into the secrets file. The notifier is rebuilt."""
        _require_admin(request)
        if body.smtp_security not in ("starttls", "ssl", "none"):
            raise HTTPException(status_code=422, detail="smtp_security: starttls | ssl | none")
        if body.remind_after_hours <= 0 or body.escalate_after_hours <= 0:
            raise HTTPException(status_code=422, detail="thresholds must be positive hours")
        values = body.model_dump(exclude={"password"})
        values["public_url"] = values["public_url"].strip().rstrip("/")
        runtime.notify_config.update(values)
        save_config_values(settings, dict(values))
        if body.password is not None:
            if secret_status(settings.data_dir).get("smtp_password") == "env" and body.password:
                raise HTTPException(
                    status_code=409, detail="the SMTP password comes from the environment"
                )
            if body.password or secret_status(settings.data_dir).get("smtp_password") == "file":
                save_secret(settings.data_dir, "smtp_password", body.password.strip())
        runtime.diag("info", "channels", "notifications_saved", actor=actor_of(request, "ui"))
        runtime.reconfigured()
        return _notify_view()

    @router.post("/channels/notifications/test")
    def channels_notify_test(body: NotifyTestBody, request: Request) -> dict:
        _require_admin(request)
        if not runtime.notifier.mail.configured:
            return {"ok": False, "reason": "incomplete"}
        try:
            runtime.notifier.send_test(body.to.strip())
        except notify.NotifyError as exc:
            return {"ok": False, "reason": "failed", "detail": str(exc)}
        return {"ok": True}

    @router.get("/notifications")
    def notifications(limit: int = 50) -> list[dict]:
        return notify.recent(runtime.conn, limit)

    @router.post("/channels/web/token")
    def channels_web_token(request: Request) -> dict:
        """Issue a fresh token for the web-form channel. Shown exactly once -
        the file keeps it, the API never returns it again."""
        _require_admin(request)
        if secret_status(settings.data_dir).get("intake_token") == "env":
            raise HTTPException(status_code=409, detail="the token comes from the environment")
        token = secrets.token_urlsafe(32)
        save_secret(settings.data_dir, "intake_token", token)
        runtime.diag("info", "channels", "web_token_issued", actor=actor_of(request, "ui"))
        return {"token": token, "endpoint": f"{API_PREFIX}/intake", "header": "X-Intake-Token"}

    @router.delete("/channels/web/token")
    def channels_web_token_revoke(request: Request) -> dict:
        _require_admin(request)
        if secret_status(settings.data_dir).get("intake_token") == "env":
            raise HTTPException(status_code=409, detail="the token comes from the environment")
        save_secret(settings.data_dir, "intake_token", "")
        runtime.diag("info", "channels", "web_token_revoked", actor=actor_of(request, "ui"))
        return {"ok": True}

    @router.get("/addressees")
    def addressees() -> list[dict]:
        catch_all = _catch_all_targets()
        return [_addressee_view(a, catch_all) for a in runtime.addressees.values()]

    @router.put("/addressees/{addressee_id}")
    def addressee_save(addressee_id: str, body: AddresseeBody, request: Request) -> dict:
        """Create or change an addressee of the Vermittler; optionally grant
        the catch-all standing mandate (the single-mandate default,
        docs/VERMITTLER.md) - the first mandate of ADR-0016, granted here by
        the person configuring the system."""
        _require_admin(request)
        addressee = Addressee(
            id=addressee_id, title=body.title.strip(), path=body.path.strip(),
            channel=body.channel,
        )
        try:
            addressee.validate()
            save_addressee(settings.addressees_path, addressee)
            runtime.reload_templates()
        except (AddresseeError, TemplateError, DoctypeError, AppError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        actor = actor_of(request, body.actor)
        if body.catch_all and addressee.id not in _catch_all_targets():
            with runtime.lock:
                runtime.engine.grant_mandate(
                    "forward", addressee.id, {}, actor_id=actor, comment="Catch-all bei Einrichtung"
                )
        runtime.diag("info", "channels", "addressee_saved", addressee.id, actor=actor)
        return _addressee_view(runtime.addressees[addressee.id], _catch_all_targets())

    @router.delete("/addressees/{addressee_id}")
    def addressee_delete(addressee_id: str, request: Request) -> dict:
        """Remove an addressee. Standing mandates that still point at it must
        be revoked first (Regeln) - a rule delivering into nothing would be a
        silent hole."""
        _require_admin(request)
        if addressee_id not in runtime.addressees:
            raise HTTPException(status_code=404, detail=f"unknown addressee {addressee_id!r}")
        still = [
            row["id"] for row in runtime.engine.active_mandates("forward")
            if row["target"] == addressee_id
        ]
        if still:
            raise HTTPException(
                status_code=409,
                detail=f"revoke the mandate(s) {still} targeting this addressee first",
            )
        delete_addressee(settings.addressees_path, addressee_id)
        runtime.reload_templates()
        runtime.diag(
            "info", "channels", "addressee_removed", addressee_id, actor=actor_of(request, "ui")
        )
        return {"ok": True}

    @router.get("/mandates")
    def mandates() -> list[dict]:
        rows = runtime.conn.execute(
            "SELECT id, action, target, condition, comment, granted_by, granted_at,"
            " revoked_by, revoked_at FROM mandates ORDER BY granted_at DESC"
        ).fetchall()
        return [
            {**dict(r), "condition": json.loads(r["condition"]) if r["condition"] else {}}
            for r in rows
        ]

    @router.post("/mandates")
    def grant(body: MandateGrant, request: Request) -> dict:
        actor = actor_of(request, body.actor)
        if body.action.startswith("gate:"):
            # a gate rule must name a real gate, one of its outcomes and
            # exactly the facts the gate declares (ADR-0054 §3)
            _, template_id, step_id = [*body.action.split(":", 2), "", ""][:3]
            definition = runtime.engine.definitions.get(template_id)
            step = definition.steps.get(step_id) if definition else None
            fields = (step.config.get("rule_fields") or []) if step else []
            if step is None or not fields:
                raise HTTPException(status_code=422, detail="this gate declares no rule facts")
            if body.target not in step.next:
                raise HTTPException(
                    status_code=422, detail=f"{body.target!r} is not an outcome of this gate"
                )
            if sorted((body.condition or {}).keys()) != sorted(fields):
                raise HTTPException(
                    status_code=422, detail="the condition must name exactly the gate's rule facts"
                )
        with runtime.lock:
            mandate_id = runtime.engine.grant_mandate(
                body.action, body.target, body.condition, actor_id=actor, comment=body.comment
            )
        runtime.diag("info", "mandates", "granted", body.target, actor=actor)
        runtime.tick(wait=False)  # gates the new rule covers are decided now
        return {"id": mandate_id}

    @router.post("/mandates/{mandate_id}/revoke")
    def revoke(mandate_id: str, request: Request, actor: str = "ui") -> dict:
        with runtime.lock:
            runtime.engine.revoke_mandate(mandate_id, actor_id=actor_of(request, actor))
        return {"ok": True}

    @router.get("/mandates/suggestions")
    def mandate_suggestions() -> list[dict]:
        return runtime.mandate_suggestions() + runtime.gate_rule_suggestions()

    return router


def _is(runtime: Runtime, case_id: str, step_id: str, flag: str) -> bool:
    row = runtime.conn.execute("SELECT template_id FROM cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        return False
    definition = runtime.engine.definitions.get(row["template_id"])
    step = definition.steps.get(step_id) if definition else None
    return bool(step and step.config.get(flag))


def ui_dist_dir(settings) -> Path | None:
    """Where the built React shell lives, if anywhere."""
    if settings.ui_dir is not None and Path(settings.ui_dir).is_dir():
        return Path(settings.ui_dir)
    candidate = Path(__file__).resolve().parents[3] / "ui" / "dist"
    return candidate if candidate.is_dir() else None
