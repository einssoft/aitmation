"""Runtime configuration.

Hard requirements (core/README.md): configuration comes from environment
variables AND a configuration file, environment variables win; no
hard-wired paths - everything hangs off the data directory.

The file is ``config.toml`` inside the data directory (matching the
DataDir layout in docs/DEPLOYMENT.md); ``AITMATION_CONFIG`` may point
somewhere else. Only known keys are accepted.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

_ENV_PREFIX = "AITMATION_"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    templates_dir: Path
    host: str = "127.0.0.1"
    port: int = 8420
    poll_interval_seconds: float = 2.0
    # Model routing (ADR-0013: the model_ref of every answer is audited).
    # The API key deliberately has no place here or in the file - it is
    # read from AITMATION_ANTHROPIC_API_KEY only (credentials stay out of
    # configuration files and model contexts, ARCHITECTURE §7).
    model_cheap: str = "claude-haiku-4-5"
    model_strong: str = "claude-sonnet-4-5"
    model_confidence_threshold: float = 0.7
    # IMAP intake; the password is read from AITMATION_IMAP_PASSWORD only.
    imap_host: str = ""
    imap_user: str = ""
    imap_folder: str = "INBOX"
    # Content-trail retention (llm_interactions, diagnostic_events);
    # the audit_log is untouched by retention - it is append-only forever.
    log_retention_days: int = 90
    # Seconds after which a claimed client task falls back to open.
    # A browser actuator may need minutes for a clip; a claim older than this
    # goes back to open (the actuator died).
    client_task_stale_seconds: float = 900.0
    # Document type declarations (docs/AGENTIK.md); defaults to
    # <data_dir>/doctypes, override via AITMATION_DOCTYPES_DIR.
    doctypes_dir: Path | None = None
    # Installed app bundles (ADR-0023); defaults to <data_dir>/apps,
    # override via AITMATION_APPS_DIR (development points it at the
    # repository's apps/ catalog source).
    apps_dir: Path | None = None
    # The instance's own record schemas (ADR-0026, local bundle); defaults
    # to <data_dir>/records, override via AITMATION_RECORDS_DIR.
    records_dir: Path | None = None
    # Identity inside a household (ADR-0024). The launcher sets these per
    # instance; a standalone instance is its own single-member household.
    instance_id: str = "default"
    instance_title: str = "aitmation"
    instance_type: str = "company"  # private | company | mailroom
    # Named configuration (ADR-0035 §7): standard | test | shadow | <own>.
    # ``shadow`` logs every external tool call instead of running it;
    # ``test`` additionally replays recorded model answers. Set via
    # AITMATION_CONFIGURATION; the test runner sets it programmatically.
    configuration: str = "standard"
    household_path: Path | None = None  # AITMATION_HOUSEHOLD_FILE
    # Lead language of this installation (ADR-0022); users pick their own in the UI.
    language: str = "de"
    # Notifications (Stufe 1.2): who is told about waiting gates, when a
    # reminder and an escalation follow, and the outgoing mail server. The
    # SMTP password is a secret (AITMATION_SMTP_PASSWORD / secrets.toml).
    notify_to: str = ""  # comma-separated e-mail addresses
    escalate_to: str = ""  # deputies; empty = the same addresses
    remind_after_hours: float = 24.0
    escalate_after_hours: float = 72.0
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_from: str = ""
    smtp_security: str = "starttls"  # starttls | ssl | none
    # Where people open the shell (links in notifications); empty = loopback.
    public_url: str = ""
    # OCR (ADR-0028): the local Tesseract binary (empty = look on PATH),
    # its language pack list, and whether a weak local result may be
    # escalated to a released vision model (data class permitting).
    tesseract_path: str = ""
    ocr_languages: str = "deu+fra+ita+eng"
    ocr_vision_escalation: bool = True
    # Model budget (ADR-0029): CHF per calendar month, 0 = no budget. At
    # 80 % the key figures warn; at 100 % ``budget_stop`` refuses paid
    # model calls until the month turns or the budget is raised.
    monthly_budget_chf: float = 0.0
    budget_stop: bool = False
    # AI security (ADR-0038): the guardrails every model call runs through
    # before and after, whatever the process declares (it may only add);
    # and the ceiling of model calls per case (incl. its chain).
    guardrails_pre: tuple[str, ...] = ("no_secrets", "limit_input", "injection_suspect")
    guardrails_post: tuple[str, ...] = ("no_secrets", "no_external_links", "no_personal_data")
    max_model_calls_per_case: int = 50
    # Read-only outside calls (fetch, search) per case and chain (ADR-0042).
    max_read_calls_per_case: int = 30
    # ADR-0041: HTTP tools reach public https hosts only, unless the operator
    # opens loopback/private addresses (a local service on this machine).
    tools_allow_private_hosts: bool = False
    # ADR-0061: MCP servers that run as a local process (stdio) - the
    # operator's decision, never the admin UI's (it would run programs).
    mcp_allow_stdio: bool = False
    # ADR-0045: pause between two requests of a knowledge crawl (seconds) -
    # a municipality's web server is no load test.
    knowledge_crawl_delay_seconds: float = 0.5
    # ADR-0048: where the ``mail`` action may send to - addresses or
    # ``@domain`` entries; empty = anywhere. A process cannot widen this.
    mail_recipients: tuple[str, ...] = ()
    mail_max_attachment_mb: int = 15
    # Action ``file``: the folder documents are filed into, in a structure
    # the process names (year/area/...). A Google Drive or OneDrive folder
    # synced to this machine makes it the business's drive. Defaults to
    # <data_dir>/filing.
    filing_dir: Path | None = None
    # ADR-0043: which trust levels of solutions this instance loads
    # (local = built here, official/verified/community = signed by the
    # platform), and extra platform public keys (id -> base64 Ed25519).
    trusted_levels: tuple[str, ...] = ("local", "official", "verified")
    trust_keys: dict[str, str] = field(default_factory=dict)
    # The signed catalog of the shop (ADR-0043 §4, Etappe A); empty = no
    # catalog, bundles only from files.
    catalog_url: str = "https://aitmation.com/shop/catalog.json"
    # Transcription (ADR-0030): a local Whisper - whisper.cpp binary plus
    # model file, or the optional extra 'transcription' (faster-whisper);
    # ffmpeg for video and format conversion; whether key frames of a
    # video may go to a permitted vision model.
    whisper_backend: str = "auto"  # auto | cpp | faster | http | off
    whisper_model: str = "small"  # faster-whisper model name (local) or the service's model
    # Backend ``http``: the household's transcription container
    # (deploy/transcribe) or another OpenAI-compatible speech endpoint;
    # the bearer, if any, is the secret ``whisper_token``.
    whisper_url: str = ""
    whisper_cpp_path: str = ""  # whisper-cli binary (empty = look on PATH)
    whisper_cpp_model: str = ""  # path to a GGML model file
    ffmpeg_path: str = ""
    video_frames: bool = True
    # Knowledge spaces (ADR-0031): originals under <data>/knowledge; a
    # local embedding model (optional extra 'knowledge', fastembed) adds
    # semantic search - "off" keeps the lexical index only.
    knowledge_dir: Path | None = None
    embeddings: str = "auto"  # auto | off
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # Built React shell (ui/dist); served under /app when present.
    ui_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.knowledge_dir is None:
            object.__setattr__(self, "knowledge_dir", self.data_dir / "knowledge")
        else:
            object.__setattr__(self, "knowledge_dir", Path(self.knowledge_dir))
        if self.doctypes_dir is None:
            object.__setattr__(self, "doctypes_dir", self.data_dir / "doctypes")
        else:
            object.__setattr__(self, "doctypes_dir", Path(self.doctypes_dir))
        if self.apps_dir is None:
            object.__setattr__(self, "apps_dir", self.data_dir / "apps")
        else:
            object.__setattr__(self, "apps_dir", Path(self.apps_dir))
        if self.records_dir is None:
            object.__setattr__(self, "records_dir", self.data_dir / "records")
        else:
            object.__setattr__(self, "records_dir", Path(self.records_dir))
        if self.household_path is not None:
            object.__setattr__(self, "household_path", Path(self.household_path))
        if self.ui_dir is not None:
            object.__setattr__(self, "ui_dir", Path(self.ui_dir))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aitmation.db"

    @property
    def addressees_path(self) -> Path:
        """Addressee configuration of the Vermittler (docs/VERMITTLER.md)."""
        return self.data_dir / "addressees.toml"

    @property
    def models_path(self) -> Path:
        """Model registry; built-in default applies
        when the file does not exist."""
        return self.data_dir / "models.toml"

    @property
    def inbox_dir(self) -> Path:
        """Watched folder: files dropped here trigger folder-trigger templates."""
        return self.data_dir / "inbox"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "export"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def filing_root(self) -> Path:
        return self.filing_dir or self.data_dir / "filing"

    def ensure_layout(self) -> None:
        dirs = (
            self.data_dir,
            self.inbox_dir,
            self.files_dir,
            self.export_dir,
            self.archive_dir,
            self.doctypes_dir,
            self.apps_dir,
            self.records_dir,
            self.knowledge_dir,
        )
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


def save_config_values(settings: Settings, values: dict[str, object]) -> None:
    """Merge known, non-secret keys into config.toml (written by the UI).

    Secrets never go here - they live in secrets.toml (see secrets.py).
    """
    allowed = {f.name for f in fields(Settings)} - {"data_dir"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"refusing to write unknown keys {sorted(unknown)}")
    path = settings.data_dir / "config.toml"
    current: dict[str, object] = {}
    if path.is_file():
        current = tomllib.loads(path.read_text(encoding="utf-8"))
    current.update(values)
    lines = ["# Managed by the aitmation admin UI. No secrets in this file."]
    for key in sorted(current):
        value = current[key]
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, int | float):
            lines.append(f"{key} = {value}")
        else:
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _as_names(value, default: tuple[str, ...]) -> tuple[str, ...]:
    """A guardrail list from TOML (list) or the environment (comma string)."""
    if value is None:
        return default
    if isinstance(value, str):
        return tuple(v.strip() for v in value.split(",") if v.strip())
    if isinstance(value, list | tuple):
        return tuple(str(v) for v in value)
    return default


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build settings from file and environment; environment wins."""
    env = dict(os.environ if env is None else env)
    data_dir = Path(env.get(f"{_ENV_PREFIX}DATA_DIR", Path.home() / ".aitmation"))

    file_values: dict[str, object] = {}
    config_path = Path(env.get(f"{_ENV_PREFIX}CONFIG", data_dir / "config.toml"))
    if config_path.is_file():
        file_values = tomllib.loads(config_path.read_text(encoding="utf-8"))

    values: dict[str, object] = {
        "data_dir": data_dir,
        "templates_dir": data_dir / "templates",
    }
    known = {f.name for f in fields(Settings)}
    unknown = set(file_values) - known
    if unknown:
        raise ValueError(f"{config_path}: unknown configuration keys {sorted(unknown)}")
    values.update(file_values)

    # Environment overrides, e.g. AITMATION_TEMPLATES_DIR, AITMATION_PORT.
    for f in fields(Settings):
        raw = env.get(_ENV_PREFIX + f.name.upper())
        if raw is not None:
            values[f.name] = raw
    if env.get(f"{_ENV_PREFIX}HOUSEHOLD_FILE"):  # the launcher's name for it
        values["household_path"] = env[f"{_ENV_PREFIX}HOUSEHOLD_FILE"]

    return Settings(
        data_dir=Path(values["data_dir"]),
        templates_dir=Path(values["templates_dir"]),
        host=str(values.get("host", "127.0.0.1")),
        port=int(values.get("port", 8420)),
        poll_interval_seconds=float(values.get("poll_interval_seconds", 2.0)),
        model_cheap=str(values.get("model_cheap", "claude-haiku-4-5")),
        model_strong=str(values.get("model_strong", "claude-sonnet-4-5")),
        model_confidence_threshold=float(values.get("model_confidence_threshold", 0.7)),
        imap_host=str(values.get("imap_host", "")),
        imap_user=str(values.get("imap_user", "")),
        imap_folder=str(values.get("imap_folder", "INBOX")),
        log_retention_days=int(values.get("log_retention_days", 90)),
        client_task_stale_seconds=float(values.get("client_task_stale_seconds", 900.0)),
        doctypes_dir=Path(str(values["doctypes_dir"])) if values.get("doctypes_dir") else None,
        apps_dir=Path(str(values["apps_dir"])) if values.get("apps_dir") else None,
        records_dir=Path(str(values["records_dir"])) if values.get("records_dir") else None,
        instance_id=str(values.get("instance_id", "default")),
        instance_title=str(values.get("instance_title", "aitmation")),
        instance_type=str(values.get("instance_type", "company")),
        configuration=str(values.get("configuration", "standard")),
        household_path=(
            Path(str(values["household_path"])) if values.get("household_path") else None
        ),
        language=str(values.get("language", "de")),
        notify_to=str(values.get("notify_to", "")),
        escalate_to=str(values.get("escalate_to", "")),
        remind_after_hours=float(values.get("remind_after_hours", 24.0)),
        escalate_after_hours=float(values.get("escalate_after_hours", 72.0)),
        smtp_host=str(values.get("smtp_host", "")),
        smtp_port=int(values.get("smtp_port", 587)),
        smtp_user=str(values.get("smtp_user", "")),
        smtp_from=str(values.get("smtp_from", "")),
        smtp_security=str(values.get("smtp_security", "starttls")),
        public_url=str(values.get("public_url", "")),
        tesseract_path=str(values.get("tesseract_path", "")),
        ocr_languages=str(values.get("ocr_languages", "deu+fra+ita+eng")),
        ocr_vision_escalation=_as_bool(values.get("ocr_vision_escalation", True)),
        monthly_budget_chf=float(values.get("monthly_budget_chf", 0.0)),
        budget_stop=_as_bool(values.get("budget_stop", False)),
        guardrails_pre=_as_names(values.get("guardrails_pre"), Settings.guardrails_pre),
        guardrails_post=_as_names(values.get("guardrails_post"), Settings.guardrails_post),
        max_model_calls_per_case=int(values.get("max_model_calls_per_case", 50)),
        max_read_calls_per_case=int(values.get("max_read_calls_per_case", 30)),
        tools_allow_private_hosts=_as_bool(values.get("tools_allow_private_hosts", False)),
        mcp_allow_stdio=_as_bool(values.get("mcp_allow_stdio", False)),
        knowledge_crawl_delay_seconds=float(values.get("knowledge_crawl_delay_seconds", 0.5)),
        mail_recipients=_as_names(values.get("mail_recipients"), ()),
        mail_max_attachment_mb=int(values.get("mail_max_attachment_mb", 15)),
        filing_dir=Path(str(values["filing_dir"])) if values.get("filing_dir") else None,
        catalog_url=str(values.get("catalog_url", Settings.catalog_url)),
        trusted_levels=_as_names(values.get("trusted_levels"), Settings.trusted_levels),
        trust_keys={
            str(k): str(v) for k, v in (values.get("trust_keys") or {}).items()
        } if isinstance(values.get("trust_keys"), dict) else {},
        whisper_backend=str(values.get("whisper_backend", "auto")),
        whisper_model=str(values.get("whisper_model", "small")),
        whisper_url=str(values.get("whisper_url", "")).strip().rstrip("/"),
        whisper_cpp_path=str(values.get("whisper_cpp_path", "")),
        whisper_cpp_model=str(values.get("whisper_cpp_model", "")),
        ffmpeg_path=str(values.get("ffmpeg_path", "")),
        video_frames=_as_bool(values.get("video_frames", True)),
        knowledge_dir=Path(str(values["knowledge_dir"])) if values.get("knowledge_dir") else None,
        embeddings=str(values.get("embeddings", "auto")),
        embedding_model=str(
            values.get(
                "embedding_model", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
            )
        ),
        ui_dir=Path(str(values["ui_dir"])) if values.get("ui_dir") else None,
    )
