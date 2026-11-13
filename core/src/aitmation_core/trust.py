"""Trust levels and signatures of solutions (ADR-0043).

A solution from the shop carries a trust level - ``official`` (built and
maintained by the platform), ``verified`` (submitted by a publisher,
reviewed and signed by the platform) or ``community`` (automatically
checked only) - and a signature of the platform over the whole bundle.
The core verifies the signature when it loads the bundle: a declared
level without a valid signature counts as ``community``, the lowest; a
bundle without any level is ``local`` - built in this instance's own
workshop, always allowed. Which levels an instance installs is a setting
(``trusted_levels``); the default excludes ``community``.

The digest covers every file of the bundle (path and content) with the
manifest taken *without* its ``[signature]`` table, so the signature can
sit inside the file it signs. The platform's public keys are built in
(``PLATFORM_KEYS``) and may be extended by the operator (``trust_keys``
in the configuration) - never by a solution.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

LEVELS = ("official", "verified", "community")
LOCAL = "local"
DEFAULT_TRUSTED = (LOCAL, "official", "verified")

# Public keys of the aitmation platform, by key id. Empty until the shop
# signs its first solution; the operator may add keys in config.toml.
PLATFORM_KEYS: dict[str, str] = {}

_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules"}
_SIGNATURE_BLOCK = re.compile(r"\n*^\[signature\]\n(?:[^\[].*\n?)*", re.MULTILINE)
_LEVEL_LINE = re.compile(r"^trust_level\s*=.*\n", re.MULTILINE)


@dataclass(frozen=True)
class TrustResult:
    level: str  # local | official | verified | community
    verified: bool  # True for local and for a valid platform signature
    reason: str = ""
    signed_by: str = ""


def solution_digest(path: str | Path) -> str:
    """SHA-256 over every file of the bundle: ``<relative path>\\n<sha256>``
    per file in sorted order, the manifest contributing its parsed content
    without the ``[signature]`` table (canonical JSON)."""
    root = Path(path)
    lines: list[str] = []
    for file in sorted(p for p in root.rglob("*") if p.is_file()):
        if any(part in _SKIP_DIRS for part in file.relative_to(root).parts):
            continue
        rel = file.relative_to(root).as_posix()
        if rel == "solution.toml":
            data = tomllib.loads(file.read_text(encoding="utf-8"))
            data.pop("signature", None)
            content = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        else:
            content = file.read_bytes()
        lines.append(f"{rel}\n{hashlib.sha256(content).hexdigest()}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _message(solution_id: str, version: str, level: str, digest: str) -> bytes:
    return f"aitmation-solution/1\n{solution_id}@{version}\n{level}\n{digest}".encode()


def generate_keypair() -> tuple[str, str]:
    """A new Ed25519 pair: (private key PEM, public key base64)."""
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public = base64.b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode("ascii")
    return pem, public


def sign_solution(path: str | Path, private_pem: str, key_id: str, level: str) -> str:
    """Set the trust level and write the platform's signature into the
    manifest. Returns the digest that was signed."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    root = Path(path)
    manifest = root / "solution.toml"
    text = manifest.read_text(encoding="utf-8")
    text = _SIGNATURE_BLOCK.sub("", text)
    text = _LEVEL_LINE.sub("", text)
    if not text.endswith("\n"):
        text += "\n"
    # The level is part of the signed message: it travels in the manifest
    # body (a top-level key, so before the first table), the signature
    # table only carries the proof.
    level_line = f'trust_level = "{level}"\n'
    first_table = re.search(r"^\[", text, re.MULTILINE)
    if first_table is None:
        body = text.rstrip("\n") + "\n" + level_line
    else:
        cut = first_table.start()
        body = text[:cut].rstrip("\n") + "\n" + level_line + "\n" + text[cut:]
    manifest.write_text(body, encoding="utf-8")
    data = tomllib.loads(body)
    digest = solution_digest(root)
    private = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("the signing key must be an Ed25519 private key")
    signature = base64.b64encode(
        private.sign(_message(str(data["id"]), str(data["version"]), level, digest))
    ).decode("ascii")
    signed_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    manifest.write_text(
        body
        + "\n[signature]\n"
        + f'manifest_sha256 = "{digest}"\n'
        + f'signed_by = "{key_id}"\n'
        + f'signed_at = "{signed_at}"\n'
        + f'signature = "{signature}"\n',
        encoding="utf-8",
    )
    return digest


def verify_solution(
    path: str | Path,
    manifest: dict,
    keys: dict[str, str] | None = None,
) -> TrustResult:
    """The effective trust level of a bundle. ``manifest`` is the parsed
    solution.toml; ``keys`` the known platform public keys (id -> base64),
    defaulting to the built-in ones."""
    keys = dict(PLATFORM_KEYS) | dict(keys or {})
    level = manifest.get("trust_level")
    if level is None:
        return TrustResult(LOCAL, True, "no trust level - built here")
    if level not in LEVELS:
        return TrustResult("community", False, f"unknown trust level {level!r}")
    sig = manifest.get("signature")
    if not isinstance(sig, dict):
        return TrustResult("community", False, f"{level!r} declared without a signature")
    key_id = str(sig.get("signed_by", ""))
    public_b64 = keys.get(key_id)
    if not public_b64:
        return TrustResult("community", False, f"unknown signing key {key_id!r}", key_id)
    digest = solution_digest(path)
    if digest != str(sig.get("manifest_sha256", "")):
        return TrustResult("community", False, "bundle changed since it was signed", key_id)
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        public.verify(
            base64.b64decode(str(sig.get("signature", ""))),
            _message(str(manifest.get("id")), str(manifest.get("version")), level, digest),
        )
    except (InvalidSignature, ValueError):
        return TrustResult("community", False, "signature does not verify", key_id)
    return TrustResult(level, True, "platform signature verified", key_id)
