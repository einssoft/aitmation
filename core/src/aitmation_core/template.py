"""Template loader: declarative TOML process templates.

"Konfiguration statt Code": a process is described in a TOML file, parsed
with the standard library (``tomllib`` - zero dependencies), turned into a
:class:`~aitmation_core.definition.ProcessDefinition` and statically
validated, including the ADR-0007 gate invariant. Swapping the template
changes the process; the code stays untouched.

Template format::

    id = "demo"
    version = 1

    [steps.intake]
    kind = "trigger"
    next = { fired = "prepare" }

    [steps.prepare]
    kind = "agent_step"
    next = { done = "approve" }

    [steps.approve]
    kind = "human_gate"
    next = { approved = "export", rejected = "end" }

    [steps.export]
    kind = "system_action"
    config = { action = "export" }
    next = { done = "end" }

``"end"`` is a reserved transition target meaning "the case completes";
consequently no step may be named ``end``. Unknown keys are rejected so
typos fail loudly instead of silently changing behavior.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from . import aimn
from .data_classes import DataClassError, demand
from .definition import END, DefinitionError, ProcessDefinition, StepDefinition, StepKind
from .guardrails import GuardrailError, validate_names

END_KEYWORD = "end"
_STEP_KEYS = {"kind", "next", "config"}
_ROOT_KEYS = {"id", "version", "steps", "meta", "parameters", "contract", "policy"}
_PARAM_PATTERN = re.compile(r"\{param:([a-z0-9_]+)\}")
_CONST_PATTERN = re.compile(r"\{const:([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)\}")


class TemplateError(ValueError):
    """The template file cannot be turned into a valid definition."""


def parse_template(
    text: str, source: str = "<string>", *, substitute: bool = True
) -> ProcessDefinition:
    """Parse TOML text into a validated :class:`ProcessDefinition`.

    ``substitute=False`` keeps ``{param:name}`` placeholders in place -
    the migration tool needs the template as written, not as bound."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TemplateError(f"{source}: invalid TOML: {exc}") from exc

    unknown = set(data) - _ROOT_KEYS
    if unknown:
        raise TemplateError(f"{source}: unknown top-level keys {sorted(unknown)}")
    for key in ("id", "version", "steps"):
        if key not in data:
            raise TemplateError(f"{source}: missing required key {key!r}")
    if not isinstance(data["version"], int):
        raise TemplateError(f"{source}: version must be an integer")

    steps: dict[str, StepDefinition] = {}
    for step_id, raw in data["steps"].items():
        if step_id == END_KEYWORD:
            raise TemplateError(f"{source}: step id {END_KEYWORD!r} is reserved")
        if not isinstance(raw, dict):
            raise TemplateError(f"{source}: step {step_id!r} must be a table")
        unknown = set(raw) - _STEP_KEYS
        if unknown:
            raise TemplateError(f"{source}: step {step_id!r} has unknown keys {sorted(unknown)}")
        try:
            kind = StepKind(raw.get("kind", ""))
        except ValueError:
            raise TemplateError(
                f"{source}: step {step_id!r} has unknown kind {raw.get('kind')!r}"
                f" (expected one of {[k.value for k in StepKind]})"
            ) from None
        raw_next = raw.get("next")
        if not isinstance(raw_next, dict) or not raw_next:
            raise TemplateError(f"{source}: step {step_id!r} needs a non-empty 'next' table")
        next_map: dict[str, str | None] = {}
        forks: dict[str, tuple[str, ...]] = {}
        for outcome, target in raw_next.items():
            if isinstance(target, list):
                # A parallel split (ADR-0037): every listed step starts.
                heads = tuple(str(t) for t in target)
                if len(heads) < 2 or END_KEYWORD in heads:
                    raise TemplateError(
                        f"{source}: step {step_id!r} outcome {outcome!r}: a split lists two or"
                        " more steps"
                    )
                forks[outcome] = heads
                next_map[outcome] = heads[0]
            else:
                next_map[outcome] = END if target == END_KEYWORD else target
        steps[step_id] = StepDefinition(
            id=step_id, kind=kind, next=next_map, config=raw.get("config", {}), forks=forks
        )

    return finish_definition(
        data["id"],
        data["version"],
        steps,
        data.get("meta", {}),
        data.get("parameters", {}),
        data.get("contract", {}),
        data.get("policy", {}),
        source,
        substitute=substitute,
    )


