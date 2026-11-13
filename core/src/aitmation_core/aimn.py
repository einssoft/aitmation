"""AIMN: the AI extension of BPMN/DMN (ADR-0034 §4).

AIMN is the namespace ``https://aitmation.com/schema/aimn/1``. It appears
as ``extensionElements`` inside BPMN processes (``aimn:trigger``,
``aimn:aiTask``, ``aimn:gate``, ``aimn:toolCall``, ``aimn:timer``,
``aimn:chainTo``) and as stand-alone TOML files for AI task briefings
(``ai/<id>.aimn.toml``).

This module holds what is independent of the XML: the **briefing** of an
AI task and its deterministic **prompt composition**. A briefing is not a
free prompt - it is a set of fields (role, instructions, limits, outputs,
model role, flexibility). The core composes the prompt from those fields
in a fixed order, so the same declaration always yields the same prompt;
the digest of the composed prompt is written into the audit trail
(``prompt_hash``) and keys the recordings used by the test configuration
(ADR-0035 §9).

Free-form text going straight to a model does not exist in a process
file. Everything that reaches the model is either a briefing field or the
untrusted ``<input_data>`` block that :mod:`aitmation_core.model` wraps.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import LANGUAGES

NAMESPACE = "https://aitmation.com/schema/aimn/1"
FLEXIBILITY = ("strict", "guided", "open")
"""How much room the model has: ``strict`` only picks declared outcomes
and fills the output schema, ``guided`` may add free text in declared
fields, ``open`` may answer freely (Sierra's creativity/determinism dial,
ADR-0034 §4)."""

MODEL_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_BRIEFING_KEYS = frozenset(
    {
        "id",
        "role",
        "instructions",
        "limits",
        "model_role",
        "flexibility",
        "skills",
        "tools",
        "output",
        "context",
        "confidence_threshold",
    }
)
# Keys of the runtime's AgentStep config that a briefing may carry as-is.
# They are mechanics of the existing executor (K4 context discipline,
# classification/assignment modes, outcomes), not prompt text.
PASS_THROUGH_KEYS = frozenset(
    {
        "read_document",
        "classify",
        "assign",
        "success_outcome",
        "unknown_outcome",
        "stub_outcome",
        "data_class",
        "knowledge_query",
        "knowledge_top_k",
        "knowledge_per_source",
        "knowledge_cite",
        "stream",
        "require_grounding",
        "ungrounded_outcome",
        "ungrounded_call",
        "allow_links",
        "personal_data",
    }
)
# The language the prompt is composed in when the briefing offers it; a
# model reads English instructions most reliably, the document language is
# handed over as context by the runtime (ADR-0022: prompts are language-
# neutral, the content language is a property of the document).
PROMPT_LANGUAGE_ORDER = ("en", "de", "fr", "it")


class BriefingError(ValueError):
    """The briefing cannot be turned into an AI task configuration."""


def prompt_text(value: object, field_name: str) -> dict[str, str]:
    """A prompt field as a language table.

    Unlike UI text (ADR-0022, lead language de), prompt text is language-
    neutral by rule: a bare string counts as English, the language models
    follow most reliably, and a table may carry any subset of the known
    languages. The runtime hands the document language over as context.
    """
    if isinstance(value, str):
        return {"en": value}
    if isinstance(value, dict):
        out: dict[str, str] = {}
        for lang, text in value.items():
            if lang not in LANGUAGES:
                raise BriefingError(
                    f"{field_name}: unknown language {lang!r} (known: {', '.join(LANGUAGES)})"
                )
            if not isinstance(text, str):
                raise BriefingError(f"{field_name}.{lang}: must be a string")
            out[str(lang)] = text
        if not out:
            raise BriefingError(f"{field_name}: empty language table")
        return out
    raise BriefingError(f"{field_name}: must be a string or a language table")


@dataclass(frozen=True)
class Briefing:
    """The declared inside of an AI task (ADR-0034 §4 table)."""

    id: str
    role: dict[str, str] = field(default_factory=dict)
    instructions: dict[str, str] = field(default_factory=dict)
    limits: tuple[dict[str, str], ...] = ()
    model_role: str = ""
    flexibility: str = "strict"
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    output: dict[str, str] = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    confidence_threshold: float | None = None
    extra: dict = field(default_factory=dict)

    def to_config(self, skills: dict[str, Briefing] | None = None) -> dict:
        """The AgentStep config the runtime executes.

        ``task`` is the composed prompt; ``briefing`` keeps the structured
        fields for the audit and the workshop; ``prompt_hash`` is the
        digest of task plus output schema (recordings key on it).
        """
        resolved = self.with_skills(skills or {})
        config: dict = dict(resolved.extra)
        if resolved.output:
            config["output"] = dict(resolved.output)
        if resolved.context:
            config["context"] = dict(resolved.context)
        if resolved.confidence_threshold is not None:
            config["confidence_threshold"] = resolved.confidence_threshold
        task = compose(resolved)
        if task:
            config["task"] = task
        config["briefing"] = {
            "id": self.id,
            "role": dict(self.role),
            "instructions": dict(self.instructions),
            "limits": [dict(limit) for limit in self.limits],
            "model_role": self.model_role,
            "flexibility": self.flexibility,
            "skills": list(self.skills),
            "tools": list(self.tools),
        }
        config["prompt_hash"] = digest(task, resolved.output)
        return config

    def with_skills(self, skills: dict[str, Briefing]) -> Briefing:
        """Fold referenced skills in: their instructions precede the task's
        own, their limits and outputs are merged (the task wins on a key).
        Unknown skills are a load error - a silent gap in instructions is
        exactly what must never happen."""
        if not self.skills:
            return self
        instructions = dict.fromkeys(LANGUAGES, "")
        limits: list[dict[str, str]] = []
        output: dict[str, str] = {}
        context: dict = {}
        for name in self.skills:
            try:
                skill = skills[name]
            except KeyError:
                raise BriefingError(f"{self.id}: unknown skill {name!r}") from None
            for lang, text in skill.instructions.items():
                instructions[lang] = (instructions.get(lang, "") + "\n" + text).strip()
            limits.extend(skill.limits)
            output.update(skill.output)
            context.update(skill.context)
        for lang, text in self.instructions.items():
            instructions[lang] = (instructions.get(lang, "") + "\n" + text).strip()
        instructions = {lang: text for lang, text in instructions.items() if text}
        limits.extend(self.limits)
        output.update(self.output)
        context.update(self.context)
        return Briefing(
            id=self.id,
            role=self.role,
            instructions=instructions,
            limits=tuple(limits),
            model_role=self.model_role,
            flexibility=self.flexibility,
            skills=(),
            tools=self.tools,
            output=output,
            context=context,
            confidence_threshold=self.confidence_threshold,
            extra=self.extra,
        )


def pick_prompt_text(text: dict[str, str]) -> str:
    for lang in PROMPT_LANGUAGE_ORDER:
        if text.get(lang):
            return text[lang].strip()
    for value in text.values():
        if value:
            return value.strip()
    return ""


def compose(briefing: Briefing) -> str:
    """Deterministic prompt composition (ADR-0034 §4, "Prompt-Regel").

    Fixed order, fixed wording, no timestamps, no instance data: role,
    instructions, limits. Tool and outcome descriptions are added by the
    runtime from the declarations they belong to; the output schema is
    passed separately (it is the ``schema_hint`` of the model call).
    """
    parts: list[str] = []
    role = pick_prompt_text(briefing.role)
    if role:
        parts.append(f"Role: {role}")
    instructions = pick_prompt_text(briefing.instructions)
    if instructions:
        parts.append(instructions)
    limits = [pick_prompt_text(limit) for limit in briefing.limits]
    limits = [limit for limit in limits if limit]
    if limits:
        parts.append("Never:\n" + "\n".join(f"- {limit}" for limit in limits))
    if briefing.flexibility == "strict" and (role or instructions):
        parts.append("Answer only with the requested fields; add nothing else.")
    return "\n\n".join(parts)


def digest(task: str, output: dict | None) -> str:
    canonical = json.dumps(
        {"task": task, "output": output or {}}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- TOML briefings ---------------------------------------------------------


def parse_briefing(text: str, source: str = "<string>", default_id: str = "") -> Briefing:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BriefingError(f"{source}: invalid TOML: {exc}") from exc
    return briefing_from_dict(data, source, default_id)


def briefing_from_dict(data: dict, source: str = "<dict>", default_id: str = "") -> Briefing:
    unknown = set(data) - _BRIEFING_KEYS - PASS_THROUGH_KEYS
    if unknown:
        raise BriefingError(f"{source}: unknown keys {sorted(unknown)}")
    briefing_id = str(data.get("id") or default_id)
    if not briefing_id or not re.match(r"^[a-z][a-z0-9_-]*$", briefing_id):
        raise BriefingError(f"{source}: id {briefing_id!r} must be a lowercase identifier")
    try:
        role = prompt_text(data["role"], "role") if data.get("role") else {}
        instructions = (
            prompt_text(data["instructions"], "instructions") if data.get("instructions") else {}
        )
        raw_limits = data.get("limits", [])
        if not isinstance(raw_limits, list):
            raise BriefingError(f"{source}: limits must be a list")
        limits = tuple(prompt_text(limit, "limits") for limit in raw_limits)
    except BriefingError as exc:
        raise BriefingError(f"{source}: {exc}") from None
    model_role = str(data.get("model_role", ""))
    if model_role and not MODEL_ROLE_PATTERN.match(model_role):
        raise BriefingError(f"{source}: model_role {model_role!r} must be a lowercase identifier")
    flexibility = str(data.get("flexibility", "strict"))
    if flexibility not in FLEXIBILITY:
        raise BriefingError(f"{source}: flexibility must be one of {list(FLEXIBILITY)}")
    for key in ("skills", "tools"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise BriefingError(f"{source}: {key} must be a list of strings")
    output = data.get("output", {})
    if not isinstance(output, dict) or not all(isinstance(v, str) for v in output.values()):
        raise BriefingError(f"{source}: output must be a table of field descriptions")
    context = data.get("context", {})
    if not isinstance(context, dict):
        raise BriefingError(f"{source}: context must be a table")
    threshold = data.get("confidence_threshold")
    if threshold is not None:
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            raise BriefingError(f"{source}: confidence_threshold must be a number") from None
    extra = {key: data[key] for key in PASS_THROUGH_KEYS if key in data}
    if "personal_data" in extra and extra["personal_data"] not in (True, False, "sources"):
        raise BriefingError(
            'personal_data: true, false or "sources" (keep what the step\'s own passages carry)'
        )
    return Briefing(
        id=briefing_id,
        role=role,
        instructions=instructions,
        limits=limits,
        model_role=model_role,
        flexibility=flexibility,
        skills=tuple(data.get("skills", [])),
        tools=tuple(data.get("tools", [])),
        output=dict(output),
        context=dict(context),
        confidence_threshold=threshold,
        extra=extra,
    )


def load_briefings(directory: str | Path) -> dict[str, Briefing]:
    """Every ``*.aimn.toml`` (or ``*.toml``) in ``directory``, keyed by id."""
    out: dict[str, Briefing] = {}
    directory = Path(directory)
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.toml")):
        if path.name.endswith(".test.toml"):
            continue  # a sidecar (ADR-0035 §6), not a briefing
        stem = path.name[: -len(".aimn.toml")] if path.name.endswith(".aimn.toml") else path.stem
        briefing = parse_briefing(path.read_text(encoding="utf-8"), str(path), default_id=stem)
        if briefing.id in out:
            raise BriefingError(f"{path}: duplicate briefing id {briefing.id!r}")
        out[briefing.id] = briefing
    return out


def briefing_from_legacy_config(step_id: str, config: dict) -> tuple[Briefing, dict]:
    """Split a format-1 AgentStep config into a briefing and the rest.

    Used by ``migrate-solution``: ``task`` becomes the (English)
    instructions, ``output``/``context``/pass-through keys move into the
    briefing, anything unknown stays in the step config untouched.
    """
    config = dict(config)
    task = str(config.pop("task", "") or "")
    output = config.pop("output", {}) or {}
    context = config.pop("context", {}) or {}
    extra = {key: config.pop(key) for key in list(config) if key in PASS_THROUGH_KEYS}
    briefing = Briefing(
        id=step_id,
        instructions={"en": task} if task else {},
        output=dict(output) if isinstance(output, dict) else {},
        context=dict(context) if isinstance(context, dict) else {},
        extra=extra,
    )
    return briefing, config


def briefing_to_toml(briefing: Briefing) -> str:
    """Serialize a briefing as ``ai/<id>.aimn.toml`` (no tomli-w: the
    format is small and fixed, so a hand-rolled writer keeps zero
    dependencies and a stable key order)."""
    lines = [f"id = {_q(briefing.id)}"]
    if briefing.model_role:
        lines.append(f"model_role = {_q(briefing.model_role)}")
    if briefing.flexibility != "strict":
        lines.append(f"flexibility = {_q(briefing.flexibility)}")
    if briefing.confidence_threshold is not None:
        lines.append(f"confidence_threshold = {briefing.confidence_threshold}")
    if briefing.skills:
        lines.append("skills = [" + ", ".join(_q(s) for s in briefing.skills) + "]")
    if briefing.tools:
        lines.append("tools = [" + ", ".join(_q(t) for t in briefing.tools) + "]")
    for key in sorted(briefing.extra):
        lines.append(f"{key} = {_toml_value(briefing.extra[key])}")
    if briefing.limits:
        lines.append("limits = [")
        for limit in briefing.limits:
            lines.append("  " + _toml_value(limit) + ",")
        lines.append("]")
    for key in ("role", "instructions"):
        table = getattr(briefing, key)
        if table:
            lines.append("")
            lines.append(f"[{key}]")
            for lang in LANGUAGES:
                if lang in table:
                    lines.append(f"{lang} = {_toml_value(table[lang])}")
    if briefing.output:
        lines.append("")
        lines.append("[output]")
        for name, desc in briefing.output.items():
            lines.append(f"{name} = {_toml_value(desc)}")
    if briefing.context:
        lines.append("")
        lines.append("[context]")
        for name, value in briefing.context.items():
            lines.append(f"{name} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        if "\n" in value:
            return '"""\n' + value.replace("\\", "\\\\").replace('"""', '\\"""') + '"""'
        return _q(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise BriefingError(f"cannot serialize {type(value).__name__} to TOML")
