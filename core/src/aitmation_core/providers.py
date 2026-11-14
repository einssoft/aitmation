"""Turning a registry entry into a client (ADR-0027).

One place decides how a :class:`ModelEntry` is reached: the provider
field picks the client class, the secret comes from :mod:`secrets` (env
first, then the owner-only file), the base URL and clearance travel with
the entry. Both the router built at start-up and the "test this model"
button in the UI go through :func:`client_for`.
"""

from __future__ import annotations

import logging
import os

from . import secrets as secrets_module
from .config import Settings
from .model import AnthropicClient, ModelClient, ModelRouter, OllamaClient, OpenAICompatibleClient
from .model_registry import (
    ModelEntry,
    RegistryError,
    default_registry,
    env_entry,
    load_registry,
    released,
)
from .secrets import resolve_secret

log = logging.getLogger("aitmation.providers")

SECRET_FOR_PROVIDER = {"anthropic": "anthropic_api_key", "openai": "openai_api_key"}
DEFAULT_BASE_URL = {
    "anthropic": "https://api.anthropic.com",
    "ollama": "http://127.0.0.1:11434",
    "openai": "https://api.openai.com/v1",
}


def registry_of(settings: Settings) -> dict[str, ModelEntry]:
    if settings.models_path.is_file():
        return load_registry(settings.models_path)
    return default_registry(settings.model_cheap, settings.model_strong)


def _key_for(entry: ModelEntry, settings: Settings) -> str | None:
    """The key of an entry's provider. In a shared host the environment is
    not the instance's (secrets.ignore_environment) - except for the model
    the operator declared there: exactly that entry, address included, so
    an entry of the instance's own can never carry the operator's key to
    another host."""
    name = SECRET_FOR_PROVIDER[entry.provider]
    key = resolve_secret(settings.data_dir, name)
    if key or not secrets_module.environment_ignored():
        return key
    try:
        lent = env_entry(dict(os.environ))
    except RegistryError:
        return None
    if lent is not None and entry.id == lent.id and entry == lent:
        return secrets_module.operator_secret(name)
    return None


def missing_secret(entry: ModelEntry, settings: Settings) -> str | None:
    """Name of the secret this entry needs but does not have, or None."""
    name = SECRET_FOR_PROVIDER.get(entry.provider)
    if name and not _key_for(entry, settings):
        return name
    return None


def client_for(entry: ModelEntry, settings: Settings) -> ModelClient | None:
    """A client for one deployment, or None when its secret is missing."""
    base_url = entry.base_url or DEFAULT_BASE_URL[entry.provider]
    if entry.provider == "ollama":
        return OllamaClient(entry.model, base_url=base_url, clearance=entry.clearance)
    api_key = _key_for(entry, settings)
    if not api_key:
        return None
    if entry.provider == "anthropic":
        return AnthropicClient(api_key, entry.model, base_url=base_url, clearance=entry.clearance)
    return OpenAICompatibleClient(
        api_key, entry.model, base_url=base_url, clearance=entry.clearance
    )


def build_model_router(settings: Settings) -> ModelClient | None:
    """The cost router over the released cheap/strong entries, or None when
    nothing usable is released (agent steps then run as stubs). The
    entries behind the router travel with it (``entries``) so the legal-
    space check can see where each call may go."""
    registry = registry_of(settings)
    cheap = released(registry, "cheap")
    strong = released(registry, "strong") or cheap
    if cheap is None or strong is None:
        log.warning("model registry has no released entries - agent steps run as stubs")
        return None
    cheap_client = client_for(cheap, settings)
    strong_client = client_for(strong, settings) if strong is not cheap else cheap_client
    if cheap_client is None or strong_client is None:
        missing = missing_secret(cheap, settings) or missing_secret(strong, settings)
        log.warning("model %s needs secret %s - agent steps run as stubs", cheap.id, missing)
        return None
    router = ModelRouter(
        cheap=cheap_client, strong=strong_client, threshold=settings.model_confidence_threshold
    )
    router.entries = (cheap, strong) if strong is not cheap else (cheap,)  # type: ignore[attr-defined]
    return router


def vision_client(settings: Settings) -> tuple[ModelEntry, ModelClient] | None:
    """The released deployment that may read images, for the OCR
    escalation - preferring the strong role."""
    registry = registry_of(settings)
    candidates = [
        e for e in registry.values()
        if e.status == "released" and e.vision and e.provider == "anthropic"
    ]
    candidates.sort(key=lambda e: 0 if e.role == "strong" else 1)
    for entry in candidates:
        client = client_for(entry, settings)
        if client is not None:
            return entry, client
    return None
