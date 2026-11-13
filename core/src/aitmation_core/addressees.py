"""Addressee configuration for the Vermittler (docs/VERMITTLER.md).

An addressee maps a name to a delivery channel - "to whom it may concern":
the Vermittler decides WHO a document is for, never WHAT runs behind the
channel. Today the only channel kind is a folder (typically the inbox of
another aitmation instance on the same machine); mail and connector
channels are later expansion stages.

The configuration lives in ``addressees.toml`` inside the data directory
and is managed by the admin UI. Deny by default: an addressee exists only
once it is configured here.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_KNOWN_CHANNELS = {"folder"}
_KEYS = {"title", "channel", "path"}


class AddresseeError(ValueError):
    """The addressee configuration is invalid."""


@dataclass(frozen=True)
class Addressee:
    id: str
    title: str
    path: str
    channel: str = "folder"

    def validate(self) -> None:
        if not _ID_PATTERN.match(self.id):
            raise AddresseeError(f"addressee id {self.id!r} must match {_ID_PATTERN.pattern}")
        if not self.title.strip():
            raise AddresseeError(f"addressee {self.id!r} needs a non-empty title")
        if self.channel not in _KNOWN_CHANNELS:
            raise AddresseeError(
                f"addressee {self.id!r}: unknown channel {self.channel!r}"
                f" (known: {sorted(_KNOWN_CHANNELS)})"
            )
        if not self.path.strip():
            raise AddresseeError(f"addressee {self.id!r} needs a delivery path (path)")


def load_addressees(path: str | Path) -> dict[str, Addressee]:
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise AddresseeError(f"{path}: invalid TOML: {exc}") from exc
    out: dict[str, Addressee] = {}
    for addressee_id, raw in data.items():
        if not isinstance(raw, dict):
            raise AddresseeError(f"{path}: addressee {addressee_id!r} must be a table")
        unknown = set(raw) - _KEYS
        if unknown:
            raise AddresseeError(
                f"{path}: addressee {addressee_id!r} has unknown keys {sorted(unknown)}"
            )
        addressee = Addressee(
            id=addressee_id,
            title=str(raw.get("title", "")),
            path=str(raw.get("path", "")),
            channel=str(raw.get("channel", "folder")),
        )
        try:
            addressee.validate()
        except AddresseeError as exc:
            raise AddresseeError(f"{path}: {exc}") from exc
        out[addressee_id] = addressee
    return out


def delete_addressee(path: str | Path, addressee_id: str) -> bool:
    """Remove one addressee from the configuration file; False if absent."""
    path = Path(path)
    current = load_addressees(path)
    if addressee_id not in current:
        return False
    del current[addressee_id]
    lines = ["# Managed by the aitmation admin UI (Vermittler addressees)."]
    for entry in sorted(current.values(), key=lambda a: a.id):
        lines.append("")
        lines.append(f"[{entry.id}]")
        lines.append(f"title = {_toml_string(entry.title)}")
        lines.append(f"channel = {_toml_string(entry.channel)}")
        lines.append(f"path = {_toml_string(entry.path)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_addressee(path: str | Path, addressee: Addressee) -> None:
    """Add or replace one addressee in the configuration file."""
    addressee.validate()
    path = Path(path)
    current = load_addressees(path)
    current[addressee.id] = addressee
    lines = ["# Managed by the aitmation admin UI (Vermittler addressees)."]
    for entry in sorted(current.values(), key=lambda a: a.id):
        lines.append("")
        lines.append(f"[{entry.id}]")
        lines.append(f"title = {_toml_string(entry.title)}")
        lines.append(f"channel = {_toml_string(entry.channel)}")
        lines.append(f"path = {_toml_string(entry.path)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
