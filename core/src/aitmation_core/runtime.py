"""Runtime: wires settings, database, engine and the built-in executors.

One :meth:`Runtime.tick` advances everything that can advance without a
human: it ingests files from the watched inbox folder (folder trigger),
runs the placeholder agent executor, performs built-in system actions and
fires due timers. The web layer runs ``tick`` in a background task inside
the same process (ADR-0011); tests call it directly - no sleeping, no
threads needed to reach every state.

Executors here are deliberately thin and domain-neutral:

* **Folder trigger**: a new file in the inbox starts a case for every
  template whose trigger declares ``source = "folder"``. The file is moved
  into the case file store first, so a crash between move and case start
  never loses the document.
* **Stub agent step** (placeholder until the LLM runtime of session 5):
  completes a pending AgentStep with ``config.stub_outcome`` (default
  ``"done"``) and marks its result as a stub.
* **Export system action**: writes the case's collected step results as a
  JSON file into the export directory - a generic, domain-free "hand the
  result to the outside world". Reachable only behind a passed HumanGate
  like every SystemAction (ADR-0007).

Access to the engine is serialized with a lock: SQLite is single-writer
and the web layer may call in from worker threads.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from . import apps as app_bundles
from . import (
    data_classes,
    db,
    guardrails,
    legal,
    metrics,
    model_registry,
    notify,
    observability,
    providers,
    schedule,
    trust,
)
from . import mail as outbound
from . import records as record_store
from . import solution as solution_format
from .addressees import Addressee, load_addressees
from .config import Settings
from .definition import INTERNAL_ACTIONS, ProcessDefinition, StepKind
from .doctypes import Doctype, DoctypeError, load_doctypes
from .document import DocumentError, EscalatingOcr, ExtractedText, TesseractOcr, extract_text
from .document import is_media as document_is_media
from .engine import Engine, EngineError, _lookup_path, effective_trigger, origin_trigger
from .i18n import pick
from .knowledge import DEFAULT_TOP_K, FastembedEmbedder, KnowledgeError, KnowledgeStore
from .knowledge_web import (
    Crawler,
    SourceError,
    WebSources,
    content_of,
    file_name_of,
    spec_of,
)
from .language import detect as detect_language
from .mail import MailSource, addressed_to
from .model import FallbackClient, ModelClient, ModelError, ModelResponse, partial_json_field
from .recordings import RECORDED_MODEL_REF, RecordingClient
from .records import RecordError, RecordSchema, load_schemas
from .secrets import is_tool_secret, resolve_secret
from .template import load_templates
from .transcribe import (
    FasterWhisperTranscriber,
    Ffmpeg,
    FrameDescriber,
    HttpTranscriber,
    Transcriber,
    WhisperCppTranscriber,
)

log = logging.getLogger("aitmation.runtime")


PROVENANCE_SUFFIX = ".provenance.json"
# A channel (web form, later app channels) drops the envelope next to the
# document: sender, subject, free text. Merged into the trigger payload.
ENVELOPE_SUFFIX = ".envelope.json"
SIDECAR_SUFFIXES = (PROVENANCE_SUFFIX, ENVELOPE_SUFFIX)
ENVELOPE_KEYS = (
    "source",
    "from",
    "to",
    "subject",
    "text",
    "language",
    "channel",
    "sender_name",
    "original_name",
)
LANGUAGE_SAMPLE_CHARS = 6000
KNOWLEDGE_QUERY_CHARS = 1500  # head of the document text used as retrieval query


def _provenance_name(document: Path) -> str:
    return document.name + PROVENANCE_SUFFIX


def _envelope_name(document: Path) -> str:
    return document.name + ENVELOPE_SUFFIX


def _read_sidecar(path: Path) -> dict | None:
    """Parse and remove a sidecar note; None when absent or unreadable."""
    if not path.is_file():
        return None
    data = None
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(path.read_text(encoding="utf-8"))
    path.unlink(missing_ok=True)
    return data if isinstance(data, dict) else None


def _language_of_file(path: Path, ocr=None) -> str | None:
    """Content language from the first part of the text layer; never fails
    the intake - a scan without OCR simply has no language yet."""
    try:
        text = extract_text(path, ocr=ocr).text
    except DocumentError:
        return None
    return detect_language(text[:LANGUAGE_SAMPLE_CHARS])


def _original_name(stored: str) -> str:
    """Strip the storage prefixes in front of a file name: the file store's
    ``<uuid>_``, the upload channel's ``<8 hex>_`` - possibly stacked when a
    document was forwarded from the mailroom to a mandant."""
    name = Path(stored).name
    while True:
        head, sep, tail = name.partition("_")
        if not (sep and tail and len(head) in (8, 32) and _is_hex(head)):
            return name
        name = tail


def _is_hex(value: str) -> bool:
    return all(c in "0123456789abcdef" for c in value)


def _safe_name(text: str) -> str:
    """One path segment for the filing folder: letters, digits, space,
    ``.-_``, umlauts kept; never ``..``, never a separator; 80 characters."""
    out = re.sub(r"[^\w .\-]+", "-", text.strip(), flags=re.UNICODE)
    out = re.sub(r"[-\s]{2,}", "-", out).strip(" .-")
    return "" if out in ("", ".", "..") else out[:80]


def _origin_files(origin: dict) -> list[str]:
    """Files a trigger payload points at, in order, without duplicates
    (a split mail case lists its attachment both as ``file`` and in
    ``attachments``)."""
    seen: list[str] = []
    for candidate in [origin.get("file"), *(origin.get("attachments") or [])]:
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


def _split_message(payload: dict) -> list[dict]:
    """One case per attachment (Vermittler 9b): a mail with several attachments
    usually carries several documents — an invoice and a delivery note, two
    receipts — and each should be classified, dispatched and archived on its
    own. A message without attachments stays one case (the body is the
    document). Every split case keeps the message envelope so the trail still
    leads back to the mail, plus ``attachment_index``/``attachment_count``."""
    attachments = list(payload.get("attachments") or [])
    if not attachments:
        return [payload]
    cases = []
    for index, stored in enumerate(attachments, start=1):
        cases.append(
            {
                **payload,
                "file": stored,
                "original_name": _original_name(stored),
                "attachments": [stored],
                "attachment_index": index,
                "attachment_count": len(attachments),
                "language": payload.get("language") or _language_of_file(Path(stored)),
            }
        )
    return cases


def _prompt_audit(
    config: dict, model_role: str | None = None, flags: list[str] | None = None
) -> dict | None:
    """The prompt digest of an AI task for the audit entry (ADR-0034 §4):
    which composed prompt produced this step's result, under which model
    role, and which security flags the call raised (ADR-0038). Absent for
    legacy TOML steps without a briefing and without flags."""
    digest = config.get("prompt_hash")
    extra: dict = {}
    if digest:
        extra["prompt_hash"] = digest
    if model_role:
        extra["model_role"] = model_role
    if flags:
        extra["guardrail_flags"] = flags
    return extra or None


_URL = re.compile(r"https?://[^\s<>\"']+")


def _declared_payload(config: dict) -> dict:
    declared = config.get("payload")
    if not isinstance(declared, dict):
        return {}
    return {k: v for k, v in declared.items() if k != "source"}


def _safe_file_name(name: str) -> str:
    base = Path(name).name
    if base in ("", ".", ".."):
        base = "delivery.bin"
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in base)[:120]


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _gate_action(template_id: str, step_id: str) -> str:
    """The mandate action that stands for deciding one gate (ADR-0054 §3)."""
    return f"gate:{template_id}:{step_id}"



_TRIGGER_REF = re.compile(r"\{(trigger(?:\.[A-Za-z0-9_]+)+)\}")
# Search words of a knowledge query: a trigger field or the output of an
# earlier step (``{steps.deep_terms.terms}``).
_QUERY_REF = re.compile(r"\{((?:trigger|steps)(?:\.[A-Za-z0-9_]+)+)\}")
SOURCE_TEXT_LIMIT = 800


def _plain(value: object) -> str:
    """A trigger or step value as search words; a list of words counts,
    tables stay out."""
    if isinstance(value, list):
        return " ".join(_plain(v) for v in value[:20] if not isinstance(v, (list, dict)))
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return str(value)


def _knowledge_sources(knowledge: dict) -> list[dict]:
    """The hits of a knowledge context as a flat, numbered list."""
    out = []
    for space in (knowledge.get("spaces") or {}).values():
        for hit in space.get("hits") or []:
            out.append(
                {
                    "n": hit.get("n"),
                    "title": str(hit.get("source", "")).rsplit(" §", 1)[0],
                    "url": hit.get("url"),
                    "text": str(hit.get("text", ""))[:SOURCE_TEXT_LIMIT],
                    "score": hit.get("score"),
                }
            )
    return sorted(out, key=lambda h: h["n"] or 0)




_SENTENCE_END = re.compile(r"[.!?:;]\s|\n")


class _AnswerStream:
    """One streaming step's live text: reads its field out of the model's
    unfinished JSON, releases it sentence by sentence through the post
    guardrails (links and personal data never reach the page unfiltered)
    and stops for good if a guardrail would block or escalate."""

    def __init__(self, sink: Callable[[dict], None], step_id: str, field: str,
                 post_names: list[str], keep: set[str] | None = None) -> None:
        self.sink, self.step_id, self.field = sink, step_id, field
        self.post_names = post_names
        self.keep = keep
        self.raw: list[str] = []
        self.released = 0  # characters of the field's text already sent
        self.dead = False

    def feed(self, piece: str) -> None:
        if self.dead:
            return
        self.raw.append(piece)
        text = partial_json_field("".join(self.raw), self.field)
        if not text:
            return
        last = None
        for m in _SENTENCE_END.finditer(text, self.released):
            last = m
        if last is None:
            return
        chunk = text[self.released:last.end()]
        self.released = last.end()
        clean = self._clean(chunk)
        if clean is None:
            self.dead = True
            return
        try:
            self.sink({"event": "text", "step": self.step_id, "delta": clean})
        except Exception:  # a listener that went away must not break the call
            self.dead = True

    def _clean(self, chunk: str) -> str | None:
        value: object = {"text": chunk}
        for name in self.post_names:
            result = guardrails.run(name, value, "post", self.keep)
            if result.action in ("block", "escalate"):
                return None
            if result.action == "redact" and result.payload is not None:
                value = result.payload
        return str(value.get("text", "")) if isinstance(value, dict) else chunk

_ROUTE_VALUE_LIMIT = 200


def _shown(value):
    """A value as the run view shows it: short, never a whole document."""
    if isinstance(value, str) and len(value) > _ROUTE_VALUE_LIMIT:
        return value[:_ROUTE_VALUE_LIMIT] + "…"
    if isinstance(value, list | dict):
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= _ROUTE_VALUE_LIMIT else text[:_ROUTE_VALUE_LIMIT] + "…"
    return value


def evaluate_routes(config: dict, context: dict) -> tuple[list[dict], str]:
    """Evaluate a ``route`` config over a decision context: every branch in
    order (the first that holds is taken, the rest are still shown as
    evaluated for the run view), then the default."""
    from . import dmn

    taken = ""
    evaluated: list[dict] = []
    for branch in config.get("routes") or []:
        rows = []
        for cond in branch.get("conditions") or []:
            value = dmn.lookup(context, cond["input"])
            ok = bool(dmn._compile_unary(cond["test"], "route")(value))
            rows.append({"input": cond["input"], "test": cond["test"], "value": _shown(value),
                         "result": ok})
        holds = (any if branch.get("match") == "any" else all)(r["result"] for r in rows)
        evaluated.append({"outcome": branch["outcome"], "conditions": rows,
                          "result": bool(holds), "taken": bool(holds and not taken)})
        if holds and not taken:
            taken = branch["outcome"]
    default = str(config.get("default", ""))
    evaluated.append({"outcome": default, "default": True, "conditions": [],
                      "result": not taken, "taken": not taken})
    return evaluated, taken or default

class Runtime:
    def __init__(
        self,
        settings: Settings,
        definitions: dict[str, ProcessDefinition] | None = None,
        *,
        model_client: ModelClient | None = None,
        mail_source: MailSource | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        # A pinned clock belongs to this instance only (proving runs,
        # ADR-0035 §7); the live instance keeps real time.
        self.clock = clock
        # Tests inject an httpx transport; live instances use the network.
        self._http_transport = None
        settings.ensure_layout()
        self.conn = db.connect(settings.db_path)
        db.migrate(self.conn)
        if definitions is None:
            definitions = (
                load_templates(settings.templates_dir) if settings.templates_dir.is_dir() else {}
            )
        self.template_app: dict[str, str] = {}
        self.app_trust: dict[str, trust.TrustResult] = {}
        # Lock bookkeeping lives before the first diagnostic (the app load
        # may already report a refused trust level).
        # Threads that hold ``self.lock`` as step runners - the scheduler's
        # tick and any synchronous run (ADR-0054) - and may let go of it
        # around a slow call; and those that currently have let go.
        self._runners: set[int] = set()
        self._released: set[int] = set()
        # Synchronous runs own their chain: the tick leaves those cases
        # alone, the run touches nothing else (ADR-0054 §2).
        self._sync_chains: set[str] = set()
        self._paused: dict[str, set[str]] = {}  # test runs stopped at breakpoints
        # Where a run's time goes (ms): retrieval per (case, step), filled
        # while a step runs, read by the run report; bounded by pruning.
        self._timings: dict[tuple[str, str], dict[str, float]] = {}
        # Live text of a run (ADR-0058): chain -> event sink; a step whose
        # briefing names a ``stream`` field sends that field while the
        # model writes it.
        self._streams: dict[str, Callable[[dict], None]] = {}
        self._scope: dict[int, str] = {}
        definitions = self._merge_apps(definitions)
        # Named configuration (ADR-0035 §7): the reserved names work
        # without a file; solutions may refine them under configurations/.
        self.configuration: solution_format.Configuration = self._resolve_configuration()
        self.engine = Engine(self.conn, definitions, clock=self.clock)
        self.doctype_source: dict[str, str] = {}
        self.doctype_overridden: dict[str, str] = {}
        self.doctypes = self._load_doctypes(self.engine.definitions)
        self.record_source: dict[str, str] = {}
        self.records: dict[str, RecordSchema] = self._load_records()
        # Knowledge spaces (ADR-0031): local index, optional local embeddings;
        # bundles seed the spaces they expect.
        self.knowledge = KnowledgeStore(
            self.conn, settings.knowledge_dir, embedder=self._detect_embedder()
        )
        self._seed_knowledge()
        # Web sources (ADR-0045): crawled in a thread of their own, the
        # tick only starts what is due. Tests inject a transport and may
        # run crawls inline (crawl_in_background = False).
        self.knowledge_web = WebSources(self.conn)
        self.knowledge_web.reset_running()
        self.web_transport = None
        self.crawl_in_background = True
        self.crawl_delay = settings.knowledge_crawl_delay_seconds
        self._crawling: set[str] = set()
        self._crawl_guard = threading.Lock()
        self._crawl_threads: list[threading.Thread] = []
        self._closing = False
        self.addressees: dict[str, Addressee] = load_addressees(settings.addressees_path)
        self.registry = self._load_registry()
        self.model_client = model_client
        # Clients built per registry entry for role-bound steps (ADR-0035 §3).
        self._role_clients: dict[str, ModelClient] = {}
        # Guardrail flags per (case, step) from the last model call (ADR-0038).
        self._flags: dict[tuple[str, str], list[str]] = {}
        self.mail_source = mail_source
        # Mutable mail settings (Einstellungen → Kanäle), applied without a
        # restart; the web layer's factory builds the source from them.
        self.mail_config: dict[str, str] = {
            "imap_host": settings.imap_host,
            "imap_user": settings.imap_user,
            "imap_folder": settings.imap_folder or "INBOX",
        }
        self.mail_factory: Callable | None = None
        # Notification settings (Einstellungen → Kanäle → Benachrichtigungen),
        # editable without a restart; the notifier is rebuilt from them.
        self.notify_config: dict = {
            "notify_to": settings.notify_to,
            "escalate_to": settings.escalate_to,
            "remind_after_hours": settings.remind_after_hours,
            "escalate_after_hours": settings.escalate_after_hours,
            "smtp_host": settings.smtp_host,
            "smtp_port": settings.smtp_port,
            "smtp_user": settings.smtp_user,
            "smtp_from": settings.smtp_from,
            "smtp_security": settings.smtp_security,
            "public_url": settings.public_url,
        }
        self.mail_sender: notify.Sender | None = None  # tests inject a fake
        # ADR-0048: the ``mail`` action's delivery; tests inject a fake that
        # receives the built message instead of an SMTP connection.
        self.outbound_mailer: Callable[[EmailMessage], None] | None = None
        self.notifier = self._build_notifier()
        self.lock = threading.Lock()
        self._tick_guard = threading.Lock()  # one tick at a time (see tick)
        # OCR (ADR-0028): Tesseract on this machine if installed; the vision
        # escalation is decided per document from data class and legal space.
        self.ocr_local: TesseractOcr | None = self._detect_tesseract()
        # Transcription (ADR-0030): ffmpeg and a local Whisper if present;
        # tests inject their own through these attributes.
        self.ffmpeg: Ffmpeg | None = self._detect_ffmpeg()
        self.transcriber_local: Transcriber | None = self._detect_transcriber()
        # Model budget (ADR-0029), editable from Verwaltung -> Kennzahlen.
        self.budget_config: dict = {
            "monthly_budget_chf": settings.monthly_budget_chf,
            "budget_stop": settings.budget_stop,
        }
        self._budget_warned: str | None = None  # month key of the last warning
        # Called after the registry or a secret changed (web.create_app
        # registers the client rebuild here; tests may register their own).
        self.reconfigure_hooks: list[Callable[[], None]] = []
        # Content-trail retention (K1). Startup is often enough at KMU
        # cadence; the audit_log is deliberately never touched.
        removed = observability.cleanup(self.conn, settings.log_retention_days)
        removed += notify.cleanup(self.conn, settings.log_retention_days)
        if removed:
            log.info("log retention removed %d expired row(s)", removed)
        # A silent zero-template start is undebuggable for the target
        # audience - say clearly what was loaded and from where.
        if self.engine.definitions:
            log.info(
                "loaded %d template(s): %s",
                len(self.engine.definitions),
                ", ".join(sorted(self.engine.definitions)),
            )
        else:
            log.warning(
                "no process templates loaded (looked in %s) - the inbox and"
                " mailbox will be ignored; put *.toml templates there or set"
                " AITMATION_TEMPLATES_DIR",
                settings.templates_dir,
            )

    def path_allowed(self, path: str | Path) -> bool:
        """Whether a path a person configured may be used: anywhere on an
        installation of one's own, only inside this instance's data
        directory in a shared host (``confine_paths``)."""
        if not self.settings.confine_paths:
            return True
        root = self.settings.data_dir.resolve()
        try:
            target = Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        return target == root or root in target.parents

    def quiescent(self) -> bool:
        """Nothing here needs the clock: no open case, no schedule, no web
        source, no mailbox. A shared host may then put the instance to
        sleep until someone asks for it (host.py)."""
        if self.mail_config.get("imap_host") or any(t.is_alive() for t in self._crawl_threads):
            return False
        with self.lock:
            for query in (
                "SELECT 1 FROM cases WHERE status = 'running' LIMIT 1",
                "SELECT 1 FROM schedules LIMIT 1",
                "SELECT 1 FROM knowledge_sources LIMIT 1",
                "SELECT 1 FROM client_tasks WHERE status IN ('open', 'claimed') LIMIT 1",
            ):
                if self.conn.execute(query).fetchone() is not None:
                    return False
        return True

    def close(self) -> None:
        self._closing = True
        for thread in self._crawl_threads:
            thread.join(timeout=10)
        self.conn.close()

    def _merge_apps(
        self, definitions: dict[str, ProcessDefinition]
    ) -> dict[str, ProcessDefinition]:
        """Load the bundles under apps_dir and add their templates (ADR-0023).

        The core set (intake, mailroom) is loaded first; a bundle may route
        into it but never shadow it - the validator rejects that. Every
        bundle is recorded in ``installed_apps``; a bundle switched off
        there is skipped. Format v1 enables newly seen bundles on first
        sight (documented transition rule until the UI carries the gate).
        """
        apps_dir = self.settings.apps_dir
        loaded = (
            app_bundles.load_apps(apps_dir, core_definitions=definitions)
            if apps_dir.is_dir()
            else {}
        )
        merged = dict(definitions)
        active: dict[str, app_bundles.App] = {}
        template_app: dict[str, str] = {}
        app_trust: dict[str, trust.TrustResult] = {}
        now = _iso(datetime.now(UTC))
        for app in loaded.values():
            # ADR-0043: the platform's signature decides the trust level;
            # a level this instance does not accept is not loaded at all.
            manifest_data = {
                "id": app.id, "version": app.version,
                "trust_level": app.manifest.trust_level,
                "signature": app.manifest.signature or None,
            }
            verdict = trust.verify_solution(app.path, manifest_data, self.settings.trust_keys)
            app_trust[app.id] = verdict
            if app.manifest.trust_level and not verdict.verified:
                self.diag(
                    "warning", "apps", "trust_unverified",
                    f"{app.id} declares {app.manifest.trust_level!r}: {verdict.reason}",
                )
            if verdict.level not in self.settings.trusted_levels:
                self.diag(
                    "error", "apps", "trust_refused",
                    f"{app.id} has trust level {verdict.level!r}, this instance loads"
                    f" {list(self.settings.trusted_levels)} (trusted_levels)",
                )
                log.warning("app %s not loaded: trust level %s refused", app.id, verdict.level)
                continue
            row = self.conn.execute(
                "SELECT enabled, version FROM installed_apps WHERE id = ?", (app.id,)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO installed_apps (id, version, format, enabled, installed_at,"
                    " updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                    (app.id, app.version, app.manifest.format, now, now),
                )
                self.conn.commit()
                enabled = True
            else:
                enabled = bool(row["enabled"])
                if row["version"] != app.version:
                    self.conn.execute(
                        "UPDATE installed_apps SET version = ?, format = ?, updated_at = ?"
                        " WHERE id = ?",
                        (app.version, app.manifest.format, now, app.id),
                    )
                    self.conn.commit()
            if not enabled:
                log.info("app %s %s is disabled - not loaded", app.id, app.version)
                continue
            active[app.id] = app
            for tid, definition in app.templates.items():
                merged[tid] = definition
                template_app[tid] = app.id
        self.apps = active
        self.template_app = template_app
        self.app_trust = app_trust
        # DMN decisions of the active solutions (ADR-0034 §2), by id.
        self.decisions = {
            did: decision for app in active.values() for did, decision in app.decisions.items()
        }
        if active:
            log.info(
                "loaded %d app(s): %s",
                len(active),
                ", ".join(f"{a.id} {a.version}" for a in active.values()),
            )
        return merged

    def set_app_enabled(self, app_id: str, enabled: bool, actor_id: str = "admin-ui") -> None:
        """Switch a bundle on or off and reload. Cases and audit stay."""
        row = self.conn.execute(
            "SELECT id FROM installed_apps WHERE id = ?", (app_id,)
        ).fetchone()
        if row is None:
            raise app_bundles.AppError(f"unknown app {app_id!r}")
        self.conn.execute(
            "UPDATE installed_apps SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _iso(datetime.now(UTC)), app_id),
        )
        self.conn.commit()
        self.diag("info", "apps", "app_enabled" if enabled else "app_disabled", app_id,
                  actor=actor_id)
        self.reload_templates()

    def installed_apps(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, version, format, enabled, installed_at, updated_at FROM installed_apps"
            " ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def save_app_settings(self, app_id: str, values: dict, actor_id: str = "ui") -> None:
        """Store the owner's answers to an app's declared settings (ADR-0023).

        Values are kept per installation; binding them into template
        parameters at case start is the next slice (the wizard writes,
        the engine reads on the next reload).
        """
        self.conn.execute(
            "UPDATE installed_apps SET settings_json = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(values, ensure_ascii=False, sort_keys=True),
                _iso(datetime.now(UTC)),
                app_id,
            ),
        )
        self.conn.commit()
        self.diag("info", "apps", "settings_saved", app_id, actor=actor_id, keys=sorted(values))

    def mandate_suggestions(self, threshold: int = 3) -> list[dict]:
        """Rule proposals from repeated hand decisions (docs/VERMITTLER.md).

        When a human has assigned documents from the same sender to the same
        addressee ``threshold`` times without a mandate covering it, propose
        a standing mandate. The proposal is data for a human to grant - it
        never grants itself (ADR-0016).
        """
        # A completed HumanGate is a human decision by construction (ADR-0007);
        # the chosen addressee sits in the step result, the audit stays content-free.
        rows = self.conn.execute(
            "SELECT case_id, result FROM steps WHERE kind = 'human_gate'"
            " AND status = 'completed' AND outcome = 'assigned'"
        ).fetchall()
        counts: dict[tuple[str, str], int] = {}
        for row in rows:
            details = json.loads(row["result"]) if row["result"] else {}
            addressee = details.get("addressee")
            if not addressee:
                continue
            envelope = self._envelope(row["case_id"])
            sender = envelope.get("from") or envelope.get("sender")
            if not sender:
                continue
            target = str(addressee) if isinstance(addressee, str) else ",".join(addressee)
            key = (str(sender), target)
            counts[key] = counts.get(key, 0) + 1
        out = []
        for (sender, addressee), n in sorted(counts.items(), key=lambda kv: -kv[1]):
            if n < threshold or "," in addressee:
                continue
            if self.engine.find_mandate("forward", {"from": sender}) is not None:
                continue
            out.append(
                {
                    "action": "forward",
                    "target": addressee,
                    "condition": {"from": sender},
                    "count": n,
                    "reason": f"{n}x von Hand zugestellt",
                }
            )
        return out

    def _load_doctypes(self, definitions: dict[str, ProcessDefinition]) -> dict[str, Doctype]:
        """Load document type declarations and cross-check their chains.

        Every declared chain must be a loaded template - a category that
        routes into nothing would swallow documents silently.
        """
        directory = self.settings.doctypes_dir
        local = load_doctypes(directory) if directory.is_dir() else {}
        # Bundles bring their own document types (ADR-0023). The instance's
        # own declarations (its "local bundle", docs/FEATURE-FEEDBACK-2026-09
        # §1) come last and overlay a bundle's type of the same id: the
        # household adapts titles, features or the chain without touching
        # the shipped, read-only bundle. Where each type came from is kept
        # for the UI (source) and for "reset to the bundle's version".
        doctypes: dict[str, Doctype] = {}
        source: dict[str, str] = {}
        overridden: dict[str, str] = {}
        for app in self.apps.values():
            for doctype in app.doctypes.values():
                doctypes[doctype.id] = doctype
                source[doctype.id] = app.id
        for doctype in local.values():
            if doctype.id in source:
                overridden[doctype.id] = source[doctype.id]
                log.info(
                    "document type %r: local declaration overlays app %r",
                    doctype.id,
                    source[doctype.id],
                )
            doctypes[doctype.id] = doctype
            source[doctype.id] = "local"
        self.doctype_source = source
        self.doctype_overridden = overridden
        for doctype in doctypes.values():
            if doctype.chain not in definitions:
                raise DoctypeError(
                    f"document type {doctype.id!r}: chain {doctype.chain!r}"
                    " is not a loaded template"
                )
        has_classifier = any(
            step.config.get("classify")
            for definition in definitions.values()
            for step in definition.steps.values()
        )
        if doctypes:
            log.info(
                "loaded %d document type(s): %s", len(doctypes), ", ".join(sorted(doctypes))
            )
        elif has_classifier:
            log.warning(
                "no document types loaded (looked in %s) - the intake will sort"
                " every document out for manual categorization; put *.toml"
                " declarations there or set AITMATION_DOCTYPES_DIR",
                directory,
            )
        return doctypes

    def _load_records(self) -> dict[str, RecordSchema]:
        """Record schemas (ADR-0026): bundles first, the instance's own
        declarations overlay them - the same rule as for document types."""
        schemas: dict[str, RecordSchema] = {}
        source: dict[str, str] = {}
        for app in self.apps.values():
            for schema in app.records.values():
                schemas[schema.id] = schema
                source[schema.id] = app.id
        directory = self.settings.records_dir
        for schema in load_schemas(directory).values() if directory.is_dir() else ():
            schemas[schema.id] = schema
            source[schema.id] = "local"
        self.record_source = source
        # Only schemas for this kind of instance are offered (company vs. private).
        applicable = {
            sid: schema
            for sid, schema in schemas.items()
            if schema.applies_to(self.settings.instance_type)
        }
        if applicable:
            log.info(
                "loaded %d record schema(s): %s", len(applicable), ", ".join(sorted(applicable))
            )
        return applicable

    # -- records (ADR-0026) ----------------------------------------------

    def record(self, schema_id: str) -> dict:
        """Schema (resolved for the stored variant) plus current values."""
        schema = self.records.get(schema_id)
        if schema is None:
            raise RecordError(f"unknown record {schema_id!r}")
        values = record_store.current_values(self.conn, schema_id)
        return {
            **schema.as_dict(values),
            "source": self.record_source.get(schema_id, "local"),
            "values": values or {},
            "meta": record_store.current_meta(self.conn, schema_id),
            "completeness": record_store.completeness(schema, values),
        }

    def record_overview(self) -> list[dict]:
        out = []
        # The profile first, then the collections - the order a person expects.
        ordered = sorted(self.records.items(), key=lambda kv: (kv[1].kind != "single", kv[0]))
        for sid, schema in ordered:
            if schema.kind == "collection":
                out.append(
                    {
                        "id": sid,
                        "title": schema.title,
                        "description": schema.description,
                        "scope": schema.scope,
                        "kind": "collection",
                        "source": self.record_source.get(sid, "local"),
                        "rows": record_store.row_count(self.conn, sid),
                        "completeness": None,
                        "meta": None,
                    }
                )
                continue
            values = record_store.current_values(self.conn, sid)
            out.append(
                {
                    "id": sid,
                    "title": schema.title,
                    "description": schema.description,
                    "scope": schema.scope,
                    "kind": "single",
                    "source": self.record_source.get(sid, "local"),
                    "completeness": record_store.completeness(schema, values),
                    "meta": record_store.current_meta(self.conn, sid),
                }
            )
        return out

    def collection_schema(self, schema_id: str) -> RecordSchema:
        schema = self.records.get(schema_id)
        if schema is None or schema.kind != "collection":
            raise RecordError(f"unknown collection {schema_id!r}")
        return schema

    def save_record(self, schema_id: str, values: dict, actor_id: str) -> dict:
        schema = self.records.get(schema_id)
        if schema is None:
            raise RecordError(f"unknown record {schema_id!r}")
        with self.lock:
            clean = record_store.save_values(self.conn, schema, values, actor_id)
        self.diag("info", "records", "record_saved", schema_id, actor=actor_id)
        return clean

    def record_values(self, schema_id: str) -> dict:
        return record_store.current_values(self.conn, schema_id) or {}

    def record_context(self, schema_id: str) -> object:
        """What an agent step sees of a record: the values of a single
        record, or the current rows of a collection (capped, marked)."""
        schema = self.records.get(schema_id)
        if schema is None:
            return {}
        if schema.kind != "collection":
            return self.record_values(schema_id)
        rows = record_store.list_rows(self.conn, schema, limit=record_store.ROW_CONTEXT_LIMIT + 1)
        truncated = len(rows) > record_store.ROW_CONTEXT_LIMIT
        return {
            "rows": [r["values"] for r in rows[: record_store.ROW_CONTEXT_LIMIT]],
            "truncated": truncated,
        }

    def country(self) -> str | None:
        """The instance's country as declared in any record with a
        ``country`` field (the company or household profile). Used for the
        legal-space rules of model access (ADR-0027); None until filled."""
        for sid, schema in self.records.items():
            if "country" not in schema.fields:
                continue
            value = self.record_values(sid).get("country")
            if value:
                return str(value)
        return None

    def _detect_tesseract(self) -> TesseractOcr | None:
        engine = TesseractOcr(self.settings.tesseract_path or None, self.settings.ocr_languages)
        if engine.available():
            log.info("ocr: tesseract found (%s, languages %s)", engine.binary, engine.languages)
            return engine
        log.warning(
            "ocr: tesseract not found - scans and photos need a released vision model"
            " or a Tesseract installation (docs/DEPLOYMENT.md)"
        )
        return None

    def _detect_embedder(self) -> FastembedEmbedder | None:
        if self.settings.embeddings == "off":
            return None
        embedder = FastembedEmbedder(
            self.settings.embedding_model,
            cache_dir=str(self.settings.embedding_cache_dir or self.settings.data_dir / "models"),
        )
        if embedder.available():
            log.info("knowledge: local embeddings via %s", embedder.name)
            return embedder
        log.info("knowledge: no embedding model (extra 'knowledge') - lexical search only")
        return None

    def _seed_knowledge(self) -> None:
        for app in self.apps.values():
            for decl in app.knowledge.values():
                if self.knowledge.seed_space(
                    decl.id,
                    title=decl.title,
                    description=decl.description or None,
                    data_class=decl.data_class,
                    source=f"app:{app.id}",
                ):
                    log.info("knowledge: space %s seeded by bundle %s", decl.id, app.id)

    def _knowledge_spaces_of(self, config: dict) -> list[str]:
        """The spaces a step binds by name. A binding chosen by the input
        (``{trigger.<field>}``) is resolved per case in
        :meth:`_input_spaces` and never raises the step's class."""
        context = config.get("context") or {}
        return [str(s) for s in (context.get("knowledge") or []) if not str(s).startswith("{")]

    def _input_spaces(self, config: dict, trigger: dict, data_class: str | None) -> list[str]:
        """Spaces named by the case's input (ADR-0054, ``context.knowledge =
        ["{trigger.space}"]``): one process serves many public knowledge
        spaces - a website assistant per tenant. Fail-closed: the field
        must name a live space, and that space's class may not exceed the
        class the step declares, so the input can choose *which* knowledge
        but never *more sensitive* knowledge."""
        out: list[str] = []
        for ref in (config.get("context") or {}).get("knowledge") or []:
            ref = str(ref)
            if not ref.startswith("{"):
                continue
            match = _TRIGGER_REF.fullmatch(ref)
            if not match:
                raise KnowledgeError(
                    f"knowledge binding {ref!r}: only {{trigger.<field>}} may name a space"
                )
            value = _lookup_path({"trigger": trigger}, match.group(1))
            if not isinstance(value, str) or not value.strip():
                raise KnowledgeError(f"knowledge binding {ref!r}: the input names no space")
            space = self.knowledge.require([value.strip()])[0]
            if data_class is None or data_classes.rank(space.data_class) > data_classes.rank(
                data_class
            ):
                raise KnowledgeError(
                    f"knowledge space {space.id!r} ({space.data_class}) is above the step's"
                    f" declared class {data_class!r} - a space chosen by the input may not"
                    " raise it (fail-closed)"
                )
            out.append(space.id)
        return out

    def _detect_ffmpeg(self) -> Ffmpeg | None:
        tool = Ffmpeg(self.settings.ffmpeg_path)
        if tool.available():
            log.info("media: ffmpeg found (%s)", tool.binary)
            return tool
        log.info("media: ffmpeg not found - video and non-WAV audio cannot be read")
        return None

    def _detect_transcriber(self) -> Transcriber | None:
        """whisper.cpp first (native sidecar), then faster-whisper (extra);
        ``whisper_backend`` pins one or switches transcription off."""
        backend = self.settings.whisper_backend
        if backend == "off":
            return None
        if backend in ("auto", "http") and self.settings.whisper_url:
            remote = HttpTranscriber(
                self.settings.whisper_url, resolve_secret(self.settings.data_dir, "whisper_token"),
                self.settings.whisper_model if self.settings.whisper_model != "small" else None,
            )
            if remote.available():
                log.info("transcription: service at %s", remote.url)
                return remote
            log.warning("transcription: service at %s not reachable", remote.url)
            if backend == "http":
                return None
        if backend in ("auto", "cpp"):
            cpp = WhisperCppTranscriber(
                self.settings.whisper_cpp_path,
                self.settings.whisper_cpp_model,
                ffmpeg=self.ffmpeg,
            )
            if cpp.available():
                log.info("transcription: whisper.cpp (%s)", cpp.model_path)
                return cpp
        if backend in ("auto", "faster"):
            fast = FasterWhisperTranscriber(
                self.settings.whisper_model,
                download_root=str(self.settings.data_dir / "models"),
            )
            if fast.available():
                log.info("transcription: faster-whisper (%s)", fast.model_name)
                return fast
        log.info(
            "transcription: no local Whisper found - recordings need whisper.cpp"
            " or the optional extra 'transcription' (docs/DEPLOYMENT.md)"
        )
        return None

    def frame_describer(self, data_class: str | None) -> FrameDescriber | None:
        """Key frames of a video go to a vision model under exactly the rule
        that applies to scanned pages (ADR-0028): released vision model,
        dominance rule, legal space - and the ``video_frames`` switch."""
        if not self.settings.video_frames:
            return None
        return self.ocr_for(data_class).vision

    def ocr_for(self, data_class: str | None) -> EscalatingOcr:
        """The OCR chain for a document of the given class: local first,
        vision only when the settings allow it and a released vision model
        may see this class in this country (dominance rule + legal space).
        None as class means "no model at all" (previews, language sniffing)."""
        vision = None
        if data_class is not None and self.settings.ocr_vision_escalation:
            picked = providers.vision_client(self.settings)
            if picked is not None:
                entry, client = picked
                allowed = data_classes.clearance_suffices(entry.clearance, data_class) and (
                    legal.violation(self.country(), entry, data_class) is None
                )
                if allowed:
                    vision = self._vision_transcriber(entry, client, data_class)
                else:
                    log.info(
                        "ocr: vision escalation not permitted for class %r (model %s)",
                        data_class,
                        entry.id,
                    )
        return EscalatingOcr(self.ocr_local, vision)

    def _vision_transcriber(self, entry, client, data_class: str):
        def transcribe(image: bytes, media_type: str) -> tuple[str, float | None]:
            started = time.monotonic()
            try:
                with self._unlocked():
                    response = client.transcribe_image(image, media_type)
            except ModelError as exc:
                self._log_llm(
                    self.conn, task="ocr-vision", payload={"bytes": len(image)}, status="error",
                    model_ref=entry.model, data_class=data_class, error=str(exc),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise DocumentError(f"vision OCR failed: {exc}") from exc
            self._log_llm(
                self.conn, task="ocr-vision", payload={"bytes": len(image), "media": media_type},
                status="ok", response={"chars": len(str(response.data.get("text", "")))},
                model_ref=response.model_ref, data_class=data_class,
                tokens_in=response.tokens_in, tokens_out=response.tokens_out,
                cost_chf=entry.cost_chf(response.tokens_in, response.tokens_out),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return str(response.data.get("text", "")), response.confidence

        return transcribe

    def _load_registry(self) -> dict[str, model_registry.ModelEntry]:
        path = self.settings.models_path
        if path.is_file():
            registry = model_registry.load_registry(path)
        else:
            registry = model_registry.default_registry(
                self.settings.model_cheap, self.settings.model_strong
            )
        try:
            from_env = model_registry.env_entry(dict(os.environ))
        except model_registry.RegistryError as exc:
            log.error("LLM_* environment ignored: %s", exc)
            from_env = None
        if from_env is not None:
            registry[from_env.id] = from_env  # the environment wins, as for secrets
        return registry

    def diag(self, level: str, source: str, event: str, detail: str = "", **context) -> None:
        """Structured diagnostic event, visible in the admin UI (K1)."""
        with self._with_lock():
            observability.log_diagnostic(self.conn, level, source, event, detail, **context)

    # -- reconfiguration (setup/admin UI) ----------------------------------

    def reload_templates(self) -> dict[str, ProcessDefinition]:
        """Re-scan templates and document types and swap both sets.

        Validation happens on load; a broken template or declaration
        raises and the old sets stay active - reconfiguration never
        leaves the runtime worse than before.
        """
        definitions = (
            load_templates(self.settings.templates_dir)
            if self.settings.templates_dir.is_dir()
            else {}
        )
        previous_apps, previous_map = self.apps, self.template_app
        try:
            definitions = self._merge_apps(definitions)
            engine = Engine(self.conn, definitions, clock=self.clock)
            doctypes = self._load_doctypes(definitions)
            records = self._load_records()
        except Exception:
            self.apps, self.template_app = previous_apps, previous_map
            raise
        self._seed_knowledge()
        addressees = load_addressees(self.settings.addressees_path)
        registry = self._load_registry()
        with self.lock:
            self.engine = engine
            self.doctypes = doctypes
            self.records = records
            self.addressees = addressees
            self.registry = registry
            self.configuration = self._resolve_configuration()
        log.info("templates reloaded: %s", ", ".join(sorted(definitions)) or "(none)")
        return definitions

    def reconfigured(self) -> None:
        """Registry, secrets or channel settings changed: reload and rebuild
        the clients and the notifier."""
        self.reload_templates()
        with self.lock:
            self._role_clients = {}
            self.notifier = self._build_notifier()
        for hook in list(self.reconfigure_hooks):
            hook()

    def _build_notifier(self) -> notify.Notifier:
        cfg = self.notify_config
        base_url = cfg.get("public_url") or f"http://127.0.0.1:{self.settings.port}"
        return notify.Notifier(
            self.conn,
            instance_title=self.settings.instance_title,
            language=self.settings.language,
            base_url=base_url,
            recipients=str(cfg.get("notify_to", "")),
            deputies=str(cfg.get("escalate_to", "")),
            thresholds=notify.Thresholds(
                float(cfg.get("remind_after_hours", 24.0)),
                float(cfg.get("escalate_after_hours", 72.0)),
            ),
            mail=notify.MailSettings(
                host=str(cfg.get("smtp_host", "")),
                port=int(cfg.get("smtp_port", 587) or 587),
                user=str(cfg.get("smtp_user", "")),
                sender=str(cfg.get("smtp_from", "")),
                security=str(cfg.get("smtp_security", "starttls") or "starttls"),
            ),
            password=resolve_secret(self.settings.data_dir, "smtp_password"),
            sender=self.mail_sender
            if self.mail_sender is not None
            else (self._log_sender if self.configuration.notifications == "log" else None),
        )

    def _log_sender(self, settings, password, to: str, subject: str, body: str) -> None:
        """Under ``test``/``shadow`` nothing leaves the instance: the mail
        is diagnosed (visible under Verwaltung -> Protokoll) instead of
        sent (ADR-0035 §7)."""
        self.diag(
            "info", "notify", "logged_instead_of_sent", subject,
            to=to, configuration=self.configuration.name,
        )

    # -- named configurations (ADR-0035 §7) --------------------------------

    def _resolve_configuration(self) -> solution_format.Configuration:
        name = self.settings.configuration or "standard"
        base = solution_format.BUILTIN_CONFIGURATIONS.get(name) or solution_format.Configuration(
            name=name
        )
        roles = dict(base.model_roles)
        tools = dict(base.tools)
        default_mode = base.default_tool_mode
        now = base.now
        notifications = base.notifications
        for app in self.apps.values():
            refined = app.configurations.get(name)
            if refined is None:
                continue
            roles.update(refined.model_roles)
            tools.update(refined.tools)
            default_mode = refined.default_tool_mode
            now = refined.now
            notifications = refined.notifications
        if name not in solution_format.BUILTIN_CONFIGURATIONS and not any(
            name in app.configurations for app in self.apps.values()
        ):
            log.warning("configuration %r is declared by no solution - behaves like standard", name)
        return solution_format.Configuration(
            name=name,
            model_roles=roles,
            tools=tools,
            default_tool_mode=default_mode,
            now=now,
            notifications=notifications,
        )

    def _tool_setting(self, config: dict) -> solution_format.ToolSetting:
        """How this step's tool runs under the active configuration: by
        declared tool id first, then by core action, else the default."""
        key = str(config.get("tool") or "")
        if key and key in self.configuration.tools:
            return self.configuration.tools[key]
        return self.configuration.tool(str(config.get("action", "")))

    def _effect_of(self, config: dict) -> str:
        """A step is internal only when its action is; a declared
        ``effect`` can mark an internal action external (stricter), never
        the other way round - a process file cannot talk its way out of
        the stubs of ``test``/``shadow``."""
        action = str(config.get("action", ""))
        if action not in INTERNAL_ACTIONS:
            # ``read`` only ever comes from a declared tool's connector
            # (the binder sets it, ADR-0042); a raw action stays external.
            return "read" if config.get("effect") == "read" and config.get("tool") else "external"
        return "external" if config.get("effect") == "external" else "internal"

    def _run_configured_stub(self, step, definition: ProcessDefinition, config: dict) -> bool:
        """Under ``stub`` the tool answers with the configured result; under
        ``log`` (shadow) the call is recorded as "would have run" and the
        step continues. Internal actions always run - they only touch our
        own data directory. Returns True when the step was handled here."""
        if self._effect_of(config) == "internal":
            return False
        setting = self._tool_setting(config)
        if setting.mode == "live":
            return False
        outcome = setting.outcome if setting.mode == "stub" else "done"
        step_def = definition.steps[step["step_id"]]
        if outcome not in step_def.next:
            # The validator refuses this at load; a stale instance still
            # fails loudly instead of taking some other path.
            self._fail_action(
                step, step_def, f"stub outcome {outcome!r} is not an outcome of this step"
            )
            return True
        result = {
            "configuration": self.configuration.name,
            "tool_mode": setting.mode,
            "action": config.get("action"),
            "tool": config.get("tool"),
        }
        if setting.mode == "stub":
            result.update(setting.result)
        else:
            result["would_have_run"] = True
            # What the call would have carried - so the protocol (and a
            # person reading the case) sees the inputs, secrets excluded.
            if config.get("inputs"):
                try:
                    result["inputs"] = self._resolve_tool_inputs(step["case_id"], config, step)
                except KeyError as exc:
                    result["inputs_error"] = str(exc)
            self.diag(
                "info", "shadow", "external_action_logged",
                f"{config.get('action')} not executed under {self.configuration.name}",
                case_id=step["case_id"], step_id=step["step_id"],
            )
        self.engine.complete_step(
            step["case_id"],
            step["step_id"],
            outcome,
            result=result,
            actor_type="system",
            actor_id=f"{setting.mode}-executor",
            audit_extra={"configuration": self.configuration.name, "tool_mode": setting.mode},
            row_id=step["id"],
        )
        return True

    # -- model roles (ADR-0035 §3) -----------------------------------------

    def model_role_pins(self) -> dict[str, dict[str, str]]:
        """Which registry entry a person chose for a solution's model role
        (``<data>/model_roles.toml``, ``[<solution>] <role> = "<entry>"``).
        Instance configuration, never part of a bundle (ADR-0035: models
        are bound by the instance)."""
        path = self.settings.data_dir / "model_roles.toml"
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {}
        return {
            str(app): {str(r): str(m) for r, m in roles.items() if isinstance(m, str) and m}
            for app, roles in data.items()
            if isinstance(roles, dict)
        }

    def pin_model_role(self, app_id: str, role: str, entry_id: str | None) -> None:
        """Choose (or with None release) the entry serving one role."""
        if app_id not in self.apps or role not in self.apps[app_id].manifest.model_roles:
            raise KeyError(f"{app_id}: unknown model role {role!r}")
        if entry_id is not None and entry_id not in self.registry:
            raise KeyError(f"unknown model {entry_id!r}")
        pins = self.model_role_pins()
        roles = pins.setdefault(app_id, {})
        if entry_id is None:
            roles.pop(role, None)
        else:
            roles[role] = entry_id
        lines = ["# Model per role, chosen in the workshop (instance configuration).", ""]
        for app, chosen in sorted(pins.items()):
            if not chosen:
                continue
            lines.append(f'["{app}"]')
            lines += [f'"{r}" = "{m}"' for r, m in sorted(chosen.items())]
            lines.append("")
        path = self.settings.data_dir / "model_roles.toml"
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(path)
        self.diag("info", "models", "role_pinned", f"{app_id}.{role} -> {entry_id or 'automatic'}")

    def model_role_bindings(self) -> dict[str, dict[str, dict]]:
        """Which registry entry serves each declared model role, per
        solution - or why none can. The solution names requirements, the
        instance binds them; an unbindable role makes the solution not
        runnable, visibly."""
        out: dict[str, dict[str, dict]] = {}
        country = self.country()
        for app in self.apps.values():
            roles = app.manifest.model_roles
            if not roles:
                continue
            bound: dict[str, dict] = {}
            pins = self.model_role_pins().get(app.id, {})
            for role in roles.values():
                need_rank = data_classes.rank(role.max_data_class)
                chosen = None
                reasons: list[str] = []
                fallbacks: list[str] = []
                pinned = pins.get(role.name)
                # local first, then entries that can actually be called
                # (their provider key is there), then by name
                ordered = sorted(
                    self.registry.values(),
                    key=lambda e: (
                        not e.local,
                        providers.missing_secret(e, self.settings) is not None,
                        e.id,
                    ),
                )
                if pinned:
                    # The person chose this entry for the role ("Tauschen"):
                    # it goes first, and if it no longer qualifies the role
                    # is unbound with the reason - never a silent swap.
                    ordered = [e for e in ordered if e.id == pinned] + [
                        e for e in ordered if e.id != pinned
                    ]
                    if pinned not in self.registry:
                        reasons.append(f"{pinned}: chosen for this role but not in the registry")
                for entry in ordered:
                    if entry.status != "released":
                        continue
                    if role.vision and not entry.vision:
                        reasons.append(f"{entry.id}: no vision")
                        continue
                    if data_classes.rank(entry.clearance) < need_rank:
                        reasons.append(f"{entry.id}: clearance {entry.clearance}")
                        continue
                    violation = legal.violation(country, entry, role.max_data_class)
                    if violation:
                        reasons.append(f"{entry.id}: {violation}")
                        continue
                    if chosen is None:
                        if pinned and entry.id != pinned:
                            break  # the chosen entry did not qualify
                        chosen = entry
                    else:
                        # Every further qualifying entry is a stand-in (ADR-0047
                        # §6): same requirements, next in line when the first
                        # one is unreachable or overloaded.
                        fallbacks.append(entry.id)
                bound[role.name] = {
                    "model": chosen.id if chosen else None,
                    "model_ref": chosen.model if chosen else None,
                    "fallbacks": fallbacks,
                    "max_data_class": role.max_data_class,
                    "vision": role.vision,
                    "pinned": pinned or None,
                    # bound, but every call would fail: shown, not hidden
                    "missing_secret": providers.missing_secret(chosen, self.settings)
                    if chosen else None,
                    "reason": None if chosen else "; ".join(reasons) or "no released model",
                }
            out[app.id] = bound
        return out

    def _notify_tick(self) -> int:
        """Queue notifications for waiting gates and send what is pending."""
        gates = []
        for gate in self.open_gates():
            definition = self.engine.definitions.get(gate["template_id"])
            title = pick(definition.meta.get("title", gate["template_id"])) if definition else None
            gates.append({**gate, "title": title})

        def sla_of(template_id: str) -> dict:
            definition = self.engine.definitions.get(template_id)
            return dict(definition.policy("sla")) if definition else {}

        queued = self.notifier.observe(gates, sla_of)
        sent = self.notifier.deliver()
        return queued + sent

    def set_model_client(self, client: ModelClient | None) -> None:
        with self.lock:
            self.model_client = client

    def set_mail_source(self, source: MailSource | None) -> None:
        with self.lock:
            self.mail_source = source

    # -- scheduler ---------------------------------------------------------

    def tick(self, *, wait: bool = True) -> int:
        """Advance everything advanceable once. Returns number of actions
        taken. Ticks never overlap: while one is in flight (and may have
        let go of the state lock for a slow call), a second caller waits
        its turn - or, with ``wait=False`` (an API request that only wants
        to nudge the case along), returns at once and leaves the rest to
        the scheduler's next tick."""
        if not self._tick_guard.acquire(blocking=wait):
            return 0
        try:
            return self._tick_locked()
        finally:
            self._tick_guard.release()

    def _tick_locked(self) -> int:
        with self.lock:
            me = threading.get_ident()
            self._runners.add(me)
            try:
                n = self._ingest_inbox()
                n += self._ingest_mail()
                n += self._run_agents()
                n += self._run_system_actions()
                n += self._apply_gate_rules()
                n += self._fire_due_timers()
                n += self._fire_schedules()
                n += self._start_due_crawls()
                n += self._notify_tick()
            finally:
                self._runners.discard(me)
        return n

    @contextlib.contextmanager
    def _unlocked(self):
        """Let go of the runtime lock around a slow call that touches no
        state of ours - a model, a foreign host, OCR or a transcription -
        so the web layer keeps answering meanwhile. Only the tick thread
        holds the lock across such calls; everywhere else (and when
        nested) this is a no-op. State read before the call may have moved
        on afterwards: every step completion still passes the engine's
        guard, so a stale row fails loudly rather than silently."""
        me = threading.get_ident()
        if me not in self._runners:
            yield
            return
        self._runners.discard(me)
        self._released.add(me)
        self.lock.release()
        try:
            yield
        finally:
            self.lock.acquire()
            self._released.discard(me)
            self._runners.add(me)

    def _log_llm(self, *args, **kwargs) -> None:
        with self._with_lock():
            observability.log_llm_interaction(*args, **kwargs)

    @contextlib.contextmanager
    def _with_lock(self):
        """A short write from inside an unlocked region (a log row from a
        nested model call, a diagnostic): take the lock back for it."""
        me = threading.get_ident()
        if me not in self._released:
            yield
            return
        self.lock.acquire()
        self._released.discard(me)
        try:
            yield
        finally:
            self._released.add(me)
            self.lock.release()

    # -- knowledge: uploads and web sources (ADR-0031, ADR-0045) -------------

    def _extract_detached(self, file_name: str, content: bytes) -> ExtractedText | DocumentError:
        """Read a document's text from a scratch copy - the slow part of
        filing (OCR, transcription, a long PDF), done without the lock."""
        import tempfile

        suffix = Path(file_name).suffix.lower()[:16]
        with tempfile.TemporaryDirectory(prefix="aitm-knowledge-") as scratch:
            path = Path(scratch) / f"document{suffix}"
            path.write_bytes(content)
            try:
                return extract_text(
                    path, ocr=self.ocr_local, transcriber=self.transcriber_local,
                    ffmpeg=self.ffmpeg,
                )
            except DocumentError as exc:
                return exc

    def add_knowledge_document(
        self, space_id: str, file_name: str, content: bytes, *, title: str = "",
        actor: str = "", replace: str | None = None,
    ) -> dict:
        """An upload: read outside the lock, file under it - so a large
        scan does not hold up the scheduler, and the write never runs
        into a crawl's transaction on the shared connection."""
        extracted = self._extract_detached(file_name, content)
        with self.lock:
            return self.knowledge.add_document(
                space_id, file_name, content, title=title, actor=actor, replace=replace,
                extracted=extracted,
            )

    def _start_due_crawls(self) -> int:
        started = 0
        for view in self.knowledge_web.due():
            if self.start_crawl(view["id"], actor="scheduler"):
                started += 1
        return started

    def crawling(self, source_id: str) -> bool:
        with self._crawl_guard:
            return source_id in self._crawling

    def start_crawl(self, source_id: str, actor: str = "scheduler") -> bool:
        """Start one crawl of a web source. The caller holds the runtime
        lock (the tick, or an API request around this call). Returns False
        when this source is already being crawled."""
        with self._crawl_guard:
            if source_id in self._crawling or self._closing:
                return False
            self._crawling.add(source_id)
        self.knowledge_web.mark_running(source_id)
        if not self.crawl_in_background:
            self._crawl(source_id, actor)
            return True
        self._crawl_threads = [t for t in self._crawl_threads if t.is_alive()]
        thread = threading.Thread(
            target=self._crawl, args=(source_id, actor), name=f"crawl-{source_id}", daemon=True
        )
        self._crawl_threads.append(thread)
        thread.start()
        return True

    def _crawl(self, source_id: str, actor: str) -> None:
        """One crawl: walk the site without the lock, then file page by
        page under it (the web layer and the tick interleave)."""
        guard = self.lock if self.crawl_in_background else contextlib.nullcontext()
        allow_private = self.settings.tools_allow_private_hosts
        try:
            with guard:
                view = self.knowledge_web.get(source_id)
                if view is None:
                    return
                known = self.knowledge_web.known(source_id)
            try:
                spec = spec_of(view, allow_private=allow_private)
            except SourceError as exc:
                with guard:
                    self.knowledge_web.finish(source_id, "failed", {}, str(exc))
                return
            crawler = Crawler(
                spec, transport=self.web_transport, allow_private=allow_private,
                delay=self.crawl_delay, should_stop=lambda: self._closing,
            )
            try:
                result = crawler.run()
            except Exception as exc:  # a crawl must never take the runtime down
                log.exception("crawl of %s failed", spec.url)
                with guard:
                    self.knowledge_web.finish(source_id, "failed", {}, f"crawler: {exc}")
                return
            stats = {
                "found": len(result.items), "new": 0, "updated": 0, "unchanged": 0,
                "removed": 0, "unreadable": 0, "skipped": result.skipped,
                "errors": len(result.errors), "requests": result.requests,
                "complete": result.complete,
            }
            seen: set[str] = set()
            for item in result.items:
                if self._closing:
                    break
                seen.add(item.url)
                content = content_of(item)
                digest = hashlib.sha256(content).hexdigest()
                prior = known.get(item.url)
                if prior and not prior["deleted"] and prior["sha256"] == digest:
                    stats["unchanged"] += 1
                    continue
                name = file_name_of(item)
                if item.kind == "html":
                    extracted: ExtractedText | DocumentError = ExtractedText(
                        text=content.decode("utf-8"), page_count=1, method="web:html"
                    )
                else:
                    extracted = self._extract_detached(name, content)
                title = item.title
                if isinstance(extracted, ExtractedText) and item.kind != "html":
                    # A file's own first heading names it better than its file name.
                    first = extracted.text.lstrip().split("\n", 1)[0]
                    if first.startswith("# ") and len(first) <= 160:
                        title = first[2:].strip() or title
                with guard:
                    if self.knowledge_web.get(source_id) is None:
                        return  # deleted meanwhile: file nothing more
                    try:
                        doc = self.knowledge.add_document(
                            view["space"], name, content, title=title,
                            actor=f"web:{source_id}",
                            replace=prior["id"] if prior else None,
                            extracted=extracted, source_id=source_id, url=item.url,
                        )
                    except KnowledgeError as exc:  # space archived meanwhile
                        self.knowledge_web.finish(source_id, "failed", stats, str(exc))
                        return
                stats["updated" if prior and not prior["deleted"] else "new"] += 1
                if doc["status"] != "indexed":
                    stats["unreadable"] += 1
            # Retire what is gone: always the 404/410 answers; documents not
            # seen again only after a complete walk that found something.
            retire = {url for url in result.gone if url in known}
            if result.complete and result.items and not result.start_failed:
                retire |= {url for url in known if url not in seen and url not in result.kept}
            with guard:
                for url in sorted(retire):
                    if known[url]["deleted"]:
                        continue
                    try:
                        self.knowledge.delete_document(view["space"], known[url]["id"])
                        stats["removed"] += 1
                    except KnowledgeError:
                        pass
                if result.start_failed and not result.items:
                    status, error = "failed", f"{spec.url}: {result.start_failed}"
                elif result.errors or not result.complete:
                    status = "partial"
                    error = "; ".join(f"{u or '-'}: {why}" for u, why in result.errors[:5]) or (
                        f"stopped at {spec.max_pages} documents" if not result.complete else None
                    )
                else:
                    status, error = "ok", None
                self.knowledge_web.finish(source_id, status, stats, error)
                self.diag(
                    "info" if status == "ok" else "warning", "knowledge", "source_crawled",
                    f"{view['space']}/{source_id} {status}: {stats['found']} found,"
                    f" {stats['new']} new, {stats['updated']} updated,"
                    f" {stats['removed']} removed",
                    actor=actor,
                )
                # The next question must not pay for the changed index.
                with contextlib.suppress(Exception):
                    self.knowledge.search([view["space"]], "warm up", 1)
        finally:
            with self._crawl_guard:
                self._crawling.discard(source_id)

    def warm_knowledge(self) -> int:
        """Prepare every space's search index and the embedding model once
        after start - so the first visitor does not wait for them."""
        t0 = time.perf_counter()
        with self.lock:
            try:
                count = self.knowledge.warm()
            except Exception as exc:  # warming is a courtesy, never a failure
                log.warning("knowledge warm-up failed: %s", exc)
                return 0
        log.info("knowledge warm: %d space(s) in %.1f s", count, time.perf_counter() - t0)
        return count

    # -- triggers ----------------------------------------------------------

    def _templates_by_source(self, source: str) -> list[ProcessDefinition]:
        return [
            definition
            for definition in self.engine.definitions.values()
            if source in definition.sources()
        ]

    def _folder_templates(self) -> list[ProcessDefinition]:
        return self._templates_by_source("folder")

    def _mail_templates(self) -> list[ProcessDefinition]:
        """Processes a mailbox message starts: trigger source ``email`` (the
        core's name) or ``mailbox`` (the workshop's and scaffold's name)."""
        seen: dict[str, ProcessDefinition] = {}
        for source in ("email", "mailbox"):
            for definition in self._templates_by_source(source):
                seen.setdefault(definition.id, definition)
        return list(seen.values())

    def _ingest_mail(self) -> int:
        """Start cases for new mailbox messages. A process gets one case per
        attachment (the document intake), unless its trigger says
        ``per_attachment = false`` - then the whole message is one case
        (a mail desk reads the mail, not each file). A trigger with ``to``
        only takes mails sent to those addresses."""
        templates = self._mail_templates()
        if self.mail_source is None or not templates:
            return 0
        count = 0
        for message in self.mail_source.fetch_new(self.settings.files_dir):
            envelope = message.as_payload()
            envelope["language"] = detect_language(
                f"{message.subject}\n{message.body_text}"[:LANGUAGE_SAMPLE_CHARS]
            )
            split = _split_message(envelope)
            # A process that names addresses (``to = "belege, rechnungen"``)
            # takes the mails sent there; a mail one of them took no longer
            # reaches the catch-all processes (the mail desk) - the specific
            # address wins, so one mail is not handled twice.
            recipients = list(envelope.get("recipients") or [])
            claimed = [
                d for d in templates
                if any(
                    s.config.get("to") and addressed_to(recipients, str(s.config["to"]))
                    for s in d.start_steps()
                )
            ]
            chosen = claimed or [
                d for d in templates if not any(s.config.get("to") for s in d.start_steps())
            ]
            for definition in chosen:
                whole = any(
                    step.config.get("per_attachment") is False
                    for step in definition.start_steps()
                )
                for payload in [envelope] if whole else split:
                    case_id = self.engine.start_case(
                        definition.id, payload, actor_id="email-trigger"
                    )
                    log.info("case %s started from email %s", case_id, message.message_id)
                    count += 1
        return count

    def _ingest_inbox(self) -> int:
        templates = self._folder_templates()
        if not templates:
            return 0
        count = 0
        for path in sorted(self.settings.inbox_dir.iterdir()):
            if not path.is_file() or path.name.endswith(SIDECAR_SUFFIXES):
                continue
            stored = self.settings.files_dir / f"{uuid.uuid4().hex}_{path.name}"
            shutil.move(str(path), stored)
            # A file forwarded by the mailroom still carries that instance's
            # storage prefix; the person should see the document's own name.
            payload = {
                "source": "folder",
                "file": str(stored),
                "original_name": _original_name(path.name),
            }
            # A channel's envelope (web form: sender, subject, text) becomes
            # part of the trigger payload - rung 1 of the Vermittler reads it.
            envelope = _read_sidecar(path.with_name(_envelope_name(path)))
            if envelope:
                payload.update({k: v for k, v in envelope.items() if k in ENVELOPE_KEYS})
            # A delivery note from the mailroom (ADR-0024 "betrifft mehrere")
            # travels with the document as envelope metadata, not as a case.
            provenance = _read_sidecar(path.with_name(_provenance_name(path)))
            if provenance:
                payload["provenance"] = provenance
            # Content language (ADR-0022): detected once at intake, carried
            # on the case; an explicit envelope value wins.
            if not payload.get("language"):
                payload["language"] = _language_of_file(stored, ocr=self.ocr_local)
            for definition in templates:
                case_id = self.engine.start_case(definition.id, payload, actor_id="folder-trigger")
                log.info("case started from inbox file", extra={"case": case_id})
                count += 1
        return count

    # -- agent executor ----------------------------------------------------
    #
    # With a configured model client, AgentSteps run against the model
    # router; without one (no API key) the stub keeps the process moving,
    # so the core stays fully usable offline and in tests.

    def _pending(self, kind: StepKind) -> list:
        """Pending steps for this runner: a synchronous run sees only its
        own chain, the tick everything except the chains such runs own."""
        chain = self._scope.get(threading.get_ident())
        if chain is not None:
            stops = self._paused.get(chain, set())
            return [
                s for s in self.engine.pending_steps(kind, chain_id=chain)
                if s["step_id"] not in stops
            ]
        return self.engine.pending_steps(
            kind, exclude_chains=self._sync_chains | set(self._paused)
        )

    def _run_agents(self) -> int:
        count = 0
        for step in self._pending(StepKind.AGENT_STEP):
            try:
                self._run_agent_step(step)
            except (KeyError, EngineError) as exc:
                # The definition changed under a running case (a step removed
                # in the workshop) or the case moved on meanwhile: say so
                # once per tick instead of ending the tick for every case.
                self.diag(
                    "error", "agent", "step_skipped", f"{exc.__class__.__name__}: {exc}",
                    case_id=step["case_id"], step_id=step["step_id"],
                )
                continue
            count += 1
        return count

    def _run_agent_step(self, step) -> None:
        definition = self.engine.definitions[self._template_of(step["case_id"])]
        config = definition.steps[step["step_id"]].config
        if config.get("classify"):
            self._run_classifier(step, definition)
        elif config.get("assign"):
            self._run_assigner(step, definition)
        elif self._role_of(config) is not None:
            # A briefing names a model role (ADR-0035 §3): the role is a
            # requirement, not a hint. Without a bound, usable model the
            # step fails with the reason, the process takes its error path
            # and the diagnosis names the role - a "done" without content
            # would only move the gap to the next gate.
            try:
                client = self._client_for(definition, config)
            except ModelError as exc:
                self._fail_agent_step(step, definition, None, str(exc))
                return
            if client is None:
                self._fail_agent_step(
                    step, definition, None,
                    f"model role {self._role_of(config)!r} has no model - bind one under"
                    " Verwaltung -> Modelle",
                )
                return
            self._run_model_agent(step, definition)
        elif "task" not in config or self.model_client is None:
            # Legacy agent steps (TOML templates, intake mechanics) keep the
            # offline stub: the instance stays operable without a model and
            # the person sees the gap at the next gate.
            self.engine.complete_step(
                step["case_id"],
                step["step_id"],
                config.get("stub_outcome", "done"),
                result={"stub": True, "note": "no model configured or no task defined"},
                actor_type="agent",
                actor_id="stub-executor",
                row_id=step["id"],
            )
        else:
            self._run_model_agent(step, definition)

    # -- routing per model role (ADR-0035 §3) ------------------------------

    def _role_of(self, config: dict) -> str | None:
        briefing = config.get("briefing")
        if isinstance(briefing, dict) and briefing.get("model_role"):
            return str(briefing["model_role"])
        return None

    def _client_for(self, definition: ProcessDefinition, config: dict) -> ModelClient | None:
        """The client that serves this step.

        A step without a briefing (legacy TOML, intake mechanics) keeps the
        instance router. A step with a briefing names a model role; the
        active configuration may map that role to ``recording`` (then the
        replaying client set by the proving run serves it - never a live
        model) or to a registry entry; otherwise the instance binds the
        role to the first released entry that satisfies it (local first,
        see :meth:`model_role_bindings`). An unbindable role or a missing
        secret fails the step loudly - a role is a requirement, not a hint.
        """
        role = self._role_of(config)
        if role is None:
            return self.model_client
        configured = self.configuration.model_roles.get(role) or self.configuration.model_roles.get(
            "*"
        )
        if configured == RECORDED_MODEL_REF:
            if not isinstance(self.model_client, RecordingClient):
                raise ModelError(
                    f"configuration {self.configuration.name!r} maps model role {role!r} to"
                    " recordings, but no recordings are loaded - live call refused"
                )
            return self.model_client
        if isinstance(self.model_client, RecordingClient):
            # A proving run replays everything it recorded, whatever the role.
            return self.model_client
        entry: model_registry.ModelEntry | None = None
        if configured:
            entry = self.registry.get(configured)
            if entry is None:
                raise ModelError(
                    f"configuration {self.configuration.name!r} maps model role {role!r} to"
                    f" unknown registry entry {configured!r}"
                )
        else:
            app_id = self.template_app.get(definition.id)
            binding = self.model_role_bindings().get(app_id or "", {}).get(role)
            if binding is None:
                raise ModelError(f"model role {role!r} is not declared by solution {app_id!r}")
            if not binding["model"]:
                raise ModelError(f"model role {role!r} cannot be bound: {binding['reason']}")
            entry = self.registry[binding["model"]]
            stand_ins = [
                self.registry[i] for i in binding.get("fallbacks", []) if i in self.registry
            ]
            if stand_ins and providers.client_for(entry, self.settings) is not None:
                chain = [self.gateway_client(entry)]
                for extra in stand_ins:
                    try:
                        chain.append(self.gateway_client(extra))
                    except ModelError:
                        continue  # a stand-in without its secret is simply not in line
                if len(chain) > 1:
                    key = "chain:" + ",".join(getattr(c, "entries", ((),))[0].id for c in chain)
                    client = self._role_clients.get(key)
                    if client is None:
                        client = FallbackClient(
                            chain,
                            entries=tuple(e for c in chain for e in getattr(c, "entries", ())),
                        )
                        self._role_clients[key] = client
                    return client
        client = self._role_clients.get(entry.id)
        if client is None:
            client = providers.client_for(entry, self.settings)
            if client is None:
                missing = providers.missing_secret(entry, self.settings)
                raise ModelError(
                    f"model role {role!r} is bound to {entry.id!r}, which needs secret"
                    f" {missing!r}"
                )
            client.entries = (entry,)  # type: ignore[attr-defined]
            self._role_clients[entry.id] = client
        return client

    def gateway_client(self, entry: model_registry.ModelEntry) -> ModelClient:
        """The client of one registry entry for the gateway (ADR-0047),
        shared with the role-bound briefings - one connection per
        deployment, the entry attached for budget and pricing."""
        client = self._role_clients.get(entry.id)
        if client is None:
            client = providers.client_for(entry, self.settings)
            if client is None:
                missing = providers.missing_secret(entry, self.settings)
                raise ModelError(f"model {entry.id!r} needs secret {missing!r}")
            client.entries = (entry,)  # type: ignore[attr-defined]
            self._role_clients[entry.id] = client
        return client

    def _dominanz_violation(
        self, definition: ProcessDefinition, config: dict, client: ModelClient | None = None
    ) -> str | None:
        """The dominance rule (K2), checked fail-closed before any call:
        the model's clearance must cover the step's declared data class.
        Generalizes the earlier privacy-only check - the privacy category
        remains the template-level default demand."""
        if client is None:
            client = self.model_client
        try:
            need = self._need(definition, config)
        except data_classes.DataClassError as exc:
            return str(exc)
        clearance = getattr(client, "clearance", None)
        if clearance is None:
            # Clients without a declaration: local models are trusted to
            # the top, remote ones default to "confidential".
            clearance = (
                "sensitive"
                if getattr(client, "local", False)
                else "confidential"
            )
        if not data_classes.clearance_suffices(clearance, need):
            return (
                f"data class {need!r} exceeds the model's clearance {clearance!r}"
                " - call refused (dominance rule, fail-closed)"
            )
        # Legal space (ADR-0027): every deployment the router may call has
        # to be permitted for this class in the instance's country.
        country = self.country()
        for entry in getattr(client, "entries", ()):
            reason = legal.violation(country, entry, need)
            if reason:
                return f"{reason} - call refused (legal space, fail-closed)"
        return None

    def budget_state(self) -> dict:
        return metrics.budget_state(
            self.conn,
            float(self.budget_config.get("monthly_budget_chf") or 0.0),
            bool(self.budget_config.get("budget_stop")),
        )

    def _call_cap_violation(self, case_id: str) -> str | None:
        """ADR-0038 §2: a case (with its chain) gets at most
        ``max_model_calls_per_case`` model calls - the third ceiling next to
        ``max`` per multi-instance step and the monthly budget."""
        limit = int(self.settings.max_model_calls_per_case)
        if limit <= 0:
            return None
        used = self.conn.execute(
            "SELECT COUNT(*) FROM llm_interactions l JOIN cases c ON c.id = l.case_id"
            " WHERE c.chain_id = (SELECT chain_id FROM cases WHERE id = ?)",
            (case_id,),
        ).fetchone()[0]
        if used >= limit:
            return (
                f"case has used {used} model calls, the ceiling is {limit}"
                " (max_model_calls_per_case) - call refused"
            )
        return None

    def _read_cap_violation(self, case_id: str) -> str | None:
        """ADR-0042 §3: read-only outside calls (fetch, search) run without
        a gate, so a case (with its chain) gets at most
        ``max_read_calls_per_case`` of them - a runaway loop cannot turn
        the instance into a crawler. Counted from the audit trail, live
        calls only (stubs never leave the instance)."""
        limit = int(self.settings.max_read_calls_per_case)
        if limit <= 0:
            return None
        used = self.conn.execute(
            "SELECT COUNT(*) FROM audit_log a JOIN cases c ON c.id = a.case_id"
            " WHERE c.chain_id = (SELECT chain_id FROM cases WHERE id = ?)"
            " AND a.actor_type = 'system'"
            " AND (a.actor_id LIKE 'fetch:%' OR a.actor_id LIKE 'search:%')",
            (case_id,),
        ).fetchone()[0]
        if used >= limit:
            return (
                f"case has made {used} read calls, the ceiling is {limit}"
                " (max_read_calls_per_case) - call refused"
            )
        return None

    def _flag_foreign_text(self, case_id: str, step_id: str, source: str, text: str) -> None:
        """Foreign content that arrived outside a model call (a fetched
        page): run the injection check now and leave the mark in the
        diagnosis trail, so the person sees it before the next briefing
        reads the text (ADR-0038 §1.6)."""
        result = guardrails.run("injection_suspect", {source: text}, "pre")
        if result.action == "flag":
            self.diag(
                "warning", "security", "injection_suspect:flag", result.detail,
                case_id=case_id, step_id=step_id, origin=source,
            )

    def pop_flags(self, case_id: str, step_id: str) -> list[str]:
        """Security flags the last call for this step raised (ADR-0038)."""
        return self._flags.pop((case_id, step_id), [])

    def _budget_refusal(self, client: ModelClient | None = None) -> str | None:
        """ADR-0029: with ``budget_stop`` an exhausted month refuses calls
        that could cost money - a router with any priced (remote) entry.
        Local-only routers keep running; the warning at 80 % is diagnosed
        once per month so it shows up in Verwaltung -> Protokoll."""
        state = self.budget_state()
        if state["state"] in ("warning", "exceeded"):
            month = metrics.month_start(datetime.now(UTC))
            if self._budget_warned != month:
                self._budget_warned = month
                self.diag(
                    "warning", "budget", state["state"],
                    f"{state['spent_chf']} of {state['monthly_budget_chf']} CHF spent this month",
                )
        if not state["stop"]:
            return None
        entries = getattr(client if client is not None else self.model_client, "entries", ())
        paid = [e for e in entries if not getattr(e, "local", False)]
        if not paid and entries:
            return None
        return (
            f"monthly model budget of {state['monthly_budget_chf']} CHF exhausted"
            f" ({state['spent_chf']} CHF spent) - paid model calls refused (ADR-0029)"
        )

    def _call_model(
        self,
        definition: ProcessDefinition,
        step,
        config: dict,
        task: str,
        payload,
        schema_hint,
        client: ModelClient | None = None,
    ) -> tuple[ModelResponse, dict]:
        """The one place a model is called (K1/K2/K3).

        Runs the pre-guardrails on the payload, calls the client, runs
        the post-guardrails on the answer, freezes the CHF cost from the
        registry price and writes the content-trail row - success or
        failure. Raises ModelError on failure paths (guardrail block/
        escalate included), which the callers route to the step's
        ``failed`` outcome.
        """
        case_id, step_id = step["case_id"], step["step_id"]
        if client is None:
            client = self.model_client
        data_class = self._need(definition, config)
        policy = definition.policy("guardrails")

        refusal = self._budget_refusal(client)
        if refusal:
            self.diag("error", "budget", "call_refused", refusal, case_id=case_id, step_id=step_id)
            raise ModelError(refusal)
        cap = self._call_cap_violation(case_id)
        if cap:
            self.diag("error", "security", "call_cap", cap, case_id=case_id, step_id=step_id)
            raise ModelError(cap)

        # Instance base lists plus the process's own (ADR-0038 §1.5); a
        # briefing may opt out of single post checks it needs for its job.
        pre_names = guardrails.merged(self.settings.guardrails_pre, policy.get("pre", []))
        post_names = [
            name
            for name in guardrails.merged(self.settings.guardrails_post, policy.get("post", []))
            if not (name in guardrails.OPT_OUT and config.get(guardrails.OPT_OUT[name]) is True)
        ]
        flags: list[str] = []
        # personal_data = "sources": identifiers the step's own passages
        # carry (a public office's phone number) may stay in the answer
        keep = None
        if config.get("personal_data") == "sources":
            passages = json.dumps(payload.get("knowledge") or {}, ensure_ascii=False)
            keep = guardrails.public_identifiers(passages)

        for name in pre_names:
            result = guardrails.run(name, payload, "pre")
            if result.action == "flag":
                flags.append(f"{name}: {result.detail}")
                self.diag(
                    "warning", "security", f"{name}:flag", result.detail,
                    case_id=case_id, step_id=step_id, phase="pre",
                )
            elif result.action == "redact":
                payload = result.payload
                self.diag(
                    "warning", "guardrail", f"{name}:redact", result.detail,
                    case_id=case_id, step_id=step_id, phase="pre",
                )
            elif result.action in ("block", "escalate"):
                self.diag(
                    "error", "guardrail", f"{name}:{result.action}", result.detail,
                    case_id=case_id, step_id=step_id, phase="pre",
                )
                raise ModelError(f"guardrail {name!r} ({result.action}): {result.detail}")

        started = time.monotonic()
        streamer = self._streamer_for(case_id, step_id, config, client, post_names, keep)
        try:
            live = {"on_text": streamer.feed} if streamer is not None else {}
            with self._unlocked():
                response = client.complete_structured(
                    task=task, payload=payload, schema_hint=schema_hint, **live
                )
        except ModelError as exc:
            self._log_llm(
                self.conn,
                task=task,
                payload=payload,
                status="error",
                case_id=case_id,
                step_id=step_id,
                data_class=data_class,
                duration_ms=int((time.monotonic() - started) * 1000),
                response={"raw": exc.raw} if getattr(exc, "raw", None) else None,
                error=str(exc),
            )
            raise

        if isinstance(client, FallbackClient) and client.used:
            self.diag(
                "warning", "model", "fallback_used",
                f"{response.model_ref} answered after {client.used} failure(s): "
                + " | ".join(client.failures)[:500],
                case_id=case_id, step_id=step_id,
            )
        for name in post_names:
            result = guardrails.run(name, response.data, "post", keep)
            if result.action == "flag":
                flags.append(f"{name}: {result.detail}")
                self.diag(
                    "warning", "security", f"{name}:flag", result.detail,
                    case_id=case_id, step_id=step_id, phase="post",
                )
            elif result.action == "redact":
                response = replace(response, data=result.payload)
                self.diag(
                    "warning", "guardrail", f"{name}:redact", result.detail,
                    case_id=case_id, step_id=step_id, phase="post",
                )
            elif result.action in ("block", "escalate"):
                self.diag(
                    "error", "guardrail", f"{name}:{result.action}", result.detail,
                    case_id=case_id, step_id=step_id, phase="post",
                )
                raise ModelError(f"guardrail {name!r} ({result.action}): {result.detail}")
        self._flags[(case_id, step_id)] = flags

        entry = model_registry.find_by_model_ref(self.registry, response.model_ref)
        self._log_llm(
            self.conn,
            task=task,
            payload=payload,
            status="ok",
            case_id=case_id,
            step_id=step_id,
            response=response.data,
            model_ref=response.model_ref,
            data_class=data_class,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_chf=entry.cost_chf(response.tokens_in, response.tokens_out)
            if entry
            else None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return response, payload

    def _demand_of(self, definition: ProcessDefinition, config: dict) -> str | None:
        """The step's data class for OCR decisions; None when undeclared
        (then the dominance check reports the real error a moment later).
        Bound knowledge spaces raise the demand to their class (ADR-0031)."""
        try:
            return self._need(definition, config)
        except data_classes.DataClassError:
            return None

    def _need(self, definition: ProcessDefinition, config: dict) -> str:
        need = data_classes.demand(config, definition.policies)
        spaces = self._knowledge_spaces_of(config)
        if spaces:
            highest = self.knowledge.demand_of(spaces)
            if highest and data_classes.rank(highest) > data_classes.rank(need):
                need = highest
        return need

    def _run_model_agent(self, step, definition: ProcessDefinition) -> None:
        config = definition.steps[step["step_id"]].config
        try:
            payload = self._agent_payload(
                step["case_id"], config, self._demand_of(definition, config), step
            )
        except (DocumentError, KnowledgeError) as exc:
            self._fail_agent_step(step, definition, None, str(exc))
            return
        knowledge = payload.get("knowledge")
        if (knowledge and not knowledge.get("grounded") and config.get("ungrounded_outcome")
                and not config.get("ungrounded_call")):
            # Nothing to stand on: no model call, the process takes the
            # declared path (an honest "not in my sources"). With
            # ``ungrounded_call`` the model is asked anyway - for what the
            # input itself carries (an assistant's own name and role).
            self.engine.complete_step(
                step["case_id"],
                step["step_id"],
                str(config["ungrounded_outcome"]),
                result={"grounded": False, "knowledge_query": knowledge.get("query", ""),
                        "knowledge_sources": [], "knowledge_hits": 0},
                actor_type="agent",
                actor_id="knowledge-check",
                row_id=step["id"],
            )
            return
        try:
            client = self._client_for(definition, config)
        except ModelError as exc:
            self._fail_agent_step(step, definition, payload, str(exc))
            return
        reason = self._dominanz_violation(definition, config, client)
        if reason:
            # Without a sufficiently cleared model the step fails loudly
            # instead of quietly calling out.
            self._fail_agent_step(step, definition, payload, reason)
            return
        try:
            response, payload = self._call_model(
                definition, step, config, str(config["task"]), payload, config.get("output"),
                client,
            )
        except (ModelError, DocumentError) as exc:
            self._fail_agent_step(step, definition, payload, str(exc))
            return
        threshold = definition.policy("model").get("confidence_threshold")
        if threshold is not None and response.confidence < float(threshold):
            self._fail_agent_step(
                step,
                definition,
                payload,
                f"confidence {response.confidence} below policy threshold {threshold}"
                f" (model {response.model_ref})",
            )
            return
        result = response.data
        if knowledge and config.get("knowledge_cite") and isinstance(result, dict):
            # Provenance from the retrieval, not from the model: what the
            # step was given, numbered as the model saw it (ADR-0031).
            sources = _knowledge_sources(knowledge)
            result = {**result, "knowledge_query": knowledge.get("query", ""),
                      "knowledge_sources": sources, "knowledge_hits": len(sources)}
            if not sources:  # asked anyway (ungrounded_call): say it stood on nothing
                result["grounded"] = False
        self.engine.complete_step(
            step["case_id"],
            step["step_id"],
            config.get("success_outcome", "done"),
            result=result,
            actor_type="agent",
            actor_id="model-executor",
            model_ref=response.model_ref,
            payload=payload,
            audit_extra=_prompt_audit(
                config, self._role_of(config), self.pop_flags(step["case_id"], step["step_id"])
            ),
            row_id=step["id"],
        )

    # -- trying a building block (ADR-0067, stage 2) -----------------------

    def try_agent(self, app_id: str, agent_id: str, text: str, *, name: str = "try.txt") -> dict:
        """One agent on one input, outside any process: what the workshop's
        "try it" runs. No case, no audit entry - but the same call path as
        a step (:meth:`_call_model`: dominance rule, jurisdiction, budget,
        guardrails, content trail under a ``try-…`` id), the same role
        binding and the same knowledge retrieval. The input is shaped as a
        sidecar case shapes it (``given.text``), so what was tried can be
        kept as a proving case and replays against the same prompt hash.

        The agent only reads and proposes here, as everywhere: tools are
        named to it, none is called."""
        from .definition import END, StepDefinition
        from .template import _substitute

        app = self.apps[app_id]
        briefing = app.briefings[agent_id]
        config = _substitute(
            briefing.to_config(app.skills), None, f"ai/{agent_id}", agent_id, app.constants
        )
        run_id = "try-" + uuid.uuid4().hex[:12]
        definition = ProcessDefinition(
            id=f"try:{run_id}", version=1,
            steps={agent_id: StepDefinition(
                id=agent_id, kind=StepKind.AGENT_STEP, next={"done": END}, config=config)},
        )
        step = {"case_id": run_id, "step_id": agent_id}
        payload: dict = {"prior_steps": {}, "trigger": {"source": "try", "original_name": name}}
        if config.get("read_document") or (config.get("context") or {}).get("document"):
            payload["documents"] = [{"name": name, "method": "text", "text": text}]
        else:
            payload["text"] = text
        report: dict = {"run": run_id, "prompt_hash": config.get("prompt_hash"), "ok": False,
                        "role": self._role_of(config)}
        started = time.perf_counter()
        me = threading.get_ident()
        with self.lock:
            self.template_app[definition.id] = app_id
            self._runners.add(me)  # the lock is let go around the model call, as in a run
            try:
                spaces = self._knowledge_spaces_of(config)
                if spaces:
                    payload["knowledge"] = self.knowledge.context(
                        spaces, str(config.get("knowledge_query") or text)[:2000],
                        top_k=int(config.get("knowledge_top_k") or DEFAULT_TOP_K),
                        require_grounding=bool(config.get("require_grounding", False)),
                        per_source=int(config.get("knowledge_per_source") or 0) or None,
                        numbered=bool(config.get("knowledge_cite", False)),
                    )
                    report["knowledge_sources"] = _knowledge_sources(payload["knowledge"])
                client = self._client_for(definition, config)
                if client is None:
                    raise ModelError(
                        f"model role {report['role']!r} has no model - bind one under"
                        " Verwaltung -> Modelle"
                    )
                reason = self._dominanz_violation(definition, config, client)
                if reason:
                    raise ModelError(reason)
                response, _sent = self._call_model(
                    definition, step, config, str(config.get("task", "")), payload,
                    config.get("output"), client,
                )
                report.update(ok=True, result=response.data, model=response.model_ref,
                              confidence=response.confidence,
                              tokens_in=response.tokens_in, tokens_out=response.tokens_out)
            except (ModelError, DocumentError, KnowledgeError, data_classes.DataClassError) as exc:
                report["error"] = str(exc)
            finally:
                self._runners.discard(me)
                self.template_app.pop(definition.id, None)
                report["flags"] = self.pop_flags(run_id, agent_id)
            row = self.conn.execute(
                "SELECT kosten_chf FROM llm_interactions WHERE case_id = ?"
                " ORDER BY at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        report["cost_chf"] = row["kosten_chf"] if row else None
        report["ms"] = round((time.perf_counter() - started) * 1000)
        return report

    def try_client(self, app_id: str, agent_id: str) -> ModelClient | None:
        """The model that serves this agent's role in this instance - what
        a recording of its proving case is taken through."""
        from .definition import END, StepDefinition

        config = self.apps[app_id].briefings[agent_id].to_config(self.apps[app_id].skills)
        definition = ProcessDefinition(
            id=f"try:client:{uuid.uuid4().hex[:8]}", version=1,
            steps={agent_id: StepDefinition(
                id=agent_id, kind=StepKind.AGENT_STEP, next={"done": END}, config=config)},
        )
        with self.lock:
            self.template_app[definition.id] = app_id
            try:
                return self._client_for(definition, config)
            finally:
                self.template_app.pop(definition.id, None)

    def try_tool(self, app_id: str, tool_id: str, inputs: dict) -> dict:
        """One tool on given inputs. A tool that only reads (``effect =
        "read"``: fetch, search) is really called - under the host policy,
        and what it brings back is checked like any foreign content. A tool
        with an outside effect is **never** called here: there is no gate
        outside a process (ADR-0007), so the report only says what would be
        sent where, with secrets shown as their names."""
        from .connectors import ConnectorError, _render, fetch_json, fetch_page, search_web

        app = self.apps[app_id]
        tool = app.tools[tool_id]
        missing = [k for k in tool.inputs if k not in inputs]
        if missing:
            return {"ok": False, "mode": "refused", "error": f"missing input(s): {missing}"}
        given = {k: inputs[k] for k in tool.inputs}
        if tool.effect != "read" or (tool.fetch is None and tool.search is None):
            preview: dict = {}
            try:
                if tool.http is not None:
                    preview = {
                        "method": tool.http.method,
                        "url": _render(
                            tool.http.url, given, None, app.constants, quote_inputs=True
                        ),
                        "headers": sorted(tool.http.headers),
                        "body": _render(tool.http.body, given, None, app.constants),
                    }
                elif tool.mcp is not None:
                    preview = {"server": tool.mcp.server, "tool": tool.mcp.tool,
                               "arguments": _render(tool.mcp.arguments, given, None, app.constants)}
                elif tool.exec == "client":
                    preview = {"instruction": pick(tool.description)}
            except (ConnectorError, KeyError) as exc:
                return {"ok": False, "mode": "refused", "error": str(exc)}
            return {"ok": True, "mode": "preview", "inputs": given, "request": preview}
        run_id = "try-" + uuid.uuid4().hex[:12]
        started = time.perf_counter()
        if tool.fetch is not None:
            reader = fetch_json if tool.fetch.format == "json" else fetch_page
            outcome = reader(
                tool.fetch, given, transport=self._http_transport,
                allow_private=self.settings.tools_allow_private_hosts,
            )
        else:
            outcome = search_web(
                tool.search, given,
                lambda name: (
                    resolve_secret(self.settings.data_dir, name) if is_tool_secret(name) else None
                ),
                transport=self._http_transport,
                allow_private=self.settings.tools_allow_private_hosts,
            )
        report = {"ok": outcome.ok, "mode": "live", "inputs": given, "status": outcome.status,
                  "ms": round((time.perf_counter() - started) * 1000)}
        if not outcome.ok:
            report["error"] = outcome.error
            return report
        report["outputs"] = outcome.outputs
        check = guardrails.run(
            "injection_suspect",
            {f"try.{tool.id}": json.dumps(outcome.outputs, ensure_ascii=False)[:200_000]}, "pre",
        )
        report["flags"] = [check.detail] if check.action == "flag" else []
        if report["flags"]:
            with self.lock:
                self.diag("warning", "security", "injection_suspect:flag", check.detail,
                          case_id=run_id, step_id=tool.id, origin=f"try.{tool.id}")
        return report

    # -- intake classification & dispatch (docs/AGENTIK.md) ----------------

    def _prepare_recordings(self, case_id: str) -> None:
        """Transcribe audio/video of a case with the local engine only (no
        vision, no model); the result lands in the extraction cache. Errors
        are diagnosed, never raised - this is a courtesy, not a step."""
        if self.transcriber_local is None:
            return
        row = self.conn.execute(
            "SELECT result FROM steps WHERE case_id = ? AND kind = 'trigger'", (case_id,)
        ).fetchone()
        trigger = json.loads(row["result"]) if row and row["result"] else {}
        origin = origin_trigger(trigger)
        for candidate in _origin_files(origin):
            if not document_is_media(candidate):
                continue
            try:
                with self._unlocked():
                    extract_text(
                        candidate, transcriber=self.transcriber_local, ffmpeg=self.ffmpeg
                    )
            except DocumentError as exc:
                self.diag("warning", "transcription", "failed", str(exc), case_id=case_id)

    def _run_classifier(self, step, definition: ProcessDefinition) -> None:
        """Intake: the agent judges which document type is present.

        The judgment call is agentic; everything around it is not. A
        recognized type needs the category to be declared AND the model's
        confidence to clear the policy threshold - anything else goes to
        the ``unknown`` outcome, i.e. to a human. Routing itself happens
        later in the deterministic dispatch action.
        """
        config = definition.steps[step["step_id"]].config
        unknown = config.get("unknown_outcome", "unknown")
        if self.model_client is None or not self.doctypes:
            # No model - but a recording is still transcribed locally, so
            # the person at the sorting gate reads instead of listens.
            self._prepare_recordings(step["case_id"])
            self.engine.complete_step(
                step["case_id"],
                step["step_id"],
                unknown,
                result={"stub": True, "note": "no model or no document types configured"},
                actor_type="agent",
                actor_id="stub-executor",
                row_id=step["id"],
            )
            return
        try:
            payload = self._agent_payload(
                step["case_id"], config, self._demand_of(definition, config)
            )
        except (DocumentError, KnowledgeError) as exc:
            self._fail_agent_step(step, definition, None, str(exc))
            return
        reason = self._dominanz_violation(definition, config)
        if reason:
            self._fail_agent_step(step, definition, payload, reason)
            return
        try:
            response, payload = self._call_model(
                definition, step, config, self._classification_task(), payload,
                config.get("output"),
            )
        except (ModelError, DocumentError) as exc:
            self._fail_agent_step(step, definition, payload, str(exc))
            return
        category = ""
        if isinstance(response.data, dict):
            category = str(response.data.get("category", ""))
        threshold = float(definition.policy("model").get("confidence_threshold", 0.0))
        recognized = category in self.doctypes and response.confidence >= threshold
        self.engine.complete_step(
            step["case_id"],
            step["step_id"],
            config.get("success_outcome", "recognized") if recognized else unknown,
            result=response.data,
            actor_type="agent",
            actor_id="model-executor",
            model_ref=response.model_ref,
            payload=payload,
            audit_extra=_prompt_audit(
                config, None, self.pop_flags(step["case_id"], step["step_id"])
            ),
            row_id=step["id"],
        )

    # -- Vermittler: assignment ladder (docs/VERMITTLER.md) ----------------

    def _envelope(self, case_id: str) -> dict:
        """Envelope metadata of a case: what the Vermittler may read at
        rung 1 without opening the document - source, names, mail header
        fields. Deliberately NOT the document content."""
        row = self.conn.execute(
            "SELECT result FROM steps WHERE case_id = ? AND kind = 'trigger'",
            (case_id,),
        ).fetchone()
        payload = json.loads(row["result"]) if row and row["result"] else {}
        fields = ("source", "original_name", "from", "to", "subject", "sender", "recipient")
        return {k: payload[k] for k in fields if payload.get(k) is not None}

    def _run_assigner(self, step, definition: ProcessDefinition) -> None:
        """The escalation ladder of the Vermittler.

        Rung 1: a matching standing mandate (deterministic rule over the
        envelope, ADR-0016) assigns without any model and without a gate.
        Rung 2: a model may propose an addressee from a minimal excerpt -
        the proposal always goes to the gate, it never delivers by itself.
        Rung 3 (no rule, no usable proposal): the human decides at the
        gate. Offline, everything lands at the gate.
        """
        config = definition.steps[step["step_id"]].config
        case_id = step["case_id"]
        envelope = self._envelope(case_id)
        mandate = self.engine.find_mandate("forward", envelope)
        if mandate is not None:
            self.engine.complete_step(
                case_id,
                step["step_id"],
                "rule",
                result={
                    "addressee": mandate["target"],
                    "mandate": mandate["id"],
                    "envelope": envelope,
                },
                actor_type="system",
                actor_id="mandate-rule",
            )
            return
        if self.model_client is not None and self.addressees:
            reason = self._dominanz_violation(definition, config)
            if reason is None:
                try:
                    payload = self._agent_payload(
                        case_id, config, self._demand_of(definition, config)
                    )
                    payload["envelope"] = envelope
                    response, payload = self._call_model(
                        definition, step, config, self._assignment_task(), payload,
                        config.get("output"),
                    )
                except (ModelError, DocumentError) as exc:
                    self._fail_agent_step(step, definition, None, str(exc))
                    return
                addressee = ""
                if isinstance(response.data, dict):
                    addressee = str(response.data.get("addressee", ""))
                threshold = float(definition.policy("model").get("confidence_threshold", 0.0))
                usable = addressee in self.addressees and response.confidence >= threshold
                self.engine.complete_step(
                    case_id,
                    step["step_id"],
                    "proposal" if usable else "unknown",
                    result={**response.data, "envelope": envelope}
                    if isinstance(response.data, dict)
                    else {"envelope": envelope},
                    actor_type="agent",
                    actor_id="model-executor",
                    model_ref=response.model_ref,
                    payload=payload,
                )
                return
            self._fail_agent_step(step, definition, None, reason)
            return
        self.engine.complete_step(
            case_id,
            step["step_id"],
            "unknown",
            result={"stub": True, "envelope": envelope},
            actor_type="agent",
            actor_id="stub-executor",
        )

    def _assignment_task(self) -> str:
        lines = [
            "Determine which addressee this document is meant for.",
            "Read as little as possible - envelope data and letterhead usually suffice.",
            "Known addressees:",
        ]
        for addressee in sorted(self.addressees.values(), key=lambda a: a.id):
            lines.append(f"- {addressee.id}: {addressee.title}")
        lines.append(
            'Answer with {"addressee": "<addressee id>", "confidence": 0.0-1.0}.'
            ' If none fits, use "unknown".'
        )
        return "\n".join(lines)

    def _classification_task(self) -> str:
        lines = [
            "Classify the document into exactly one of the known document types.",
            "Known document types:",
        ]
        for doctype in sorted(self.doctypes.values(), key=lambda d: d.id):
            signs = "; ".join(doctype.features) or doctype.description
            lines.append(f"- {doctype.id}: {doctype.title}. Typical signs: {signs}")
        lines.append(
            'Answer with {"category": "<type id>", "confidence": 0.0-1.0}.'
            ' If no listed type fits, use "unknown".'
        )
        return "\n".join(lines)

    def _fail_agent_step(self, step, definition: ProcessDefinition, payload, reason: str) -> None:
        self.diag(
            "error", "agent", "agent_step_failed", reason,
            case_id=step["case_id"], step_id=step["step_id"], template=definition.id,
        )
        if "failed" in definition.steps[step["step_id"]].next:
            self.engine.complete_step(
                step["case_id"],
                step["step_id"],
                "failed",
                result={"error": reason},
                actor_type="agent",
                actor_id="model-executor",
                payload=payload,
                row_id=step["id"],
            )
        else:
            log.error("agent step %s failed, left pending: %s", step["step_id"], reason)

    def _agent_payload(
        self, case_id: str, config: dict, data_class: str | None = None, step_row=None
    ) -> dict:
        """Collect the input for an agent step - context discipline (K4).

        By default a step sees the trigger, all prior step results and -
        if it asks via ``read_document`` - the extracted document text.
        A ``context`` table in the step config narrows that deliberately
        (only the named prior steps, no trigger, tighter document limit):
        what a step does not need must not reach the model - for cost,
        quality and data economy alike.

        ``context`` keys: ``today`` (bool: the instance's date as
        ``today``), ``steps`` (list of prior step ids; absent =
        all), ``trigger`` (bool, default true), ``document`` (bool,
        default = ``read_document``), ``max_chars`` (per-document
        truncation limit, default 60000), ``records`` (list of record
        schema ids whose current values the step may see, ADR-0026;
        absent = none - master data reaches a model only on request).
        """
        context: dict = config.get("context") or {}
        allowed_steps = context.get("steps")  # None = all prior steps
        include_trigger = bool(context.get("trigger", True))
        want_document = bool(context.get("document", config.get("read_document", False)))
        max_chars = int(context.get("max_chars", 60_000))

        trigger, completed = self.engine.step_outputs(case_id)
        prior: dict = {
            sid: {"outcome": entry["outcome"], "result": entry["result"]}
            for sid, entry in completed.items()
            if allowed_steps is None or sid in allowed_steps
        }
        payload: dict = {"prior_steps": prior}
        if include_trigger:
            payload["trigger"] = trigger
        if context.get("today"):
            # Opt-in (``context.today = true``): the instance's date, from the
            # engine clock - pinned in a proving run - for steps that judge
            # deadlines (overdue invoices, notice periods).
            now = self.clock() if self.clock else datetime.now(UTC)
            payload["today"] = now.astimezone().date().isoformat()
        if step_row is not None and step_row["instance_total"] is not None:
            # One instance of a multi-instance step (ADR-0037 §2): the
            # element it works on, under the declared name, plus its place.
            payload.update(json.loads(step_row["instance_input"] or "{}"))
            payload["instance"] = {
                "index": step_row["instance_index"], "total": step_row["instance_total"]
            }
        wanted_records = context.get("records") or []
        if wanted_records:
            payload["records"] = {
                sid: self.record_context(sid) for sid in wanted_records if sid in self.records
            }
        if want_document:
            # Chain-started cases carry the original trigger nested in the
            # hand-over payload; the file lives there (ADR-0014).
            origin = origin_trigger(trigger)
            documents = []
            ocr = self.ocr_for(data_class)
            describe = self.frame_describer(data_class)
            for candidate in _origin_files(origin):
                with self._unlocked():
                    extracted = extract_text(  # DocumentError handled by caller
                        candidate,
                        ocr=ocr,
                        transcriber=self.transcriber_local,
                        ffmpeg=self.ffmpeg,
                        describe=describe,
                    )
                text = extracted.text
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n[... gekürzt (Kontext-Limit) ...]"
                documents.append(
                    {
                        "name": Path(candidate).name,
                        "method": extracted.method,
                        "text": text,
                    }
                )
            payload["documents"] = documents
        spaces = self._knowledge_spaces_of(config) + self._input_spaces(
            config, trigger, data_class
        )
        if spaces:
            # Retrieval (ADR-0031), fail-closed: the query is the trigger's
            # subject plus the head of the document text unless the step
            # names a literal ``knowledge_query`` - or a field of the input
            # (``{trigger.query}``).
            query = str(config.get("knowledge_query") or "").strip()
            if _QUERY_REF.search(query):
                scope: dict = {"trigger": trigger}
                if "{steps." in query:
                    scope["steps"] = self._decision_context(case_id, {})["steps"]
                query = " ".join(_QUERY_REF.sub(
                    lambda m: _plain(_lookup_path(scope, m.group(1))), query
                ).split())
            if not query:
                parts = [str(trigger.get("subject") or "")]
                for doc in payload.get("documents") or []:
                    parts.append(str(doc.get("text") or "")[:KNOWLEDGE_QUERY_CHARS])
                query = " ".join(p for p in parts if p).strip()
            t0 = time.perf_counter()
            payload["knowledge"] = self.knowledge.context(
                spaces,
                query,
                top_k=int(config.get("knowledge_top_k") or DEFAULT_TOP_K),
                require_grounding=bool(config.get("require_grounding", False)),
                per_source=int(config.get("knowledge_per_source") or 0) or None,
                numbered=bool(config.get("knowledge_cite", False)),
            )
            if step_row is not None:
                self._note_timing(case_id, str(step_row["step_id"]), "search_ms",
                                  (time.perf_counter() - t0) * 1000)
        return payload

    # -- built-in system actions -------------------------------------------

    def _run_system_actions(self) -> int:
        count = 0
        for step in self._pending(StepKind.SYSTEM_ACTION):
            definition = self.engine.definitions[self._template_of(step["case_id"])]
            config = definition.steps[step["step_id"]].config
            action = config.get("action", "export")
            if self._run_configured_stub(step, definition, config):
                count += 1
                continue
            if config.get("exec") == "client":
                self._run_client_action(step, definition, config)
                count += 1
                continue
            if action.startswith("connector:"):
                self._run_connector(step, definition, config)
                count += 1
                continue
            if action == "dispatch":
                self._dispatch_case(step)
                count += 1
                continue
            if action == "record":
                self._record_row(step, definition, config)
                count += 1
                continue
            if action == "knowledge":
                self._file_knowledge(step, definition, config)
                count += 1
                continue
            if action == "rows":
                self._read_rows(step, definition, config)
                count += 1
                continue
            if action == "links":
                self._extract_links(step, definition, config)
                count += 1
                continue
            if action == "figures":
                self._figures(step, definition, config)
                count += 1
                continue
            if action == "file":
                self._file_documents(step, definition, config)
                count += 1
                continue
            if action == "pdf":
                self._make_pdf(step, definition, config)
                count += 1
                continue
            if action == "mail":
                self._send_mail(step, definition, config)
                count += 1
                continue
            if action == "decide":
                self._decide_case(step, definition, config)
                count += 1
                continue
            if action == "route":
                self._route_case(step, definition, config)
                count += 1
                continue
            if action == "call":
                target = str(config.get("process", ""))
                if target not in self.engine.definitions:
                    self._fail_action(
                        step, definition.steps[step["step_id"]], f"unknown process {target!r}"
                    )
                else:
                    self.engine.call_subprocess(step["case_id"], step["step_id"], target)
                count += 1
                continue
            if action == "forward":
                self._forward_case(step)
                count += 1
                continue
            if action == "archive":
                out = self._archive_case(step["case_id"])
                self.engine.complete_step(
                    step["case_id"],
                    step["step_id"],
                    "done",
                    result={"archived_to": str(out)},
                    actor_type="system",
                    actor_id="archive-action",
                    row_id=step["id"],
                )
                count += 1
                continue
            if action != "export":
                # Connectors (MCP) arrive in a later session. Fail the step
                # via its "failed" outcome if the template defines one;
                # otherwise leave it pending and log (visible, not silent).
                if "failed" in definition.steps[step["step_id"]].next:
                    self.engine.complete_step(
                        step["case_id"],
                        step["step_id"],
                        "failed",
                        result={"error": f"unknown action {action!r}"},
                        actor_type="system",
                        actor_id="runtime",
                        row_id=step["id"],
                    )
                    count += 1
                else:
                    log.error("unknown system action %r, step left pending", action)
                continue
            out = self._export_case(step["case_id"])
            self.engine.complete_step(
                step["case_id"],
                step["step_id"],
                "done",
                result={"exported_to": str(out)},
                actor_type="system",
                actor_id="export-action",
                row_id=step["id"],
            )
            count += 1
        return count

    def _decide_case(self, step, definition: ProcessDefinition, config: dict) -> None:
        """Internal action ``decide`` (businessRuleTask): evaluate a DMN
        table over the case's data - trigger, prior step results, bound
        records - and take the outcome the table names (output
        ``outcome``), else ``done``. Deterministic, no model, audited with
        the matched rules."""
        from . import dmn

        decision_id = str(config.get("decision", ""))
        decision = self.decisions.get(decision_id)
        step_def = definition.steps[step["step_id"]]
        if decision is None:
            self._fail_action(step, step_def, f"unknown decision {decision_id!r}")
            return
        context = self._decision_context(step["case_id"], config)
        try:
            outputs = dmn.evaluate(decision, context)
            rules = dmn.matched_rules(decision, context)
        except dmn.DmnError as exc:
            self._fail_action(step, step_def, str(exc))
            return
        outcome = str(outputs.get("outcome") or "")
        if outcome not in step_def.next:
            if not rules and "failed" in step_def.next:
                outcome = "failed"  # no rule matched: that is a finding, not a default
            elif outcome and "outcome" in outputs:
                # The table named an outcome the process does not have:
                # a modelling error, never silently "done".
                self._fail_action(
                    step, step_def,
                    f"decision {decision_id!r} produced outcome {outcome!r}, which step"
                    f" {step_def.id!r} does not offer ({sorted(step_def.next)})",
                )
                return
            else:
                outcome = "done" if "done" in step_def.next else next(iter(step_def.next))
        self.engine.complete_step(
            step["case_id"],
            step["step_id"],
            outcome,
            result={**outputs, "matched_rules": rules, "decision": decision_id},
            actor_type="system",
            actor_id="decision-table",
            audit_extra={"decision": decision_id, "matched_rules": rules},
            row_id=step["id"],
        )

    def _route_case(self, step, definition: ProcessDefinition, config: dict) -> None:
        """Internal action ``route`` - a deciding gateway (ADR-0056): the
        first branch whose conditions hold, else the default. The result
        keeps every condition with the value it saw, so a run shows why a
        branch was (not) taken; the audit gets names and booleans only."""
        context = self._decision_context(step["case_id"], config)
        evaluated, taken = evaluate_routes(config, context)
        self.engine.complete_step(
            step["case_id"],
            step["step_id"],
            taken,
            result={"route": taken, "evaluated": evaluated},
            actor_type="system",
            actor_id="gateway",
            audit_extra={
                "route": taken,
                "branches": {e["outcome"]: e["result"] for e in evaluated},
            },
            row_id=step["id"],
        )

    def _decision_context(self, case_id: str, config: dict) -> dict:
        trigger, completed = self.engine.step_outputs(case_id)
        steps: dict = {}
        for sid, entry in completed.items():
            result = entry["result"]
            if isinstance(result, dict):
                # a step's outcome is readable next to its result (a gate's
                # decision in a later record or mail); a result field of
                # that name wins. ``result`` is there too, so a path reads
                # the same as in contracts (steps.x.result.y).
                steps[sid] = {"outcome": entry["outcome"], "result": result, **result}
            elif isinstance(result, list):
                steps[sid] = result
        if trigger.get("source") in ("chain", "call"):
            # Chained and called cases: the predecessor's outputs count as steps too.
            for sid, result in (trigger.get("outputs") or {}).items():
                if isinstance(result, dict):
                    steps.setdefault(sid, result)
        context: dict = {"trigger": effective_trigger(trigger), "steps": steps}
        for sid in (config.get("context") or {}).get("records") or []:
            if sid in self.records:
                context.setdefault("records", {})[sid] = self.record_values(sid)
        return context

    def _fail_action(self, step, step_def, reason: str) -> None:
        self.diag(
            "error", "action", "action_failed", reason,
            case_id=step["case_id"], step_id=step["step_id"],
        )
        # A tool names its error path (``on_error``); plain actions use
        # ``failed``. Without such an outcome the step stays pending.
        outcome = str(step_def.config.get("on_error") or "failed")
        if outcome in step_def.next:
            self.engine.complete_step(
                step["case_id"],
                step["step_id"],
                outcome,
                result={"error": reason},
                actor_type="system",
                actor_id="runtime",
                row_id=step["id"],
            )
        else:
            log.error("action %s failed, left pending: %s", step["step_id"], reason)

    def _dispatch_case(self, step) -> None:
        """Deterministic routing (docs/AGENTIK.md): look up the recognized
        or manually assigned document type and start its declared chain.
        No model is involved here - the judgment already happened in the
        classifier or at the categorization gate."""
        case_id = step["case_id"]
        category = self._dispatch_category(case_id)
        doctype = self.doctypes.get(category) if category else None
        if doctype is None or doctype.chain not in self.engine.definitions:
            self.engine.complete_step(
                case_id,
                step["step_id"],
                "failed",
                result={"error": f"no routable document type (category={category!r})"},
                actor_type="system",
                actor_id="dispatch-action",
            )
            log.error("case %s not dispatchable (category=%r)", case_id, category)
            self.diag(
                "error", "dispatch", "not_dispatchable",
                f"category={category!r}", case_id=case_id,
            )
            return
        self.engine.complete_step(
            case_id,
            step["step_id"],
            "done",
            result={"category": doctype.id, "chain": doctype.chain},
            actor_type="system",
            actor_id="dispatch-action",
        )
        new_case = self.engine.start_successor(case_id, doctype.chain, actor_id="dispatch-action")
        log.info(
            "case %s dispatched as %r to %s (%s)", case_id, doctype.id, doctype.chain, new_case
        )

    def _forward_case(self, step) -> None:
        """Deliver the case's files to the assigned addressee's channel.

        Forwarding has outside effect (the document leaves this
        instance's data directory), so the engine demands a passed gate
        or a valid standing mandate (ADR-0016). "Weiterleiten ohne
        behalten" (docs/VERMITTLER.md): the files are MOVED, not copied -
        the Vermittler keeps only delivery metadata in case and audit.
        """
        case_id = step["case_id"]
        assignment = self.forward_assignment(case_id)
        wanted = assignment.get("addressee", "")
        ids = list(wanted) if isinstance(wanted, list) else [wanted]
        targets = [self.addressees.get(i) for i in ids]
        if any(t is not None and not self.path_allowed(t.path) for t in targets):
            targets = [None]  # a shared host: nothing is delivered outside this instance
        if not ids or any(t is None for t in targets):
            self.engine.complete_step(
                case_id,
                step["step_id"],
                "failed",
                result={"error": f"no configured addressee (got {wanted!r})"},
                actor_type="system",
                actor_id="forward-action",
            )
            log.error("case %s not deliverable: %r", case_id, wanted)
            self.diag(
                "error", "forward", "not_deliverable", f"addressee={wanted!r}", case_id=case_id
            )
            return
        # "Betrifft mehrere" (ADR-0024): a document concerning several
        # mandants is COPIED to all but the last target and MOVED to the
        # last - the Vermittler still keeps nothing. Each copy carries a
        # provenance note naming its siblings, so no side can lose track.
        delivered: list[str] = []
        provenance = {
            "source_case": case_id,
            "delivered_to": [t.id for t in targets],
            "delivered_at": _iso(datetime.now(UTC)),
        }
        files = [f for f in self._case_files(case_id) if f.is_file()]
        for index, target in enumerate(targets):
            target_dir = Path(target.path)
            target_dir.mkdir(parents=True, exist_ok=True)
            last = index == len(targets) - 1
            for source in files:
                if last:
                    shutil.move(str(source), target_dir / source.name)
                else:
                    shutil.copy2(str(source), target_dir / source.name)
                if index == 0:
                    delivered.append(source.name)
            if len(targets) > 1:
                for source in files:
                    note = target_dir / _provenance_name(source)
                    payload = {**provenance, "files": delivered}
                    note.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
        self.engine.complete_step(
            case_id,
            step["step_id"],
            "done",
            result={
                "delivered_to": targets[0].id if len(targets) == 1 else [t.id for t in targets],
                "files": delivered,
            },
            actor_type="system",
            actor_id="forward-action",
            mandate_id=assignment.get("mandate"),
        )
        log.info(
            "case %s delivered to %s (%d file(s))",
            case_id, ", ".join(t.id for t in targets), len(delivered),
        )

    def forward_assignment(self, case_id: str) -> dict:
        """The addressee decision to deliver by: the most recent completed
        step whose result carries an ``addressee`` - the gate decision if a
        human assigned, otherwise the mandate rule's result (which also
        names the legitimizing mandate)."""
        rows = self.conn.execute(
            "SELECT result FROM steps WHERE case_id = ? AND status = 'completed'"
            " ORDER BY started_at DESC",
            (case_id,),
        ).fetchall()
        for row in rows:
            if not row["result"]:
                continue
            data = json.loads(row["result"])
            if isinstance(data, dict) and data.get("addressee"):
                return data
        return {}

    def _dispatch_category(self, case_id: str) -> str | None:
        """The category to route by: the most recent completed step whose
        result carries a ``category`` - the categorization gate if a human
        decided, otherwise the classifier's recognized result."""
        rows = self.conn.execute(
            "SELECT result FROM steps WHERE case_id = ? AND status = 'completed'"
            " ORDER BY started_at DESC",
            (case_id,),
        ).fetchall()
        for row in rows:
            if not row["result"]:
                continue
            data = json.loads(row["result"])
            if isinstance(data, dict) and data.get("category"):
                return str(data["category"])
        return None

    # -- client-executed actions ------------------
    #
    # A SystemAction with ``exec = "client"`` is performed by an external
    # actuator (e.g. a browser extension in the user's authenticated
    # session) instead of the runtime: the step stays pending while a
    # task travels the queue open -> claimed -> done|error. ADR-0007 is
    # untouched - completing the step goes through the same engine guard,
    # so an external action still needs a passed gate (or a standing
    # mandate on a mandate-declared step). The actuator is exchangeable:
    # a later MCP connector replaces only the claimer, never the queue.

    def _run_client_action(self, step, definition: ProcessDefinition, config: dict) -> None:
        case_id, step_id = step["case_id"], step["step_id"]
        row = self.conn.execute(
            "SELECT * FROM client_tasks WHERE case_id = ? AND step_id = ? AND created_at >= ?"
            " ORDER BY created_at DESC LIMIT 1",
            (case_id, step_id, step["started_at"] or ""),
        ).fetchone()  # a task from an earlier pass through this step does not count
        now = datetime.now(UTC)
        if row is None:
            params = dict(config.get("params", {}))
            tool = self._tool_declaration(case_id, config)
            if tool is not None:
                # A declared client tool (ADR-0041 §1): the task carries the
                # resolved inputs and the instruction for the actuator.
                try:
                    params["inputs"] = self._resolve_tool_inputs(case_id, config, step)
                except KeyError as exc:
                    self._fail_action(step, definition.steps[step_id], str(exc))
                    return
                params["tool"] = tool.id
                params["name"] = tool.name
                if tool.client is not None:
                    params["instruction"] = tool.client.instruction
                    params["expects"] = tool.client.expects
                    params["kind"] = tool.client.kind_for(params["inputs"])
                    if tool.client.links:
                        params["links"] = tool.client.links
                params["outputs"] = tool.outputs
            self.conn.execute(
                "INSERT INTO client_tasks (id, case_id, step_id, action, params,"
                " status, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (
                    uuid.uuid4().hex,
                    case_id,
                    step_id,
                    str(config.get("action", "")),
                    json.dumps(params, ensure_ascii=False, default=str),
                    _iso(now),
                ),
            )
            log.info("client task queued for case %s step %s", case_id, step_id)
            return
        if row["status"] == "claimed" and row["claimed_at"]:
            # Stale claim: the actuator died - the task goes back to open
            # so another poll can pick it up.
            claimed = datetime.fromisoformat(row["claimed_at"].replace("Z", "+00:00"))
            age = (now - claimed).total_seconds()
            if age > self.settings.client_task_stale_seconds:
                self.conn.execute(
                    "UPDATE client_tasks SET status = 'open', claimed_by = NULL,"
                    " claimed_at = NULL WHERE id = ? AND status = 'claimed'",
                    (row["id"],),
                )
                self.diag(
                    "warning", "client-task", "stale_claim_reopened",
                    f"claimed {int(age)}s ago by {row['claimed_by']!r}",
                    case_id=case_id, step_id=step_id, task_id=row["id"],
                )
            return
        if row["status"] in ("open", "claimed"):
            return  # waiting for the actuator
        result = json.loads(row["result"]) if row["result"] else None
        if row["status"] == "done":
            self.engine.complete_step(
                case_id,
                step_id,
                "done",
                result=result,
                actor_type="system",
                actor_id=f"client:{row['claimed_by'] or 'unknown'}",
                row_id=step["id"],
            )
            return
        # status == "error": route to the failed path if the template has
        # one; otherwise leave pending and record the failure once.
        on_error = str(config.get("on_error") or "failed")
        if on_error in definition.steps[step_id].next:
            self.engine.complete_step(
                case_id,
                step_id,
                on_error,
                result={"error": (result or {}).get("error", "client task failed")},
                actor_type="system",
                actor_id=f"client:{row['claimed_by'] or 'unknown'}",
                row_id=step["id"],
            )
        else:
            self.diag(
                "error", "client-task", "task_failed_no_failed_path",
                json.dumps(result, ensure_ascii=False, default=str)[:200],
                case_id=case_id, step_id=step_id, task_id=row["id"],
            )

    def _record_row(self, step, definition: ProcessDefinition, config: dict) -> None:
        """Internal action ``record``: one versioned row into a declared
        collection (ADR-0026), fields bound like tool inputs."""
        case_id, step_id = step["case_id"], step["step_id"]
        step_def = definition.steps[step_id]
        schema = self.records.get(str(config.get("collection", "")))
        if schema is None or schema.kind != "collection":
            self._fail_action(step, step_def, f"unknown collection {config.get('collection')!r}")
            return
        try:
            values = self._resolve_tool_inputs(case_id, config, step)
            row = record_store.save_row(
                self.conn, schema, values, actor_id=f"process:{definition.id}"
            )
        except (KeyError, RecordError) as exc:
            self._fail_action(step, step_def, str(exc))
            return
        self.engine.complete_step(
            case_id, step_id, "done",
            result={"collection": schema.id, "row_id": row["row_id"], "version": row["version"]},
            actor_type="system", actor_id="record-action", row_id=step["id"],
        )

    def _make_pdf(self, step, definition: ProcessDefinition, config: dict) -> None:
        """Internal action ``pdf``: a plain text PDF (``title``, ``text``)
        as a file of this case (``media/<case>/<name>.pdf``) - an offer, a
        reminder, minutes. A later ``mail`` attaches it by name
        (``attachments = "steps.<id>.file"``), ``file`` files it."""
        from .document import create_pdf

        case_id, step_id = step["case_id"], step["step_id"]
        step_def = definition.steps[step_id]
        try:
            inputs = self._resolve_tool_inputs(case_id, config, step)
        except KeyError as exc:
            self._fail_action(step, step_def, str(exc))
            return
        text = inputs.get("text", "")
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False, indent=1, default=str)
        if not text.strip():
            self._fail_action(step, step_def, "pdf needs a text")
            return
        name = _safe_name(str(inputs.get("name", "") or inputs.get("title", "") or step_id))
        media = self.settings.files_dir / "media" / case_id
        media.mkdir(parents=True, exist_ok=True)
        out = media / f"{name or step_id}.pdf"
        create_pdf(out, str(inputs.get("title", ""))[:120], text.splitlines())
        self.engine.complete_step(
            case_id, step_id, "done", result={"file": out.name},
            actor_type="system", actor_id="pdf-action", row_id=step["id"],
        )

    def _file_documents(self, step, definition: ProcessDefinition, config: dict) -> None:
        """Internal action ``file``: the case's documents into the filing
        folder of the instance (``filing_dir``) under ``folder`` (segments
        separated by ``/``) and ``name`` (without extension; the original's
        extension stays). Names are cleaned to safe file names; nothing
        leaves the folder, nothing is overwritten (``-2``, ``-3`` …). A
        synced drive folder makes this the business's drive - the instance
        itself never talks to the drive. ``media = "true"`` files what the
        case made itself (a PDF of the ``pdf`` action) instead."""
        case_id, step_id = step["case_id"], step["step_id"]
        step_def = definition.steps[step_id]
        try:
            inputs = self._resolve_tool_inputs(case_id, config, step)
        except KeyError as exc:
            self._fail_action(step, step_def, str(exc))
            return
        segments = [
            _safe_name(part) for part in str(inputs.get("folder", "")).replace("\\", "/").split("/")
        ]
        segments = [s for s in segments if s]
        base_name = _safe_name(str(inputs.get("name", "")))
        sources = [p for p in self._case_files(case_id) if p.is_file()]
        if str(config.get("media", "")).lower() in ("true", "1", "yes"):
            # what the case made itself (a PDF of the ``pdf`` action) - only
            sources = sorted(
                (p for p in (self.settings.files_dir / "media" / case_id).glob("*") if p.is_file()),
                key=lambda p: p.name,
            )
        if not sources:
            self._fail_action(step, step_def, "the case has no document to file")
            return
        root = self.settings.filing_root.resolve()
        target = root.joinpath(*segments) if segments else root
        if not target.resolve().is_relative_to(root):
            self._fail_action(step, step_def, "folder leaves the filing directory")
            return
        target.mkdir(parents=True, exist_ok=True)
        filed: list[str] = []
        for i, source in enumerate(sources):
            stem = base_name or _safe_name(source.stem) or "dokument"
            if len(sources) > 1:
                stem = f"{stem}-{i + 1}"
            out, n = target / f"{stem}{source.suffix.lower()}", 2
            while out.exists():
                out, n = target / f"{stem}-{n}{source.suffix.lower()}", n + 1
            shutil.copy2(source, out)
            filed.append(out.relative_to(root).as_posix())
        self.engine.complete_step(
            case_id, step_id, "done",
            result={"filed": filed, "folder": "/".join(segments)},
            actor_type="system", actor_id="file-action", row_id=step["id"],
        )

    def _send_mail(self, step, definition: ProcessDefinition, config: dict) -> None:
        """Action ``mail`` (ADR-0048): one e-mail from the instance's sender
        to recipients bound from the case - ``to``, ``subject``, ``body``,
        optional ``cc``, ``reply_to``, ``in_reply_to`` (``trigger.message_id``
        answers the mail that started the case) and ``attachments`` (names
        of the case's own files, or ``case_files = "true"`` for all of
        them). Outside effect, so only after a gate; under test/shadow the
        configuration stubs it before this point. Recipients are checked
        against the operator's ``mail_recipients`` allowlist."""
        case_id, step_id = step["case_id"], step["step_id"]
        step_def = definition.steps[step_id]
        cfg = self.notify_config
        if self.outbound_mailer is None and not (cfg.get("smtp_host") and cfg.get("smtp_from")):
            self._fail_action(step, step_def, "no SMTP sender configured (Verwaltung → Kanäle)")
            return
        try:
            inputs = self._resolve_tool_inputs(case_id, config, step)
            to = outbound.addresses(inputs.get("to", ""))
            cc = outbound.addresses(inputs.get("cc", "")) if inputs.get("cc") else ()
            reply_to = ""
            if inputs.get("reply_to"):
                reply_to = outbound.addresses(inputs.get("reply_to", ""))[0]
            refused = outbound.recipients_allowed(to + cc, self.settings.mail_recipients)
            if refused:
                raise outbound.MailError(f"recipient {refused} is outside mail_recipients")
            body = inputs.get("body", "")
            if not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False, indent=1, default=str)
            attachments = self._mail_attachments(case_id, config, inputs)
            mail = outbound.OutgoingMail(
                to=to, subject=str(inputs.get("subject", "")), body=body, cc=cc, reply_to=reply_to,
                in_reply_to=str(inputs.get("in_reply_to") or ""), attachments=attachments,
            )
            message = outbound.build_message(
                str(cfg.get("smtp_from") or "aitmation@localhost"), mail,
                max_attachment_bytes=self.settings.mail_max_attachment_mb * 1024 * 1024,
            )
            if self.outbound_mailer is not None:
                self.outbound_mailer(message)
            else:
                with self._unlocked():
                    outbound.smtp_deliver(
                        str(cfg.get("smtp_host")), int(cfg.get("smtp_port") or 587),
                        str(cfg.get("smtp_security") or "starttls"),
                        str(cfg.get("smtp_user") or ""),
                        resolve_secret(self.settings.data_dir, "smtp_password"), message,
                    )
        except (KeyError, outbound.MailError) as exc:
            self._fail_action(step, step_def, str(exc))
            return
        self.engine.complete_step(
            case_id, step_id, "done",
            result={"to": list(mail.to), "cc": list(mail.cc), "subject": message["Subject"],
                    "attachments": [p.name for p in mail.attachments]},
            actor_type="system", actor_id="mail-action", row_id=step["id"],
        )

    def _mail_attachments(self, case_id: str, config: dict, inputs: dict) -> tuple[Path, ...]:
        """Only the case's own files (trigger files and media of this case):
        by name from ``attachments``, or all of them with ``case_files``."""
        own = {p.name: p for p in self._case_files(case_id) if p.is_file()}
        media = self.settings.files_dir / "media" / case_id
        if media.is_dir():
            own.update({p.name: p for p in media.iterdir() if p.is_file()})
        if str(config.get("case_files", "")).lower() in ("true", "1", "yes"):
            return tuple(sorted(own.values(), key=lambda p: p.name))
        wanted = inputs.get("attachments")
        if not wanted:
            return ()
        names = [wanted] if isinstance(wanted, str) else [str(w) for w in wanted]
        out: list[Path] = []
        for name in names:
            name = Path(name).name
            if name not in own:
                raise outbound.MailError(f"attachment {name!r} is not a file of this case")
            out.append(own[name])
        return tuple(out)

    def _figures(self, step, definition: ProcessDefinition, config: dict) -> None:
        """Internal action ``figures``: the instance's own operating figures
        of the last ``days`` (cases per process, gate decisions, errors by
        event, model calls, tokens and cost) as the step's result - what a
        retrospective looks at. Reads only our own trails, gate-free."""
        from . import metrics, operations

        case_id, step_id = step["case_id"], step["step_id"]
        days = int(config.get("days", 7))
        now = self.clock() if self.clock else datetime.now(UTC)
        since = _iso(now - timedelta(days=days))
        approving = {
            (d.id, st.id): st.approving_outcomes()
            for d in self.engine.definitions.values()
            for st in d.steps.values()
            if st.kind is StepKind.HUMAN_GATE
        }
        llm = metrics.llm_figures(self.conn, since)
        errors = operations.errors(self.conn, since, limit=10)
        result = {
            "days": days,
            "since": since,
            "cases": metrics.case_figures(self.conn, since),
            "gates": metrics.gate_figures(self.conn, since, approving),
            "errors": {
                "errors": errors["errors"],
                "warnings": errors["warnings"],
                "by_event": errors["by_event"],
            },
            "models": {
                k: llm[k] for k in ("calls", "errors", "tokens_in", "tokens_out", "cost_chf")
            },
            "per_process": llm["per_template"],
        }
        self.engine.complete_step(
            case_id, step_id, "done", result=result,
            actor_type="system", actor_id="figures-action", row_id=step["id"],
        )

    def _extract_links(self, step, definition: ProcessDefinition, config: dict) -> None:
        """Internal action ``links``: the web addresses in a text - the
        bound ``inputs.text`` or, without one, the case's document(s) -
        as ``urls`` (deduplicated, in order, ``max`` default 5) plus the
        text without them as ``note``. Deterministic, no model."""
        case_id, step_id = step["case_id"], step["step_id"]
        step_def = definition.steps[step_id]
        text = ""
        if config.get("inputs"):
            try:
                text = str(self._resolve_tool_inputs(case_id, config, step).get("text") or "")
            except KeyError:
                text = ""  # no note in this start (the inbox way in): the document has it
        if not text.strip():
            text = self._document_text(case_id)
        limit = int(config.get("max", 5))
        urls: list[str] = []
        for match in _URL.finditer(text):
            url = match.group(0).rstrip(".,;:)]}>'\"")
            if url not in urls:
                urls.append(url)
        note = _URL.sub("", text)
        note = " ".join(note.split())[:2000]
        result = {"urls": urls[:limit], "count": len(urls), "note": note}
        if not urls:
            self._fail_action(step, step_def, "no web address in the text")
            return
        self.engine.complete_step(
            case_id, step_id, "done", result=result,
            actor_type="system", actor_id="links-action", row_id=step["id"],
        )

    def _document_text(self, case_id: str, max_chars: int = 20000) -> str:
        """The plain text of the case's document(s), for deterministic
        actions (no OCR escalation here - the text layer or the cache)."""
        row = self.conn.execute(
            "SELECT result FROM steps WHERE case_id = ? AND kind = 'trigger'", (case_id,)
        ).fetchone()
        trigger = json.loads(row["result"]) if row and row["result"] else {}
        parts: list[str] = []
        for candidate in _origin_files(origin_trigger(trigger)):
            try:
                parts.append(extract_text(candidate).text)
            except DocumentError:
                continue
        for key in ("text", "note", "body"):
            if isinstance(trigger.get(key), str):
                parts.append(trigger[key])
        return "\n".join(parts)[:max_chars]

    def _read_rows(self, step, definition: ProcessDefinition, config: dict) -> None:
        """Internal action ``rows``: the current rows of a collection as the
        step's result (``items`` = list of field tables), so a later step
        can run ``for_each`` over them - "measure every post of the week".
        ``where`` = field -> path narrows, ``limit`` caps (default 50)."""
        case_id, step_id = step["case_id"], step["step_id"]
        step_def = definition.steps[step_id]
        schema = self.records.get(str(config.get("collection", "")))
        if schema is None or schema.kind != "collection":
            self._fail_action(step, step_def, f"unknown collection {config.get('collection')!r}")
            return
        try:
            where = config.get("where") or {}
            filters = (
                self._resolve_tool_inputs(case_id, {**config, "inputs": where}, step)
                if where else {}
            )
        except KeyError as exc:
            self._fail_action(step, step_def, str(exc))
            return
        limit = int(config.get("limit", 50))
        items = []
        for row in record_store.list_rows(self.conn, schema, limit=5000):
            values = row["values"]
            if all(str(values.get(k, "")) == str(v) for k, v in filters.items()):
                items.append({**values, "row_id": row["row_id"], "saved_at": row["saved_at"]})
            if len(items) >= limit:
                break
        self.engine.complete_step(
            case_id, step_id, "done",
            result={"collection": schema.id, "count": len(items), "items": items},
            actor_type="system", actor_id="rows-action", row_id=step["id"],
        )

    def _file_knowledge(self, step, definition: ProcessDefinition, config: dict) -> None:
        """Internal action ``knowledge``: a text (a step's output, say) becomes
        a document in a knowledge space of our own (ADR-0031) - the way a
        pipeline feeds what it learned back to later steps."""
        case_id, step_id = step["case_id"], step["step_id"]
        step_def = definition.steps[step_id]
        space_id = str(config.get("space", ""))
        if self.knowledge.space(space_id) is None:
            self._fail_action(step, step_def, f"unknown knowledge space {space_id!r}")
            return
        try:
            inputs = self._resolve_tool_inputs(case_id, config, step)
        except KeyError as exc:
            self._fail_action(step, step_def, str(exc))
            return
        text = inputs["text"]
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False, indent=1, default=str)
        stamp = self.clock().strftime("%Y-%m-%d") if self.clock else "note"
        default_name = f"{definition.id}-{stamp}-{case_id[:8]}.md"
        name = _safe_file_name(str(inputs.get("name") or default_name))
        if not name.endswith((".md", ".txt")):
            name += ".md"
        try:
            doc = self.knowledge.add_document(
                space_id, name, text.encode("utf-8"),
                title=str(inputs.get("title") or ""), actor=f"process:{definition.id}",
            )
        except KnowledgeError as exc:
            self._fail_action(step, step_def, str(exc))
            return
        self.engine.complete_step(
            case_id, step_id, "done",
            result={"space": space_id, "document": doc.get("id"), "name": name},
            actor_type="system", actor_id="knowledge-action", row_id=step["id"],
        )

    def _tool_declaration(self, case_id: str, config: dict):
        tool_id = str(config.get("tool") or "")
        if not tool_id:
            return None
        app_id = self.template_app.get(self._template_of(case_id))
        app = self.apps.get(app_id or "")
        if app is None:
            return None
        return app.tools.get(tool_id)

    def _resolve_tool_inputs(self, case_id: str, config: dict, step_row=None) -> dict:
        """The step's bound inputs (ADR-0041 §2) from the case context:
        ``steps.<id>.<field>``, ``trigger.<field>``, ``const:<name>``,
        ``literal:<text>``, ``template:<text>`` (a text with ``{path}``
        placeholders resolved from the same context - the mail body a
        process composes without a model; ``template:const:<name>`` takes
        the text from a constant first) or, in a multi-instance step,
        ``each`` / ``each.<field>`` for the current element. A path that
        resolves to nothing is an error - a tool never runs on a silently
        missing value."""
        from .engine import _lookup_path

        context = self._decision_context(case_id, config)
        context["case"] = {"id": case_id, "process": self._template_of(case_id)}
        if step_row is not None:
            try:
                raw = step_row["instance_input"]
            except (KeyError, IndexError, TypeError):
                raw = None
            if raw:
                context["each"] = json.loads(raw) if isinstance(raw, str) else raw
        app_id = self.template_app.get(self._template_of(case_id))
        constants = self.apps[app_id].constants if app_id in self.apps else {}
        def resolve(path: str):
            if path.startswith("const:"):
                return _lookup_path(constants, path[len("const:"):])
            if path == "each":
                return context.get("each")
            return _lookup_path(context, path)

        out: dict = {}
        for name, path in (config.get("inputs") or {}).items():
            path = str(path)
            if path.startswith("literal:"):
                out[name] = path[len("literal:"):]
                continue
            if path.startswith("template:"):
                text = path[len("template:"):]
                if text.startswith("const:"):
                    found = resolve(text)
                    if found is None:
                        raise KeyError(f"input {name!r}: nothing at {text!r}")
                    text = str(found)
                out[name] = self._fill_template(name, text, resolve)
                continue
            value = resolve(path)
            if value is None:
                raise KeyError(f"input {name!r}: nothing at {path!r}")
            out[name] = value
        return out

    @staticmethod
    def _fill_template(name: str, text: str, resolve) -> str:
        """``{path}`` placeholders from the case context; a missing one is
        the same error as a missing bound input."""
        def fill(match: re.Match) -> str:
            path = match.group(1).strip()
            value = resolve(path)
            if value is None:
                raise KeyError(f"input {name!r}: nothing at {path!r}")
            return str(value)

        return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_.:]*)\}", fill, text)

    def _run_connector(self, step, definition: ProcessDefinition, config: dict) -> None:
        """A declared HTTP tool, live (ADR-0041 §1/§3). Stub and log modes
        were handled before this point by the configuration."""
        from .connectors import call_http

        case_id, step_id = step["case_id"], step["step_id"]
        step_def = definition.steps[step_id]
        tool = self._tool_declaration(case_id, config)
        if tool is None or (
            tool.http is None and tool.fetch is None and tool.search is None and tool.mcp is None
        ):
            self._fail_action(
                step, step_def, f"tool {config.get('tool')!r} has no connector binding"
            )
            return
        try:
            inputs = self._resolve_tool_inputs(case_id, config, step)
        except KeyError as exc:
            self._fail_action(step, step_def, str(exc))
            return
        app_id = self.template_app.get(self._template_of(case_id))
        if tool.fetch is not None or tool.search is not None:
            capped = self._read_cap_violation(case_id)
            if capped:
                self._fail_action(step, step_def, capped)
                return
        if tool.fetch is not None:
            from .connectors import fetch_json, fetch_page

            reader = fetch_json if tool.fetch.format == "json" else fetch_page
            with self._unlocked():
                outcome = reader(
                    tool.fetch, inputs, transport=self._http_transport,
                    allow_private=self.settings.tools_allow_private_hosts,
                )
            self._finish_connector(step, step_def, tool, config, inputs, outcome, "fetch")
            return
        if tool.mcp is not None:
            with self._unlocked():
                outcome = self._call_mcp(tool, inputs, app_id)
            self._finish_connector(step, step_def, tool, config, inputs, outcome, "mcp")
            return
        if tool.search is not None:
            from .connectors import search_web

            with self._unlocked():
                outcome = search_web(
                    tool.search, inputs,
                    lambda name: (
                        resolve_secret(self.settings.data_dir, name)
                        if is_tool_secret(name) else None
                    ),
                    transport=self._http_transport,
                    allow_private=self.settings.tools_allow_private_hosts,
                )
            self._finish_connector(step, step_def, tool, config, inputs, outcome, "search")
            return
        with self._unlocked():  # the web layer keeps answering during the call
            outcome = call_http(
                tool.http, inputs,
                lambda name: (
                    resolve_secret(self.settings.data_dir, name) if is_tool_secret(name) else None
                ),
                constants=self.apps[app_id].constants if app_id in self.apps else {},
                transport=self._http_transport,
                allow_private=self.settings.tools_allow_private_hosts,
                files_root=self.settings.files_dir,
                files_scope=self.settings.files_dir / "media" / case_id,
            )
        self._finish_connector(step, step_def, tool, config, inputs, outcome, "http")

    def _call_mcp(self, tool, inputs: dict, app_id: str | None):
        """A tool of an MCP server (ADR-0061): the instance's server entry,
        its allowlist, the arguments rendered from the bound inputs."""
        from . import mcp_client, mcp_servers
        from .connectors import ConnectorError, HttpOutcome, _bounded, _lookup, _render

        spec = tool.mcp
        try:
            servers = mcp_servers.load(self.settings.data_dir)
        except mcp_servers.McpConfigError as exc:
            return HttpOutcome(False, None, {}, f"MCP configuration: {exc}")
        server = servers.get(spec.server)
        if server is None:
            return HttpOutcome(
                False, None, {}, f"MCP server {spec.server!r} is not configured in this instance"
            )
        constants = self.apps[app_id].constants if app_id in self.apps else {}
        try:
            arguments = _render(spec.arguments, inputs, None, constants)
            answer = mcp_client.call_tool(
                server, spec.tool, arguments,
                lambda name: (
                    resolve_secret(self.settings.data_dir, name) if is_tool_secret(name) else None
                ),
                transport=self._http_transport,
                allow_private=self.settings.tools_allow_private_hosts,
                allow_stdio=self.settings.mcp_allow_stdio,
                timeout=min(spec.timeout_seconds, server.timeout_seconds),
            )
        except (mcp_client.McpError, ConnectorError) as exc:
            return HttpOutcome(False, None, {}, f"MCP {spec.server}/{spec.tool}: {exc}")
        if answer["is_error"]:
            return HttpOutcome(False, None, {}, f"MCP {spec.tool} reported an error",
                               answer["text"][:500])
        outputs = {"text": answer["text"], "data": _bounded(answer["data"])}
        for name, path in spec.response.items():
            outputs[name] = _bounded(_lookup(answer["data"], path))
        return HttpOutcome(True, 200, outputs, "")

    def _finish_connector(self, step, step_def, tool, config, inputs, outcome, kind) -> None:
        case_id, step_id = step["case_id"], step["step_id"]
        result = {
            "tool": tool.id,
            "configuration": self.configuration.name,
            "tool_mode": "live",
            "status": outcome.status,
            "inputs": inputs,
            **({"outputs": outcome.outputs} if outcome.ok else {"error": outcome.error}),
        }
        if kind == "fetch" and outcome.ok:
            # Foreign content: the guardrails look at it like at a document
            # (ADR-0038) - a page that tells the model what to do gets flagged.
            text = outcome.outputs.get("text", "")
            if tool.fetch is not None and tool.fetch.format == "json":
                # ADR-0059: every value a JSON answer brought is foreign.
                text = json.dumps(outcome.outputs, ensure_ascii=False)
            self._flag_foreign_text(case_id, step_id, f"fetch.{tool.id}", text)
        if kind == "mcp" and outcome.ok:
            # whatever a server answers is foreign content (ADR-0038/0061)
            self._flag_foreign_text(
                case_id, step_id, f"mcp.{tool.id}",
                json.dumps(outcome.outputs, ensure_ascii=False)[:200_000],
            )
        if kind == "search" and outcome.ok:
            self._flag_foreign_text(
                case_id, step_id, f"search.{tool.id}",
                "\n".join(
                    f"{i.get('title', '')} {i.get('snippet', '')}"
                    for i in outcome.outputs.get("items", [])
                ),
            )
        if outcome.body_excerpt and not outcome.ok:
            result["answer"] = outcome.body_excerpt[:500]
        on_error = str(config.get("on_error") or tool.on_error)
        if outcome.ok:
            done = "done"
            # A search with no usable hit takes ``empty`` when the process
            # offers it - a multi-instance over nothing would fail loudly.
            if (
                (kind == "search" or (tool.fetch is not None and tool.fetch.items))
                and not outcome.outputs.get("items") and "empty" in step_def.next
            ):
                done = "empty"
            self.engine.complete_step(
                case_id, step_id, done, result=result, actor_type="system",
                actor_id=f"{kind}:{tool.id}", row_id=step["id"],
                audit_extra={"configuration": self.configuration.name, "tool_mode": "live"},
            )
            return
        self.diag(
            "error", "connector", "http_failed", f"{tool.id}: {outcome.error}",
            case_id=case_id, step_id=step_id,
        )
        if on_error in step_def.next:
            self.engine.complete_step(
                case_id, step_id, on_error, result=result, actor_type="system",
                actor_id=f"{kind}:{tool.id}", row_id=step["id"],
            )
        else:
            log.error("http tool %s failed and step %s has no %r path", tool.id, step_id, on_error)

    def fulfil_client_task(
        self, task_id: str, actor_id: str, *, data: dict | None = None,
        file_name: str | None = None, content: bytes | None = None,
    ) -> dict:
        """A person (or an actuator in one go) delivers what the task asked
        for (ADR-0041 §4): a file into the case store and/or data. The
        task is claimed and finished together; the next tick continues."""
        row = self.conn.execute("SELECT * FROM client_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise EngineError(f"unknown client task {task_id!r}")
        if row["status"] not in ("open", "claimed"):
            raise EngineError(f"client task {task_id!r} is {row['status']}")
        if not self._client_task_live(row):
            raise EngineError(f"client task {task_id!r} belongs to a step that no longer waits")
        params = json.loads(row["params"]) if row["params"] else {}
        declared = params.get("outputs")
        if isinstance(declared, dict) and data:
            unknown = sorted(set(data) - set(declared))
            if unknown:
                raise EngineError(f"this task has no output(s) {unknown}")
        result: dict = dict(data or {})
        if content is not None:
            safe = _safe_file_name(file_name or "delivery.bin")
            target = self.settings.files_dir / "media" / row["case_id"]
            target.mkdir(parents=True, exist_ok=True)
            path = target / safe
            # Relative to the files directory: the step result (and so the
            # audit and the gate card) never shows where the instance keeps
            # its data; the connector resolves it against the same root.
            result["file"] = str(path.relative_to(self.settings.files_dir).as_posix())
            result["file_name"] = safe
            result["bytes"] = len(content)
        elif params.get("expects") == "file":
            raise EngineError("this task expects a file")
        result["fulfilled_by"] = actor_id
        with self.lock:
            cursor = self.conn.execute(
                "UPDATE client_tasks SET status = 'done', claimed_by = ?, claimed_at = ?,"
                " result = ?, finished_at = ? WHERE id = ? AND status IN ('open', 'claimed')",
                (
                    actor_id, _iso(datetime.now(UTC)),
                    json.dumps(result, ensure_ascii=False, default=str),
                    _iso(datetime.now(UTC)), task_id,
                ),
            )
            self.conn.commit()
            if cursor.rowcount == 0:
                raise EngineError(f"client task {task_id!r} was delivered by someone else")
            if content is not None:
                path.write_bytes(content)  # only the winner writes
        return result

    def _client_task_live(self, row) -> bool:
        """A task is only worth delivering while its step still waits for
        it; one left behind by a failed or cancelled step is an orphan."""
        step = self.conn.execute(
            "SELECT 1 FROM steps WHERE case_id = ? AND step_id = ? AND status = 'pending'"
            " AND COALESCE(started_at, '') <= ?",
            (row["case_id"], row["step_id"], row["created_at"]),
        ).fetchone()
        return step is not None

    def open_client_tasks(self, *, include_claimed: bool = False) -> list[dict]:
        """Open tasks (and, for the inbox, the claimed ones with who holds
        them - so a person sees that an actuator is on it)."""
        statuses = ("open", "claimed") if include_claimed else ("open",)
        rows = self.conn.execute(
            "SELECT id, case_id, step_id, action, params, status, claimed_by, claimed_at,"
            " created_at FROM client_tasks WHERE status IN (?, ?) ORDER BY created_at",
            (statuses[0], statuses[-1]),
        ).fetchall()
        return [
            {**dict(r), "params": json.loads(r["params"]) if r["params"] else {}}
            for r in rows if self._client_task_live(r)
        ]

    def claim_client_task(self, task_id: str, actor_id: str) -> dict:
        """Atomically claim an open task. Raises EngineError when the task
        is unknown or no longer open (the web layer maps that to 409)."""
        with self.lock:
            cursor = self.conn.execute(
                "UPDATE client_tasks SET status = 'claimed', claimed_by = ?, claimed_at = ?"
                " WHERE id = ? AND status = 'open'",
                (actor_id, _iso(datetime.now(UTC)), task_id),
            )
            if cursor.rowcount == 0:
                raise EngineError(f"client task {task_id!r} is not open")
            row = self.conn.execute(
                "SELECT id, case_id, step_id, action, params FROM client_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return {**dict(row), "params": json.loads(row["params"]) if row["params"] else {}}

    def submit_client_task_result(
        self, task_id: str, ok: bool, result: dict | None, actor_id: str
    ) -> None:
        """Record the actuator's result; the next tick completes the step."""
        with self.lock:
            cursor = self.conn.execute(
                "UPDATE client_tasks SET status = ?, result = ?, finished_at = ?"
                " WHERE id = ? AND status = 'claimed' AND claimed_by = ?",
                (
                    "done" if ok else "error",
                    json.dumps(result or {}, ensure_ascii=False, default=str),
                    _iso(datetime.now(UTC)),
                    task_id,
                    actor_id,
                ),
            )
            if cursor.rowcount == 0:
                raise EngineError(
                    f"client task {task_id!r} is not claimed by {actor_id!r}"
                )

    def _archive_case(self, case_id: str) -> Path:
        """Copy the case's source files plus a summary into the archive.

        Writes only inside our own data directory - an INTERNAL_ACTION,
        allowed without a gate (see definition.INTERNAL_ACTIONS).
        """
        target = self.settings.archive_dir / case_id
        target.mkdir(parents=True, exist_ok=True)
        for source in self._case_files(case_id):
            if source.is_file():
                shutil.copy2(source, target / source.name)
        summary = self._export_payload(case_id)
        (target / "vorgang.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    def _case_files(self, case_id: str) -> list[Path]:
        """Files referenced by the case's trigger payload (chain-aware)."""
        row = self.conn.execute(
            "SELECT result FROM steps WHERE case_id = ? AND kind = 'trigger'",
            (case_id,),
        ).fetchone()
        if row is None or not row["result"]:
            return []
        payload = json.loads(row["result"])
        return [Path(c) for c in _origin_files(origin_trigger(payload))]

    def _export_case(self, case_id: str) -> Path:
        payload = self._export_payload(case_id)
        out = self.settings.export_dir / f"{case_id}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def _export_payload(self, case_id: str) -> dict:
        steps = self.conn.execute(
            "SELECT step_id, kind, outcome, result, model_ref, finished_at"
            " FROM steps WHERE case_id = ? AND status = 'completed' ORDER BY started_at",
            (case_id,),
        ).fetchall()
        return {
            "case_id": case_id,
            "steps": [
                {
                    "step": s["step_id"],
                    "kind": s["kind"],
                    "outcome": s["outcome"],
                    "result": json.loads(s["result"]) if s["result"] else None,
                    "model_ref": s["model_ref"],
                    "finished_at": s["finished_at"],
                }
                for s in steps
            ],
        }

    # -- timers ------------------------------------------------------------

    def _fire_schedules(self) -> int:
        """Start cases whose calendar slot has come (trigger source
        "schedule"); a missed slot fires once."""
        count = 0
        now = datetime.now(UTC)
        for definition in self._templates_by_source("schedule"):
            start = definition.start_with_source("schedule")
            try:
                spec = schedule.parse_schedule(start.config, source=definition.id)
            except schedule.ScheduleError as exc:
                self.diag("error", "schedule", "invalid", str(exc), template=definition.id)
                continue
            if not schedule.due(self.conn, definition.id, spec, now):
                continue
            planned = schedule.state(self.conn, definition.id)["next_at"]
            payload = {"source": "schedule", "scheduled_for": planned}
            # The slot's calendar facts travel with the trigger (a decision
            # table may route on them), plus the declared payload fields.
            payload.update(self._calendar_facts(planned))
            payload.update(_declared_payload(start.config))
            if start.config.get("channel"):
                payload.setdefault("channel", start.config["channel"])
            self.engine.start_case(definition.id, payload, actor_id="scheduler")
            schedule.mark_fired(self.conn, definition.id, spec, now)
            count += 1
        return count

    def schedules(self) -> list[dict]:
        out = []
        for definition in self._templates_by_source("schedule"):
            start = definition.start_with_source("schedule")
            try:
                spec = schedule.parse_schedule(start.config, source=definition.id)
            except schedule.ScheduleError as exc:
                out.append({"template_id": definition.id, "error": str(exc)})
                continue
            out.append(
                {
                    "template_id": definition.id,
                    "title": definition.meta.get("title", definition.id),
                    "app": self.template_app.get(definition.id),
                    **spec.describe(),
                    **schedule.state(self.conn, definition.id),
                }
            )
        return out

    def run_now(self, template_id: str, actor_id: str, payload: dict | None = None) -> str:
        """Start a manual or scheduled template right now (a person asked)."""
        trigger = self._manual_trigger(template_id, payload)
        with self.lock:
            case_id = self.engine.start_case(template_id, trigger, actor_id=actor_id)
        return case_id

    def _manual_trigger(self, template_id: str, payload: dict | None) -> dict:
        definition = self.engine.definitions.get(template_id)
        if definition is None:
            raise EngineError(f"unknown template {template_id!r}")
        sources = definition.sources()
        if not sources & {"schedule", "manual"}:
            raise EngineError(
                f"template {template_id!r} is not started by hand ({sorted(sources)})"
            )
        source = "manual" if "manual" in sources else "schedule"
        trigger = {"source": "manual"}
        # Contract defaults (``<aimn:input default=…>``) hold wherever the
        # start leaves a field out - the form, the chat, the API.
        defaults = definition.contract.get("defaults")
        if isinstance(defaults, dict):
            trigger.update(defaults)
        if source == "schedule":
            # "Run now" on a slot: the same calendar facts and declared
            # fields as a scheduled firing, with the moment as the plan.
            now = self.clock() if self.clock else datetime.now(UTC)
            trigger.update(self._calendar_facts(_iso(now)))
            trigger.update(_declared_payload(definition.start_with_source("schedule").config))
        trigger.update(payload or {})
        return trigger

    def run_through(
        self, template_id: str, actor_id: str, payload: dict | None = None, *,
        timeout: float = 30.0, breakpoints: list[str] | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> dict:
        """Start a case and drive it right here, step after step, without
        waiting for the scheduler's tick - until it ends, waits (a gate no
        rule covers, a timer, a device) or ``timeout`` runs out (ADR-0054).

        Same engine, same guard, same audit, same guardrails as a ticked
        case; only the pace differs. The run owns its chain: the tick
        leaves those cases alone meanwhile, and other runs and the tick go
        on in parallel - the lock is let go around every model call as in
        the tick. What is still open afterwards, the tick carries on.

        ``breakpoints`` (step ids) make it a test run: the run stops before
        each of them with status ``paused`` - inputs and outputs so far are
        in the report and the trace - and :meth:`resume_run` goes on.

        ``on_event`` receives the run's live text (ADR-0058): ``{"event":
        "text", "step": …, "delta": …}`` while a streaming step's model
        writes - sentence by sentence, through the same post-guardrails as
        the final answer. Called from the model thread; keep it quick."""
        trigger = self._manual_trigger(template_id, payload)
        with self.lock:
            # start and claim in one step: the tick never sees this chain
            case_id = self.engine.start_case(template_id, trigger, actor_id=actor_id)
            chain = self.engine._case(case_id)["chain_id"] or case_id
            self._sync_chains.add(chain)
            if breakpoints:
                self._paused[chain] = set(breakpoints)
            if on_event is not None:
                self._streams[chain] = on_event
                on_event({"event": "started", "case_id": case_id})
        try:
            return self._drive(case_id, chain, timeout)
        finally:
            if on_event is not None:
                with self.lock:
                    self._streams.pop(chain, None)

    def resume_run(
        self, case_id: str, *, breakpoints: list[str] | None = None, timeout: float = 30.0
    ) -> dict:
        """Continue a run that stopped at a breakpoint: the steps it stopped
        before run now; ``breakpoints`` (default: the remaining ones) are
        where it stops next. An empty list runs to the end."""
        with self.lock:
            case = self.engine._case(case_id)
            chain = case["chain_id"] or case_id
            if chain not in self._paused:
                raise EngineError(f"case {case_id} is not stopped at a breakpoint")
            if chain in self._sync_chains:
                raise EngineError(f"case {case_id} is running")
            waiting_at = {
                r["step_id"] for r in self.conn.execute(
                    "SELECT step_id FROM steps WHERE status = 'pending' AND case_id IN"
                    " (SELECT id FROM cases WHERE COALESCE(chain_id, id) = ?)", (chain,))
            }
            if breakpoints is None:
                self._paused[chain] = self._paused[chain] - waiting_at
            else:
                self._paused[chain] = set(breakpoints)
            self._sync_chains.add(chain)
        return self._drive(case_id, chain, timeout)

    def _drive(self, case_id: str, chain: str, timeout: float) -> dict:
        deadline = time.monotonic() + max(1.0, min(float(timeout), 300.0))
        me = threading.get_ident()
        timed_out = False
        t_start = time.perf_counter()
        since = observability._now_iso()
        lock_wait = 0.0
        try:
            self.lock.acquire()
            lock_wait = (time.perf_counter() - t_start) * 1000
            self.lock.release()
            with self.lock:
                self._runners.add(me)
                self._scope[me] = chain
                try:
                    while True:
                        n = self._run_agents() + self._run_system_actions()
                        n += self._apply_gate_rules()
                        if n == 0:
                            break
                        if time.monotonic() > deadline:
                            timed_out = True
                            break
                finally:
                    self._scope.pop(me, None)
                    self._runners.discard(me)
        finally:
            with self.lock:
                self._sync_chains.discard(chain)
        with self.lock:
            report = self._run_report(case_id, timed_out)
            report["timing"].update({"total_ms": round((time.perf_counter() - t_start) * 1000),
                                     "lock_wait_ms": round(lock_wait)})
            report["usage"] = self._run_usage(chain, since)
            stops = self._paused.get(chain)
            if stops is not None:
                held = [w["step_id"] for w in report["waiting_on"]
                        if w["step_id"] in stops and w["kind"] != "human_gate"]
                if held and not timed_out:
                    # Stopped before a breakpoint: the tick keeps its hands
                    # off this chain until the run is resumed.
                    report["status"] = "paused"
                    report["paused_at"] = held
                else:
                    self._paused.pop(chain, None)
            return report

    # -- trace: what each step got and gave (ADR-0055 / debugging) --------

    def _llm_rows(self, case_ids: list[str]) -> dict[tuple[str, str], list[dict]]:
        if not case_ids:
            return {}
        marks = ",".join("?" * len(case_ids))
        out: dict[tuple[str, str], list[dict]] = {}
        for r in self.conn.execute(
            "SELECT id, at, case_id, step_id, task, input, response, model_ref, tokens_in,"
            " tokens_out, duration_ms, status, error FROM llm_interactions"
            f" WHERE case_id IN ({marks}) ORDER BY at", case_ids,
        ):
            row = dict(r)
            for key in ("input", "response"):
                if row.get(key):
                    with contextlib.suppress(ValueError):
                        row[key] = json.loads(row[key])
            out.setdefault((row["case_id"], row["step_id"] or ""), []).append(row)
        return out

    def case_trace(self, case_id: str) -> dict:
        """Every step of a case's chain in order: status, outcome, result and
        the model calls it made (task text, redacted input, response)."""
        case = self.engine._case(case_id)
        chain = case["chain_id"] or case_id
        cases = [dict(r) for r in self.conn.execute(
            "SELECT id, template_id, status, created_at, closed_at FROM cases"
            " WHERE COALESCE(chain_id, id) = ? ORDER BY created_at", (chain,))]
        ids = [c["id"] for c in cases]
        llm = self._llm_rows(ids)
        marks = ",".join("?" * len(ids))
        steps = []
        for r in self.conn.execute(
            "SELECT case_id, step_id, kind, status, outcome, result, model_ref, started_at,"
            f" finished_at FROM steps WHERE case_id IN ({marks}) ORDER BY started_at, rowid", ids,
        ):
            row = dict(r)
            if row.get("result"):
                with contextlib.suppress(ValueError):
                    row["result"] = json.loads(row["result"])
            row["llm"] = llm.get((row["case_id"], row["step_id"]), [])
            steps.append(row)
        return {"case_id": case_id, "chain": chain, "cases": cases,
                "paused": sorted(self._paused.get(chain, set())), "steps": steps}

    def node_counts(
        self, template_id: str, where: dict[str, str] | None = None, days: int = 30
    ) -> dict[str, dict[str, int]]:
        """How often each step ran in the last days and with which outcome,
        optionally only for cases whose trigger matches ``where``:
        ``{step_id: {"*": runs, outcome: n, ...}}``."""
        where = where or {}
        since = (datetime.now(UTC) - timedelta(days=max(1, min(days, 365)))).isoformat()
        counts: dict[str, dict[str, int]] = {}
        rows = self.conn.execute(
            "SELECT c.id, t.result AS trigger FROM cases c JOIN steps t ON t.case_id = c.id"
            " AND t.kind = 'trigger' WHERE c.template_id = ? AND c.created_at >= ?"
            " ORDER BY c.created_at DESC LIMIT 5000", (template_id, since),
        ).fetchall()
        for r in rows:
            try:
                trigger = json.loads(r["trigger"] or "{}")
            except ValueError:
                continue
            if any(str(trigger.get(k, "")) != str(v) for k, v in where.items()):
                continue
            for step in self.conn.execute(
                "SELECT step_id, status, outcome FROM steps WHERE case_id = ?", (r["id"],)
            ):
                entry = counts.setdefault(step["step_id"], {"*": 0})
                entry["*"] += 1
                if step["status"] == "completed" and step["outcome"]:
                    entry[step["outcome"]] = entry.get(step["outcome"], 0) + 1
        return counts

    def case_runs(
        self, template_id: str, where: dict[str, str] | None = None, limit: int = 20
    ) -> list[dict]:
        """The last cases of a process with the path they took - every step
        in order with status and outcome; the run view of the workshop
        draws it onto the graph (ADR-0056). ``where`` filters by trigger."""
        where = where or {}
        found: list[dict] = []
        rows = self.conn.execute(
            "SELECT c.id, c.status, c.created_at, c.closed_at, t.result AS trigger FROM cases c"
            " JOIN steps t ON t.case_id = c.id AND t.kind = 'trigger' WHERE c.template_id = ?"
            " ORDER BY c.created_at DESC LIMIT 400", (template_id,),
        ).fetchall()
        for r in rows:
            try:
                trigger = json.loads(r["trigger"] or "{}")
            except ValueError:
                continue
            if any(str(trigger.get(k, "")) != str(v) for k, v in where.items()):
                continue
            path = [dict(s) for s in self.conn.execute(
                "SELECT step_id, status, outcome FROM steps WHERE case_id = ?"
                " ORDER BY started_at, rowid", (r["id"],))]
            found.append({"case_id": r["id"], "status": r["status"], "created_at": r["created_at"],
                          "closed_at": r["closed_at"], "trigger": trigger, "path": path,
                          "paused": sorted(self._paused.get(r["id"], set()))})
            if len(found) >= limit:
                break
        return found

    def step_runs(
        self, template_id: str, step_id: str, where: dict[str, str] | None = None, limit: int = 5
    ) -> list[dict]:
        """The last executions of one step of a process - optionally only
        cases whose trigger matches ``where`` (``{"space": "townhall"}``)."""
        where = where or {}
        found: list[dict] = []
        rows = self.conn.execute(
            "SELECT c.id, c.created_at, t.result AS trigger FROM cases c JOIN steps t"
            " ON t.case_id = c.id AND t.kind = 'trigger' WHERE c.template_id = ?"
            " ORDER BY c.created_at DESC LIMIT 400", (template_id,),
        ).fetchall()
        for r in rows:
            try:
                trigger = json.loads(r["trigger"] or "{}")
            except ValueError:
                continue
            if any(str(trigger.get(k, "")) != str(v) for k, v in where.items()):
                continue
            step = self.conn.execute(
                "SELECT status, outcome, result, model_ref, started_at, finished_at FROM steps"
                " WHERE case_id = ? AND step_id = ? ORDER BY started_at DESC LIMIT 1",
                (r["id"], step_id),
            ).fetchone()
            if step is None:
                continue
            row = dict(step)
            with contextlib.suppress(ValueError, TypeError):
                row["result"] = json.loads(row["result"])
            row["case_id"] = r["id"]
            row["trigger"] = trigger
            row["llm"] = self._llm_rows([r["id"]]).get((r["id"], step_id), [])
            found.append(row)
            if len(found) >= limit:
                break
        return found

    def _streamer_for(
        self, case_id: str, step_id: str, config: dict, client, post_names: list[str],
        keep: set[str] | None = None,
    ) -> _AnswerStream | None:
        """A live text feed for this call - only in a run someone listens to
        (``run_through(on_event=…)``), only for a step that names its
        ``stream`` field, only with a client that can stream."""
        field = config.get("stream")
        if not field or not getattr(client, "streams", False) or not self._streams:
            return None
        chain = self.engine._case(case_id)["chain_id"]
        sink = self._streams.get(chain)
        if sink is None:
            return None
        return _AnswerStream(sink, step_id, str(field), post_names, keep)

    def _note_timing(self, case_id: str, step_id: str, key: str, ms: float) -> None:
        if len(self._timings) > 2000:  # nobody asked for these: drop the oldest half
            for old in list(self._timings)[:1000]:
                self._timings.pop(old, None)
        entry = self._timings.setdefault((case_id, step_id), {})
        entry[key] = round(entry.get(key, 0.0) + ms, 1)

    def _run_usage(self, chain: str, since: str) -> dict:
        """What this run (or this stretch of it, after a resume) spent on
        models: calls, tokens and the frozen cost - numbers only. The caller
        that started the run counts them against its own budget."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(l.tokens_in), 0) AS tokens_in,"
            " COALESCE(SUM(l.tokens_out), 0) AS tokens_out, COALESCE(SUM(l.kosten_chf), 0) AS chf"
            " FROM llm_interactions l JOIN cases c ON c.id = l.case_id"
            " WHERE COALESCE(c.chain_id, c.id) = ? AND l.at >= ?", (chain, since)).fetchone()
        return {"calls": int(row["calls"]), "tokens_in": int(row["tokens_in"]),
                "tokens_out": int(row["tokens_out"]), "chf": round(float(row["chf"]), 6)}

    def _run_timing(self, case_id: str) -> list[dict]:
        """Per step: time from becoming due to done, the model calls'
        share and the retrieval's - where a run's seconds went."""
        llm: dict[str, list] = {}
        for r in self.conn.execute(
            "SELECT step_id, duration_ms FROM llm_interactions WHERE case_id = ?", (case_id,)
        ):
            llm.setdefault(r["step_id"] or "", []).append(r["duration_ms"] or 0)
        out = []
        for r in self.conn.execute(
            "SELECT step_id, kind, started_at, finished_at FROM steps WHERE case_id = ?"
            " ORDER BY started_at, rowid", (case_id,)
        ):
            ms = None
            with contextlib.suppress(Exception):
                if r["started_at"] and r["finished_at"]:
                    ms = round((datetime.fromisoformat(r["finished_at"])
                                - datetime.fromisoformat(r["started_at"])).total_seconds() * 1000)
            extra = self._timings.pop((case_id, r["step_id"]), {})
            calls = llm.get(r["step_id"], [])
            out.append({"step_id": r["step_id"], "kind": r["kind"], "ms": ms,
                        "model_ms": sum(calls) if calls else None, "model_calls": len(calls),
                        **extra})
        return out

    def _run_report(self, case_id: str, timed_out: bool) -> dict:
        """What a synchronous run reports: the case's state, what it waits
        for, the end it reached and every step's output."""
        case = self.engine._case(case_id)
        _trigger, outputs = self.engine.step_outputs(case_id)
        waiting = self.conn.execute(
            "SELECT step_id, kind FROM steps WHERE case_id = ? AND status IN ('waiting', 'pending')"
            " ORDER BY started_at",
            (case_id,),
        ).fetchall()
        children = [r["id"] for r in self.conn.execute(
            "SELECT id FROM cases WHERE parent_case_id = ? ORDER BY created_at", (case_id,))]
        status = str(case["status"])
        if status not in ("completed", "failed", "cancelled"):
            status = "running" if timed_out else ("waiting" if waiting else status)
        return {
            "case_id": case_id,
            "status": status,
            "waiting_on": [{"step_id": w["step_id"], "kind": w["kind"]} for w in waiting],
            "outputs": {
                sid: {"outcome": o["outcome"], "result": o["result"]} for sid, o in outputs.items()
            },
            "children": children,
            "timing": {"steps": self._run_timing(case_id)},
        }

    # -- rules from repeated decisions: standing mandates for any gate --------
    #
    # A gate that declares which facts make two decisions "the same"
    # (``aimn:gate rule="trigger.sender, steps.read.amount"``) can be
    # decided by a standing mandate: granted once by a person, exact match
    # on those facts, visible, revocable, in the audit of every decision it
    # makes (ADR-0016, ADR-0054 §3). aitmation proposes such a rule after a
    # person decided the same way three times; it never grants one itself.

    def _gate_facts(self, case_id: str, config: dict) -> dict | None:
        fields = config.get("rule_fields") or []
        if not fields:
            return None
        context = self._decision_context(case_id, {})
        facts = {}
        for path in fields:
            value = _lookup_path(context, path)
            if value is None or isinstance(value, (dict, list)) or len(str(value)) > 200:
                return None  # a rule needs every fact, each a plain value
            facts[path] = value
        return facts

    def _apply_gate_rules(self) -> int:
        """Decide waiting gates a standing mandate covers."""
        chain = self._scope.get(threading.get_ident())
        sql = ("SELECT s.id, s.case_id, s.step_id, c.template_id FROM steps s"
               " JOIN cases c ON c.id = s.case_id"
               " WHERE s.status = 'waiting' AND s.kind = 'human_gate'")
        args: list = []
        if chain is not None:
            sql += " AND COALESCE(c.chain_id, c.id) = ?"
            args.append(chain)
        elif self._sync_chains or self._paused:
            owned = sorted(self._sync_chains | set(self._paused))
            marks = ",".join("?" * len(owned))
            sql += f" AND COALESCE(c.chain_id, c.id) NOT IN ({marks})"
            args.extend(owned)
        count = 0
        for row in self.conn.execute(sql, args).fetchall():
            definition = self.engine.definitions.get(row["template_id"])
            if definition is None or row["step_id"] not in definition.steps:
                continue
            gate = definition.steps[row["step_id"]]
            facts = self._gate_facts(row["case_id"], gate.config)
            if not facts:
                continue
            action = _gate_action(row["template_id"], row["step_id"])
            mandate = self.engine.find_mandate(action, facts)
            if mandate is None or not json.loads(mandate["condition"]):
                continue  # a gate rule always names its facts - no catch-all
            try:
                self.engine.decide_gate_by_mandate(
                    row["case_id"], row["step_id"], mandate["id"], row_id=row["id"]
                )
            except EngineError as exc:
                self.diag("error", "gate", "gate_rule_failed", str(exc),
                          case_id=row["case_id"], step_id=row["step_id"])
                continue
            count += 1
        return count

    def gate_rule_suggestions(self, threshold: int = 3) -> list[dict]:
        """Proposals from repeated hand decisions on gates that declare
        their rule facts: the same facts decided the same way ``threshold``
        times, no rule covering it yet."""
        rows = self.conn.execute(
            "SELECT s.case_id, s.step_id, s.outcome, c.template_id FROM steps s"
            " JOIN cases c ON c.id = s.case_id"
            " WHERE s.kind = 'human_gate' AND s.status = 'completed'"
        ).fetchall()
        human = {
            (a["case_id"], a["step_id"])
            for a in self.conn.execute(
                "SELECT case_id, json_extract(details, '$.step') AS step_id FROM audit_log"
                " WHERE event = 'step_completed' AND actor_type = 'human'"
            )
        }
        counts: dict[tuple, int] = {}
        for row in rows:
            if (row["case_id"], row["step_id"]) not in human:
                continue
            definition = self.engine.definitions.get(row["template_id"])
            if definition is None or row["step_id"] not in definition.steps:
                continue
            facts = self._gate_facts(row["case_id"], definition.steps[row["step_id"]].config)
            if not facts:
                continue
            digest = json.dumps(facts, sort_keys=True, ensure_ascii=False)
            key = (row["template_id"], row["step_id"], row["outcome"], digest)
            counts[key] = counts.get(key, 0) + 1
        out = []
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        for (template_id, step_id, outcome, facts_json), n in ranked:
            if n < threshold:
                continue
            facts = json.loads(facts_json)
            action = _gate_action(template_id, step_id)
            if self.engine.find_mandate(action, facts) is not None:
                continue
            definition = self.engine.definitions[template_id]
            out.append({
                "action": action, "target": outcome, "condition": facts, "count": n,
                "template_id": template_id, "step_id": step_id,
                "title": definition.meta.get("title", template_id),
                "reason": f"{n}x gleich entschieden",
            })
        return out

    @staticmethod
    def _calendar_facts(planned) -> dict:
        try:
            local = datetime.fromisoformat(str(planned).replace("Z", "+00:00")).astimezone()
        except (TypeError, ValueError):
            return {}
        return {"weekday": local.isoweekday(), "hour": local.hour}

    def deadlines(self) -> list[dict]:
        """Waiting timers across cases: the follow-ups and deadlines a
        person can see coming, soonest first."""
        rows = self.conn.execute(
            "SELECT s.case_id, s.step_id, s.deadline_at, s.started_at, c.template_id"
            " FROM steps s JOIN cases c ON c.id = s.case_id"
            " WHERE s.status = 'waiting' AND s.kind = 'timer' ORDER BY s.deadline_at"
        ).fetchall()
        out = []
        for row in rows:
            trigger = self.conn.execute(
                "SELECT result FROM steps WHERE case_id = ? AND kind = 'trigger'",
                (row["case_id"],),
            ).fetchone()
            payload = json.loads(trigger["result"]) if trigger and trigger["result"] else {}
            definition = self.engine.definitions.get(row["template_id"])
            out.append(
                {
                    **dict(row),
                    "template_title": definition.meta.get("title", row["template_id"])
                    if definition
                    else row["template_id"],
                    "title": payload.get("title") if isinstance(payload, dict) else None,
                    "note": payload.get("note") if isinstance(payload, dict) else None,
                    "about_case": payload.get("about_case") if isinstance(payload, dict) else None,
                }
            )
        return out

    def _fire_due_timers(self) -> int:
        count = 0
        for timer in self.engine.due_timers():
            self.engine.fire_timer(timer["case_id"], timer["step_id"], row_id=timer["id"])
            count += 1
        return count

    # -- helpers -----------------------------------------------------------

    def _template_of(self, case_id: str) -> str:
        return self.conn.execute(
            "SELECT template_id FROM cases WHERE id = ?", (case_id,)
        ).fetchone()["template_id"]

    def open_gates(self) -> list[dict]:
        """Waiting HumanGates for the approval inbox, oldest first."""
        rows = self.conn.execute(
            "SELECT s.case_id, s.step_id, s.started_at, s.deadline_at,"
            " c.template_id, c.template_version"
            " FROM steps s JOIN cases c ON c.id = s.case_id"
            " WHERE s.status = 'waiting' AND s.kind = 'human_gate'"
            " ORDER BY s.started_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def decide(self, case_id: str, step_id: str, outcome: str, actor_id: str, comment: str = ""):
        with self.lock:
            self.engine.decide_gate(case_id, step_id, outcome, actor_id=actor_id, comment=comment)

    def address(
        self,
        case_id: str,
        step_id: str,
        addressee: str | list[str] | None,
        actor_id: str,
        comment: str = "",
    ) -> None:
        """Human decision at the Vermittler's addressing gate.

        ``addressee=None`` discards the document (outcome ``discarded``);
        a single id or a list of ids ("betrifft mehrere", ADR-0024) is
        recorded and the next tick delivers. ``assigned`` is the gate's
        approving outcome - the assignment IS the approval of the delivery
        (ADR-0007).
        """
        with self.lock:
            if addressee is None:
                self.engine.decide_gate(
                    case_id, step_id, "discarded", actor_id=actor_id, comment=comment
                )
                return
            ids = list(dict.fromkeys(addressee)) if isinstance(addressee, list) else [addressee]
            if not ids:
                raise EngineError("at least one addressee is required")
            for one in ids:
                if one not in self.addressees:
                    raise EngineError(f"unknown addressee {one!r}")
            self.engine.decide_gate(
                case_id,
                step_id,
                "assigned",
                actor_id=actor_id,
                comment=comment,
                extra={"addressee": ids[0] if len(ids) == 1 else ids},
            )

    def categorize(
        self,
        case_id: str,
        step_id: str,
        category: str | None,
        actor_id: str,
        comment: str = "",
    ) -> None:
        """Human decision at the sort-out gate (docs/AGENTIK.md).

        ``category=None`` discards the document (outcome ``discarded``);
        otherwise the chosen category is recorded and the next dispatch
        tick routes the case into that category's chain.
        """
        with self.lock:
            if category is None:
                self.engine.decide_gate(
                    case_id, step_id, "discarded", actor_id=actor_id, comment=comment
                )
                return
            if category not in self.doctypes:
                raise EngineError(f"unknown document type {category!r}")
            self.engine.decide_gate(
                case_id,
                step_id,
                "assigned",
                actor_id=actor_id,
                comment=comment,
                extra={"category": category},
            )
