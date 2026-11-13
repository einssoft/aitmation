"""Guardrail pipeline: pre/post checks around every model call.

Design:

* ``pre`` guardrails run on the payload BEFORE it reaches a model;
  ``post`` guardrails run on the model's answer before it is persisted.
* Each guardrail yields one of ``pass | flag | redact | block | escalate``.
  ``flag`` continues like ``pass`` but marks the step (audit, diagnosis,
  key figure - ADR-0038 §1.6); ``redact`` replaces the checked value with
  a cleaned version and processing continues; ``block`` and ``escalate``
  stop the step via its ``failed`` path (escalation thereby lands where
  the templates route failures: at a human).
* The instance runs a base list before and after every call
  (``Settings.guardrails_pre/post``); a process policy adds to it, never
  removes from it (ADR-0038 §1.5).
* Guardrails live HERE, as a code registry - templates only reference
  registered names, and a declared-but-unregistered name is a load
  error. A typo must never silently switch a safety check off. This is
  the same philosophy as ``definition.INTERNAL_ACTIONS``: templates
  cannot declare themselves harmless.

Audited are name, phase and result - never the checked contents
(diagnostic_events carry the metadata; the content trail is
llm_interactions with its own retention).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_PAYLOAD_CHARS = 500_000
"""Hard ceiling for 'limit_input': inputs beyond this are blocked."""

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),  # Anthropic API keys
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key ids
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    # Value part excludes quotes/backslashes so redaction inside a JSON
    # string never breaks the surrounding JSON document.
    re.compile(r"(?i)(passwort|password|api[_-]?key)\s*[:=]\s*[^\s\"\\]+"),
]

_REDACTED = "[entfernt]"
_LINK_REMOVED = "[link entfernt]"

# Personal data that never belongs in a model log or an unrequested
# answer (ADR-0038 §2): e-mail, phone, Swiss AHV number, IBAN, card number.
_PERSONAL_PATTERNS = [
    # The lookbehind keeps the scan linear: without it every position inside
    # a long run of local-part characters restarts the greedy match.
    ("email", re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("ahv", re.compile(r"\b756\.\d{4}\.\d{4}\.\d{2}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,4})?\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    (
        "phone",
        re.compile(
            r"(?<![\w.])(?:\+41|0041|\+49|\+43|\+33|\+39|0)[ ]?\(?\d{2,3}\)?"
            r"[ ]?\d{3}[ ]?\d{2}[ ]?\d{2}\b"
        ),
    ),
]

_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s\"'<>)]+")

# Phrases that address the model instead of describing the document - the
# classic shape of a prompt injection, in the four product languages and
# English. Matching marks the step; it never blocks (ADR-0038 §1.6).
_INJECTION_PATTERNS = [
    re.compile(r"(?i)\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)"),
    re.compile(r"(?i)\bdisregard\s+(?:all\s+|any\s+)?(?:previous|prior|above|the)\s+(?:instructions?|rules?)"),
    re.compile(r"(?i)\byou\s+are\s+now\s+(?:a|an|the|in)\b"),
    re.compile(r"(?i)\b(?:system|developer)\s+prompt\b"),
    re.compile(r"(?i)\bnew\s+instructions?\s*:"),
    re.compile(r"(?i)\bact\s+as\s+(?:a|an|the)\b.*\b(?:without|no)\s+(?:restrictions?|limits?|rules?)"),
    re.compile(r"(?i)\bdo\s+not\s+(?:tell|reveal|mention)\s+(?:the\s+)?(?:user|human|admin)"),
    re.compile(r"(?i)\bignoriere?\s+(?:alle\s+)?(?:vorherigen|bisherigen|obigen)\s+(?:anweisungen|regeln|instruktionen)"),
    re.compile(r"(?i)\bdu\s+bist\s+(?:jetzt|ab\s+jetzt|nun)\s+(?:ein|eine|der|die)\b"),
    re.compile(r"(?i)\bneue\s+anweisungen?\s*:"),
    re.compile(r"(?i)\bignore[zr]?\s+(?:toutes\s+)?les\s+instructions\s+(?:précédentes|ci-dessus)"),
    re.compile(r"(?i)\btu\s+es\s+(?:maintenant|désormais)\s+(?:un|une)\b"),
    re.compile(r"(?i)\bignora\s+(?:tutte\s+)?le\s+istruzioni\s+(?:precedenti|sopra)"),
    re.compile(r"(?i)\bora\s+sei\s+(?:un|una)\b"),
    re.compile(r"<\s*/?\s*(?:system|input_data|instructions?)\s*>"),
]


class GuardrailError(ValueError):
    """A template references an unregistered guardrail."""


@dataclass(frozen=True)
class GuardrailResult:
    action: str  # pass | flag | redact | block | escalate
    detail: str = ""
    payload: Any = None  # replacement value when action == "redact"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _no_secrets(value: Any, phase: str) -> GuardrailResult:
    """Credentials never travel into model contexts or step results."""
    text = _dump(value)
    hits = 0
    for pattern in _SECRET_PATTERNS:
        text, n = pattern.subn(_REDACTED, text)
        hits += n
    if hits == 0:
        return GuardrailResult("pass")
    return GuardrailResult(
        "redact", detail=f"{hits} secret-like value(s) redacted", payload=json.loads(text)
    )


def _limit_input(value: Any, phase: str) -> GuardrailResult:
    size = len(_dump(value))
    if size <= MAX_PAYLOAD_CHARS:
        return GuardrailResult("pass")
    return GuardrailResult("block", detail=f"payload of {size} chars exceeds {MAX_PAYLOAD_CHARS}")


def _walk_strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Every string inside a payload with the key path that leads to it -
    so a hit names its source (``documents.0.text``, ``knowledge.1``)."""
    out: list[tuple[str, str]] = []
    if isinstance(value, str):
        out.append((path or "$", value))
    elif isinstance(value, dict):
        for k, v in value.items():
            out.extend(_walk_strings(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            out.extend(_walk_strings(v, f"{path}.{i}" if path else str(i)))
    return out


def _injection_suspect(value: Any, phase: str) -> GuardrailResult:
    """Text that addresses the model rather than describing the matter:
    marked with its source, never blocked (ADR-0038 §1.6)."""
    hits: list[str] = []
    for path, text in _walk_strings(value):
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                hits.append(f"{path}: {pattern.pattern[:40]}")
                break
    if not hits:
        return GuardrailResult("pass")
    return GuardrailResult("flag", detail="; ".join(hits[:5]))


def _no_external_links(value: Any, phase: str) -> GuardrailResult:
    """URLs in a model answer are removed: a step never hands a person a
    link it got from the document (phishing, exfiltration by link)."""
    text = _dump(value)
    cleaned, n = _URL.subn(_LINK_REMOVED, text)
    if n == 0:
        return GuardrailResult("pass")
    return GuardrailResult("redact", detail=f"{n} link(s) removed", payload=json.loads(cleaned))


def _identity(kind: str, found: str) -> str:
    """The comparable form of an identifier: an address in lower case,
    numbers by their digits (the last nine, so 0741 … and +49 741 … match)."""
    if kind == "email":
        return "email:" + found.lower()
    digits = re.sub(r"[^0-9A-Za-z]", "", found)
    return f"{kind}:{digits[-9:] if kind == 'phone' else digits.upper()}"


def public_identifiers(text: str) -> set[str]:
    """The identifiers that stand in published sources (the passages a
    step retrieved from a public knowledge space) - an office's phone
    number on its own website is already public."""
    out: set[str] = set()
    for name, pattern in _PERSONAL_PATTERNS:
        for m in pattern.finditer(text):
            out.add(_identity(name, m.group(0)))
    return out


def redact_personal_data(text: str, keep: set[str] | None = None) -> tuple[str, dict[str, int]]:
    """Blank personal identifiers; returns the text and the hit counts.
    ``keep``: identifiers from the sources (:func:`public_identifiers`)
    that may stay."""
    counts: dict[str, int] = {}
    for name, pattern in _PERSONAL_PATTERNS:
        def blank(m: re.Match, name: str = name) -> str:
            if keep and _identity(name, m.group(0)) in keep:
                return m.group(0)
            counts[name] = counts.get(name, 0) + 1
            return _REDACTED
        text = pattern.sub(blank, text)
    return text, counts


def _no_personal_data(value: Any, phase: str, keep: set[str] | None = None) -> GuardrailResult:
    """Personal identifiers in the answer are blanked unless the step
    declares ``personal_data = true`` (checked by the caller); with
    ``personal_data = "sources"`` those that stand in the step's own
    passages stay (``keep``)."""
    cleaned, counts = redact_personal_data(_dump(value), keep)
    if not counts:
        return GuardrailResult("pass")
    detail = ", ".join(f"{k} {n}" for k, n in counts.items())
    return GuardrailResult(
        "redact", detail=f"personal data blanked: {detail}", payload=json.loads(cleaned)
    )


def redact_for_log(value: Any) -> Any:
    """What the model log may keep (ADR-0038 §1.7): secrets and personal
    identifiers blanked, the rest readable for diagnosis."""
    if value is None:
        return None
    text = _dump(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    text, _counts = redact_personal_data(text)
    try:
        return json.loads(text)
    except ValueError:
        return text


DEFAULT_PRE = ("no_secrets", "limit_input", "injection_suspect")
DEFAULT_POST = ("no_secrets", "no_external_links", "no_personal_data")
"""The instance-wide base lists (ADR-0038 §2); a process policy adds."""

OPT_OUT = {"no_external_links": "allow_links", "no_personal_data": "personal_data"}
"""Post guardrails a briefing may switch off for itself, by declaring the
named key - visible in the workshop, checked at load time."""


_REGISTRY: dict[str, Callable[[Any, str], GuardrailResult]] = {
    "no_secrets": _no_secrets,
    "limit_input": _limit_input,
    "injection_suspect": _injection_suspect,
    "no_external_links": _no_external_links,
    "no_personal_data": _no_personal_data,
}


def merged(base: tuple[str, ...] | list[str], declared: list) -> list[str]:
    """Instance base list plus a process's own, in order, without doubles."""
    out: list[str] = []
    for name in [*base, *declared]:
        if name not in out:
            out.append(name)
    return out


def known() -> set[str]:
    return set(_REGISTRY)


def validate_names(names: list, source: str = "<policy>") -> None:
    """Raise loudly on unregistered names - fail-closed at load time."""
    for name in names:
        if name not in _REGISTRY:
            raise GuardrailError(
                f"{source}: unknown guardrail {name!r} (registered: {sorted(_REGISTRY)})"
            )


def run(name: str, value: Any, phase: str, keep: set[str] | None = None) -> GuardrailResult:
    if keep and name == "no_personal_data":
        return _no_personal_data(value, phase, keep)
    return _REGISTRY[name](value, phase)