def finish_definition(
    process_id: str,
    version: int,
    steps: dict[str, StepDefinition],
    meta: dict,
    parameters: dict,
    contract: dict,
    policies: dict,
    source: str,
    *,
    substitute: bool = True,
    constants: dict | None = None,
) -> ProcessDefinition:
    """Shared tail of both loaders (TOML templates and BPMN processes,
    ADR-0034): parameter and constant substitution, structural validation
    including the ADR-0007 invariant, and the fail-closed policy checks.
    ``constants`` are the solution's ``constants/`` tables (ADR-0035 §2),
    referenced as ``{const:name}`` or ``{const:table.key}``."""
    values = {
        name: (spec.get("default") if isinstance(spec, dict) else spec)
        for name, spec in parameters.items()
    }
    if substitute:
        steps = {
            step_id: StepDefinition(
                id=step.id,
                kind=step.kind,
                next=step.next,
                config=_substitute(step.config, values, source, step_id, constants or {}),
                forks=step.forks,
            )
            for step_id, step in steps.items()
        }
    # The prompt digest keys recordings (ADR-0035 §9) and is audited; it
    # has to describe the prompt as the model sees it, i.e. after
    # parameter substitution.
    for step in steps.values():
        if step.kind is StepKind.AGENT_STEP and "briefing" in step.config:
            step.config["prompt_hash"] = aimn.digest(
                str(step.config.get("task", "")), step.config.get("output")
            )

    definition = ProcessDefinition(
        id=process_id,
        version=version,
        steps=steps,
        meta=meta,
        parameters=parameters,
        contract=contract,
        policies=policies,
    )
    try:
        definition.validate()
    except DefinitionError as exc:
        raise TemplateError(f"{source}: {exc}") from exc

    # Fail-closed load-time checks: a typo in
    # a guardrail name or data class must break the load, never silently
    # weaken a policy.
    guardrail_policy = policies.get("guardrails", {})
    for phase in ("pre", "post"):
        names = guardrail_policy.get(phase, [])
        if not isinstance(names, list):
            raise TemplateError(f"{source}: policy.guardrails.{phase} must be a list")
        try:
            validate_names(names, source)
        except GuardrailError as exc:
            raise TemplateError(str(exc)) from exc
    for step_id, step in steps.items():
        if step.kind is StepKind.AGENT_STEP:
            try:
                demand(step.config, policies)
            except DataClassError as exc:
                raise TemplateError(f"{source}: step {step_id!r}: {exc}") from exc
    return definition


def _substitute(
    value,
    params: dict | None,
    source: str,
    step_id: str,
    constants: dict | None = None,
):
    """Resolve ``{param:name}`` and ``{const:path}`` placeholders in string
    config values.

    Parameters resolve against the template's own defaults; bindings
    (instances overriding parameter values) plug in at the same spot later.
    Constants come from the solution's ``constants/`` tables; a dotted
    path walks nested tables. Unknown names fail loudly. ``params=None``
    leaves parameter placeholders in place (a briefing proven on its own
    has no process to bind them).
    """
    constants = constants or {}
    if isinstance(value, str):

        def repl(match: re.Match) -> str:
            name = match.group(1)
            if params is None:
                return match.group(0)
            if name not in params or params[name] is None:
                raise TemplateError(
                    f"{source}: step {step_id!r} references unknown parameter {name!r}"
                )
            return str(params[name])

        def repl_const(match: re.Match) -> str:
            path = match.group(1)
            node: Any = constants
            for part in path.split("."):
                if not isinstance(node, dict) or part not in node:
                    raise TemplateError(
                        f"{source}: step {step_id!r} references unknown constant {path!r}"
                    )
                node = node[part]
            if isinstance(node, list) and all(
                isinstance(v, str | int | float | bool) for v in node
            ):
                # A list of values reads as prompt material: "a; b; c".
                return "; ".join(str(v) for v in node)
            if isinstance(node, dict | list):
                raise TemplateError(
                    f"{source}: step {step_id!r}: constant {path!r} is a table, not a value"
                )
            return str(node)

        return _CONST_PATTERN.sub(repl_const, _PARAM_PATTERN.sub(repl, value))
    if isinstance(value, dict):
        return {k: _substitute(v, params, source, step_id, constants) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, params, source, step_id, constants) for v in value]
    return value


def load_template(path: str | Path) -> ProcessDefinition:
    path = Path(path)
    return parse_template(path.read_text(encoding="utf-8"), source=str(path))


def load_templates(directory: str | Path) -> dict[str, ProcessDefinition]:
    """Load every ``*.toml`` template and every ``*.bpmn`` process in
    ``directory``, keyed by id. BPMN processes may reference briefings in
    a sibling ``ai/`` directory (ADR-0034)."""
    from .bpmn import BpmnError, load_bpmn

    directory = Path(directory)
    out: dict[str, ProcessDefinition] = {}
    briefings = aimn.load_briefings(directory / "ai")
    for path in sorted(list(directory.glob("*.toml")) + list(directory.glob("*.bpmn"))):
        try:
            definition = (
                load_template(path) if path.suffix == ".toml" else load_bpmn(path, briefings)
            )
        except BpmnError as exc:
            raise TemplateError(str(exc)) from exc
        if definition.id in out:
            raise TemplateError(f"{path}: duplicate template id {definition.id!r}")
        out[definition.id] = definition
    return out
