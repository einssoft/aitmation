"""Recorded model answers for the ``test`` configuration (ADR-0035 §9).

A recording is one JSON file per (prompt, input) pair under
``tests/recordings/`` of a solution, keyed by the digest of the composed
prompt (ADR-0034 §4) and the digest of the payload. ``test`` replays
them; a missing recording is a failure, never a silent live call.
``record`` mode calls a real client once and stores the answer - the only
live call on the proving path, and only from the workshop.

Recordings may only stem from ``samples/`` (synthetic inputs); the
validator checks their provenance, so no customer document ever ends up
in a shop bundle.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .aimn import digest as prompt_digest
from .model import ModelClient, ModelError, ModelResponse

RECORDED_MODEL_REF = "recording"


_HEX_ID = re.compile(r"^[0-9a-f]{32}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$")
_FILE_PATH = re.compile(r"^(/|[A-Za-z]:[\\/])\S*$")
_UUID_PREFIX = re.compile(r"^[0-9a-f]{32}_")
_DROPPED_KEYS = frozenset({"chain_id", "from_case", "archived_to", "exported_to"})


def canonical(value: Any) -> Any:
    """The payload with run-specific noise removed, so the same sample
    yields the same key on every run: file paths shrink to their base
    name (minus the stored-file uuid prefix), case ids and timestamps
    become placeholders, dict keys are sorted by the JSON dump.

    Every placeholder needs the *whole* string to match - a document
    text that merely starts with a date or a slash keeps its content,
    so two different documents never share a recording."""
    if isinstance(value, dict):
        return {str(k): canonical(v) for k, v in value.items() if k not in _DROPPED_KEYS}
    if isinstance(value, list | tuple):
        return [canonical(v) for v in value]
    if isinstance(value, str):
        if _HEX_ID.match(value):
            return "<id>"
        if _TIMESTAMP.match(value):
            return "<timestamp>"
        if len(value) < 1024 and _FILE_PATH.match(value):
            name = value.replace("\\", "/").rsplit("/", 1)[-1]
            return _UUID_PREFIX.sub("", name)
        return value
    return value


def payload_digest(payload: Any) -> str:
    text = json.dumps(canonical(payload), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def recording_key(task: str, payload: Any, schema_hint: dict | None) -> str:
    return f"{prompt_digest(task, schema_hint)[:16]}-{payload_digest(payload)[:16]}"


class RecordingClient:
    """A :class:`ModelClient` that replays or records.

    ``mode`` is ``replay`` (default) or ``record``; recording needs an
    ``inner`` client. When replaying, the clearance is the top class -
    nothing leaves the machine. When recording, clearance, locality and
    deployments are the inner client's: the dominance rule and the legal
    space judge the model that is actually called.
    """

    @property
    def clearance(self) -> str:
        if self.mode == "record":
            return str(getattr(self.inner, "clearance", "confidential"))
        return "sensitive"

    @property
    def local(self) -> bool:
        if self.mode == "record":
            return bool(getattr(self.inner, "local", False))
        return True

    @property
    def entries(self) -> tuple:
        if self.mode == "record":
            return tuple(getattr(self.inner, "entries", ()))
        return ()

    def __init__(
        self, directory: str | Path, *, mode: str = "replay", inner: ModelClient | None = None
    ) -> None:
        self.directory = Path(directory)
        if mode not in ("replay", "record"):
            raise ValueError(f"unknown recording mode {mode!r}")
        if mode == "record" and inner is None:
            raise ValueError("record mode needs an inner model client")
        self.mode = mode
        self.inner = inner
        self.calls: list[str] = []
        self.missing: list[str] = []

    def complete_structured(
        self, task: str, payload: Any, schema_hint: dict[str, str] | None = None
    ) -> ModelResponse:
        key = recording_key(task, payload, schema_hint)
        self.calls.append(key)
        path = self.directory / f"{key}.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            response = data["response"]
            return ModelResponse(
                data=response["data"],
                model_ref=RECORDED_MODEL_REF + ":" + str(response.get("model_ref", "?")),
                confidence=float(response.get("confidence", 0.0)),
                tokens_in=response.get("tokens_in"),
                tokens_out=response.get("tokens_out"),
            )
        if self.mode == "replay":
            self.missing.append(key)
            raise ModelError(
                f"no recording {key} for this prompt and input - record it from the workshop"
                " with `aitmation-core test --record` (ADR-0035 §9)"
            )
        assert self.inner is not None
        response = self.inner.complete_structured(task, payload, schema_hint)
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "key": key,
                    "prompt_hash": prompt_digest(task, schema_hint),
                    "payload_digest": payload_digest(payload),
                    "task": task,
                    "schema_hint": schema_hint or {},
                    "response": asdict(response),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return response
