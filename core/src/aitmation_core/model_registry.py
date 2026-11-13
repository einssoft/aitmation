"""Model registry: which model may compute what, where, at what price.

Each entry describes one model deployment with its **provider** (how it
is reached: Anthropic API, Ollama on this machine, an OpenAI-compatible
endpoint such as an EU-hosted provider), its **clearance** (highest data
class it may process - a property of the OPERATING FORM, not of model
quality: the same model is cleared higher on-prem than as a public API),
its **region** and whether a data processing agreement (**dpa**,
Auftragsverarbeitungsvertrag) is in place - the two facts the legal-space
rules (ADR-0027, :mod:`legal`) need -, its **role** in the cost router
(cheap routine model vs. strong escalation model), a human-governed
**status**, whether it can look at images (**vision**, used by the OCR
escalation) and CHF prices per million tokens (maintained manually; used
to freeze the cost of every call at call time, so later price changes
never falsify history).

The registry lives in ``models.toml`` inside the data directory and is
edited through the UI (Einstellungen → Modelle) - :func:`save_registry`
writes it back. Without the file, a built-in default derived from the
settings applies (the two Anthropic models, cleared up to "confidential" -
remote APIs never get "sensitive"; that clearance is reserved for local
models). Releasing a model (status "released") is a human decision - the
file is the record of it.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .data_classes import rank

PROVIDERS = ("anthropic", "ollama", "openai")
"""How a deployment is reached. "openai" means any OpenAI-compatible
chat-completions endpoint (base_url), which covers most EU providers."""

REGIONS = ("local", "CH", "EU", "US", "other")
"""Where the model computes. "local" is this machine; the others are the
operating region of the provider (its own statement, entered by the
owner)."""

_KEYS = {
    "model", "provider", "base_url", "clearance", "role", "status", "cost_in_chf",
    "cost_out_chf", "local", "region", "dpa", "vision", "note",
}
_ROLES = {"cheap", "strong"}
_STATUS = {"released", "experimental", "blocked"}


class RegistryError(ValueError):
    """The model registry file is invalid."""


@dataclass(frozen=True)
class ModelEntry:
    id: str
    model: str
    clearance: str
    role: str
    status: str = "released"
    cost_in_chf: float = 0.0  # CHF per 1M input tokens, maintained manually
    cost_out_chf: float = 0.0  # CHF per 1M output tokens
    local: bool = False
    provider: str = "anthropic"
    base_url: str = ""
    region: str = "US"
    dpa: bool = False  # data processing agreement with the provider in place
    vision: bool = False  # may be asked to read images (OCR escalation)
    note: str = ""

    def validate(self) -> None:
        rank(self.clearance)  # raises on unknown class
        if self.role not in _ROLES:
            raise RegistryError(f"{self.id}: unknown role {self.role!r} (known: {_ROLES})")
        if self.status not in _STATUS:
            raise RegistryError(f"{self.id}: unknown status {self.status!r} (known: {_STATUS})")
        if self.provider not in PROVIDERS:
            raise RegistryError(
                f"{self.id}: unknown provider {self.provider!r} (known: {PROVIDERS})"
            )
        if self.region not in REGIONS:
            raise RegistryError(f"{self.id}: unknown region {self.region!r} (known: {REGIONS})")
        if self.local != (self.region == "local"):
            raise RegistryError(f"{self.id}: local = true means region = 'local' and vice versa")
        if not self.local and self.clearance == "sensitive":
            # Remote deployments never process specially protected data -
            # that clearance is structurally reserved for local models.
            raise RegistryError(
                f"{self.id}: clearance 'sensitive' requires local = true"
            )
        if self.provider == "ollama" and not self.local:
            raise RegistryError(f"{self.id}: an Ollama deployment is local by definition")

    def cost_chf(self, tokens_in: int | None, tokens_out: int | None) -> float | None:
        if tokens_in is None and tokens_out is None:
            return None
        cost = (tokens_in or 0) / 1_000_000 * self.cost_in_chf
        cost += (tokens_out or 0) / 1_000_000 * self.cost_out_chf
        return round(cost, 6)

    def as_dict(self) -> dict:
        return asdict(self)


def default_registry(model_cheap: str, model_strong: str) -> dict[str, ModelEntry]:
    """Built-in fallback when no models.toml exists: the configured
    Anthropic pair, cleared to "confidential". Prices are plausible
    placeholders and should be maintained in the UI."""
    return {
        "cheap": ModelEntry(
            id="cheap",
            model=model_cheap,
            clearance="confidential",
            role="cheap",
            cost_in_chf=0.80,
            cost_out_chf=4.00,
            provider="anthropic",
            region="US",
            vision=True,
        ),
        "strong": ModelEntry(
            id="strong",
            model=model_strong,
            clearance="confidential",
            role="strong",
            cost_in_chf=2.40,
            cost_out_chf=12.00,
            provider="anthropic",
            region="US",
            vision=True,
        ),
    }


def entry_from_dict(entry_id: str, raw: dict, *, source: str = "") -> ModelEntry:
    """Build and validate one entry from a TOML table or an API body."""
    where = f"{source}: " if source else ""
    unknown = set(raw) - _KEYS
    if unknown:
        raise RegistryError(f"{where}entry {entry_id!r} has unknown keys {sorted(unknown)}")
    local = bool(raw.get("local", False))
    provider = str(raw.get("provider") or ("ollama" if local else "anthropic"))
    if provider == "ollama":
        local = True  # an Ollama deployment runs on this machine by definition
    region = str(raw.get("region") or ("local" if local else "US"))
    entry = ModelEntry(
        id=entry_id,
        model=str(raw.get("model", "")),
        clearance=str(raw.get("clearance", "internal")),
        role=str(raw.get("role", "cheap")),
        status=str(raw.get("status", "released")),
        cost_in_chf=float(raw.get("cost_in_chf", 0.0)),
        cost_out_chf=float(raw.get("cost_out_chf", 0.0)),
        local=local,
        provider=provider,
        base_url=str(raw.get("base_url", "")).rstrip("/"),
        region=region,
        dpa=bool(raw.get("dpa", False)),
        vision=bool(raw.get("vision", False)),
        note=str(raw.get("note", "")),
    )
    if not entry.model:
        raise RegistryError(f"{where}entry {entry_id!r} needs a model name")
    try:
        entry.validate()
    except (RegistryError, ValueError) as exc:
        raise RegistryError(f"{where}{exc}") from exc
    return entry


def load_registry(path: str | Path) -> dict[str, ModelEntry]:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{path}: invalid TOML: {exc}") from exc
    out: dict[str, ModelEntry] = {}
    for entry_id, raw in data.items():
        if not isinstance(raw, dict):
            raise RegistryError(f"{path}: entry {entry_id!r} must be a table")
        out[entry_id] = entry_from_dict(entry_id, raw, source=str(path))
    return out


def save_registry(path: str | Path, entries: dict[str, ModelEntry]) -> None:
    """Write the whole registry as TOML (the UI's persistence)."""
    lines = [
        "# Model registry (ADR-0017/ADR-0027). Managed by the aitmation UI:",
        "# which model may compute which data class, where it runs, what it costs.",
        "",
    ]
    for entry_id in sorted(entries):
        e = entries[entry_id]
        lines.append(f"[{_toml_key(entry_id)}]")
        lines.append(f'model = "{_escape(e.model)}"')
        lines.append(f'provider = "{e.provider}"')
        if e.base_url:
            lines.append(f'base_url = "{_escape(e.base_url)}"')
        lines.append(f'clearance = "{e.clearance}"')
        lines.append(f'role = "{e.role}"')
        lines.append(f'status = "{e.status}"')
        lines.append(f"local = {'true' if e.local else 'false'}")
        lines.append(f'region = "{e.region}"')
        lines.append(f"dpa = {'true' if e.dpa else 'false'}")
        lines.append(f"vision = {'true' if e.vision else 'false'}")
        lines.append(f"cost_in_chf = {e.cost_in_chf}")
        lines.append(f"cost_out_chf = {e.cost_out_chf}")
        if e.note:
            lines.append(f'note = "{_escape(e.note)}"')
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def _toml_key(key: str) -> str:
    return key if key.replace("-", "").replace("_", "").isalnum() else f'"{_escape(key)}"'


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def released(registry: dict[str, ModelEntry], role: str) -> ModelEntry | None:
    """The released entry for a router role, if any."""
    for entry in registry.values():
        if entry.role == role and entry.status == "released":
            return entry
    return None


def find_by_model_ref(registry: dict[str, ModelEntry], model_ref: str) -> ModelEntry | None:
    """Match a response's model_ref back to its registry entry (for price
    freezing). The API may report a more specific name than configured,
    so prefix matching is accepted."""
    for entry in registry.values():
        if model_ref == entry.model or model_ref.startswith(entry.model):
            return entry
    return None


# -- the same three variables the assistant uses (LLM_PROVIDER, LLM_MODEL,
#    LLM_API_KEY, optional LLM_BASE_URL): one model for a container setup
#    without touching the UI. The entry is called "llm".

ENV_PROVIDERS = {
    # assistant name -> (registry provider, default base url, region)
    "mistral": ("openai", "https://api.mistral.ai/v1", "EU"),
    "openai": ("openai", "", "US"),
    "anthropic": ("anthropic", "", "US"),
    "ollama": ("ollama", "", "local"),
}
ENV_ENTRY_ID = "llm"


def env_entry(env: dict[str, str]) -> ModelEntry | None:
    """The registry entry described by LLM_* variables, or None."""
    name = env.get("LLM_PROVIDER", "").strip().lower()
    model = env.get("LLM_MODEL", "").strip()
    if not model or name not in ENV_PROVIDERS:
        return None
    provider, base_url, region = ENV_PROVIDERS[name]
    entry = ModelEntry(
        id=ENV_ENTRY_ID,
        model=model,
        clearance=env.get("LLM_CLEARANCE", "").strip() or "internal",
        role="cheap",
        provider=provider,
        base_url=env.get("LLM_BASE_URL", "").strip().rstrip("/") or base_url,
        region=region,
        local=provider == "ollama",
        note=f"from the environment (LLM_PROVIDER={name})",
    )
    entry.validate()
    return entry
