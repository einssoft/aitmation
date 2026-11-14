"""Secrets storage: a restricted file managed by the setup UI.

"Env only" was correct for containers and hostile to the target audience -
nobody at a 30-person company sets environment variables. Resolution order:

1. Environment variable (``AITMATION_ANTHROPIC_API_KEY``,
   ``AITMATION_IMAP_PASSWORD``) - wins, so cloud/container deployments
   keep injecting secrets the standard way.
2. ``secrets.toml`` in the data directory, written by the setup UI with
   permissions restricted to the owner (best effort on Windows).

Secrets never appear in ``config.toml``, in URLs, in logs, in exports or
in a model context (ARCHITECTURE §7). This file store is deliberately
replaceable - the supervisor can later swap it for the OS keychain behind
the same two functions.
"""

from __future__ import annotations

import contextlib
import os
import re
import tomllib
from pathlib import Path

KNOWN_SECRETS = {
    "anthropic_api_key": "AITMATION_ANTHROPIC_API_KEY",
    "openai_api_key": "AITMATION_OPENAI_API_KEY",
    "imap_password": "AITMATION_IMAP_PASSWORD",
    # Web-form channel (ADR-0024 channels): issued in the UI or set by the
    # operator; the form on the website sends it as X-Intake-Token.
    "intake_token": "AITMATION_INTAKE_TOKEN",
    "smtp_password": "AITMATION_SMTP_PASSWORD",
    # Model gateway (ADR-0047): the bearer a program presents to call the
    # instance's released models through /api/v1/gateway.
    "gateway_token": "AITMATION_GATEWAY_TOKEN",
    # Transcription service (ADR-0030, backend http): bearer for whisper_url.
    "whisper_token": "AITMATION_WHISPER_TOKEN",
}


def secret_status(data_dir: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """Where each known secret comes from: "env", "file" or "none". Values
    never leave this module - the UI only learns whether a key is set."""
    env = _process_env() if env is None else dict(env)
    from_file = load_file_secrets(data_dir)
    out: dict[str, str] = {}
    names = list(KNOWN_SECRETS) + sorted(n for n in from_file if n not in KNOWN_SECRETS)
    for name in names:
        if _from_env(name, env):
            out[name] = "env"
        elif from_file.get(name):
            out[name] = "file"
        else:
            out[name] = "none"
    return out


def _secrets_path(data_dir: Path) -> Path:
    return data_dir / "secrets.toml"


# In a shared host (host.py) the process environment belongs to the
# operator, not to any one instance: with the environment ignored, an
# instance reads its secrets from its own file only.
_ignore_environment = False
_lent: frozenset[str] = frozenset()


def ignore_environment(ignore: bool = True, *, lend: tuple[str, ...] = ()) -> None:
    """``lend`` names the secrets every instance may still read from the
    environment - for services whose address the operator fixes too (the
    transcription service's token, never a tool secret)."""
    global _ignore_environment, _lent
    _ignore_environment = ignore
    _lent = frozenset(lend) if ignore else frozenset()


def environment_ignored() -> bool:
    return _ignore_environment


def _process_env() -> dict[str, str]:
    return {} if _ignore_environment else dict(os.environ)


def operator_secret(name: str) -> str | None:
    """A secret from the process environment whatever the policy - for the
    one thing the operator of a shared host lends to every instance: the
    model described by the LLM_* variables (providers.py)."""
    return _from_env(name, dict(os.environ)) or None


_TOOL_SECRET = re.compile(r"^[a-z][a-z0-9_]{1,60}$")


def env_name(name: str) -> str:
    """The environment variable for a secret: the known ones keep their
    historical names, tool secrets (ADR-0041) are AITMATION_SECRET_<NAME>."""
    return KNOWN_SECRETS.get(name) or f"AITMATION_SECRET_{name.upper()}"


def is_valid_name(name: str) -> bool:
    return name in KNOWN_SECRETS or bool(_TOOL_SECRET.match(name))


def is_tool_secret(name: str) -> bool:
    """Tool declarations (ADR-0041) may only reach secrets of their own
    namespace - never the instance's model keys or mail password."""
    return name not in KNOWN_SECRETS and bool(_TOOL_SECRET.match(name))


def load_file_secrets(data_dir: Path) -> dict[str, str]:
    path = _secrets_path(data_dir)
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {k: str(v) for k, v in data.items() if is_valid_name(k) and v}


def save_secret(data_dir: Path, name: str, value: str) -> None:
    """Write or update one secret; an empty value removes it."""
    if not is_valid_name(name):
        raise ValueError(f"invalid secret name {name!r}")
    current = load_file_secrets(data_dir)
    if value:
        current[name] = value
    else:
        current.pop(name, None)
    path = _secrets_path(data_dir)
    lines = ["# Managed by the aitmation setup UI. Owner-readable only."]
    lines += [f'{k} = "{_escape(v)}"' for k, v in sorted(current.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):  # Windows has no POSIX modes
        os.chmod(path, 0o600)


# The provider's own variable names, as every SDK and hosting panel uses
# them: a key set as ANTHROPIC_API_KEY is meant for us too. Our prefixed
# name still wins.
LLM_PROVIDER_SECRET = {
    "mistral": "openai_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
}
PROVIDER_ENV_FALLBACK = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
}


def _from_env(name: str, env: dict[str, str]) -> str:
    value = env.get(env_name(name)) or env.get(PROVIDER_ENV_FALLBACK.get(name, ""), "")
    if not value.strip() and name == LLM_PROVIDER_SECRET.get(
        env.get("LLM_PROVIDER", "").strip().lower()
    ):
        value = env.get("LLM_API_KEY", "")  # the assistant's naming
    return value.strip()


def resolve_secret(data_dir: Path, name: str, env: dict[str, str] | None = None) -> str | None:
    """Environment first (``AITMATION_…``, the provider's usual name, or
    ``LLM_API_KEY`` for the ``LLM_PROVIDER``), secrets file second."""
    if env is None and name in _lent:
        lent = _from_env(name, dict(os.environ))
        if lent:
            return lent
    env = _process_env() if env is None else dict(env)
    return _from_env(name, env) or load_file_secrets(data_dir).get(name)


def _escape(value: str) -> str:
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    # Control characters would break the TOML line (and so every secret).
    return out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
