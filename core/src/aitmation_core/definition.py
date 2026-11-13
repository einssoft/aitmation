"""Process definitions: the five primitives and their static validation.

A process definition is a small directed graph over the five primitives of
ADR-0008: Trigger, AgentStep, HumanGate, SystemAction, Timer. Definitions
are data, not code - the template loader (session 4) will produce them from
declarative templates. The core never contains domain logic; a definition
of "incoming invoice" lives in the template layer.

The most important piece here is :meth:`ProcessDefinition.validate`, which
proves the ADR-0007 invariant statically: **no SystemAction is reachable
on any path that has not passed an approving HumanGate outcome.** This is
a property of the definition, not a configuration option; invalid
definitions are rejected before a single case can start.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

END = None
"""Transition target meaning: the case completes."""

PROCESS_PREFIX = "process:"
"""Transition target prefix meaning: the case completes and a successor
sub-process starts with the hand-over payload (ADR-0014 chaining)."""

INTERNAL_ACTIONS = frozenset(
    {
        "export", "archive", "dispatch", "decide", "call", "join", "record", "knowledge", "rows",
        "links", "figures", "route", "file", "pdf",
    }
)
"""Built-in SystemActions that only write inside the instance's own data
directory. They are exempt from the gate requirement: ADR-0007 protects
writes with outside effect (connector calls); archiving into our own
archive folder has none, and dispatch merely starts a successor case in
our own database - the successor's outside effects are protected by its
own gates. ``decide`` evaluates a DMN table (ADR-0034 §2) and ``call``
starts a child case and waits for it (callActivity, ADR-0034 §1); both
only touch our own database; ``join`` is the synchronisation point of a
parallel split (ADR-0037), handled by the engine itself; ``record`` writes
one versioned row into a declared collection of our own database
(ADR-0026, bound field by field like a tool - ADR-0041 §2); ``knowledge``
files a text as a document into a declared knowledge space of our own
(ADR-0031). This set lives in code on purpose - templates cannot declare
themselves harmless."""

SERIAL_ONLY_ACTIONS = frozenset({"forward", "call", "dispatch", "join"})
"""Actions that may never run inside a parallel branch or as a
multi-instance step (ADR-0037 §4): they move files, start and wait for
other cases, or synchronise - one at a time, visibly."""

FOR_EACH_KEYS = frozenset({"over", "as", "collect", "max"})
DEFAULT_MAX_INSTANCES = 20


class StepKind(StrEnum):
    TRIGGER = "trigger"
    AGENT_STEP = "agent_step"
    HUMAN_GATE = "human_gate"
    SYSTEM_ACTION = "system_action"
    TIMER = "timer"


def _reads_only(config: dict) -> bool:
    """A read-only outside call (ADR-0042): a declared tool on a read
    connector (fetch, search). The binder sets both keys from the tool
    declaration - a process file alone cannot claim this."""
    return config.get("effect") == "read" and bool(config.get("tool"))


class DefinitionError(ValueError):
    """The process definition is structurally invalid."""


@dataclass(frozen=True)
class StepDefinition:
    """One node of the process graph.

    ``next`` maps an outcome key (e.g. ``"done"``, ``"approved"``,
    ``"rejected"``, ``"elapsed"``) to the id of the next step, or ``END``
    to complete the case. ``config`` is primitive-specific and opaque to
    the engine except for the documented keys:

    * HumanGate: ``approving_outcomes`` (default ``["approved"]``) - the
      outcomes that count as a passed gate for the ADR-0007 invariant;
      ``deadline_seconds`` (optional) - escalation deadline.
    * Timer: ``seconds`` (required) - how long to wait; completion outcome
      is ``"elapsed"``.
    """

    id: str
    kind: StepKind
    next: dict[str, str | None]
    config: dict = field(default_factory=dict)
    # Parallel split (ADR-0037 §1): outcome -> all branch heads. ``next``
    # keeps the first head so single-token readers still see a target;
    # the engine opens every head when the outcome has a fork.
    forks: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def approving_outcomes(self) -> set[str]:
        return set(self.config.get("approving_outcomes", ["approved"]))

    @property
    def is_join(self) -> bool:
        return self.kind is StepKind.SYSTEM_ACTION and self.config.get("action") == "join"

    @property
    def for_each(self) -> dict | None:
        """Multi-instance declaration (ADR-0037 §2) or None."""
        value = self.config.get("for_each")
        return value if isinstance(value, dict) else None

    def targets(self, outcome: str) -> tuple[str | None, ...]:
        """All targets of an outcome: the branch heads of a fork, else the one."""
        if outcome in self.forks:
            return self.forks[outcome]
        return (self.next[outcome],)


@dataclass(frozen=True)
class ProcessDefinition:
    id: str
    version: int
    steps: dict[str, StepDefinition]
    meta: dict = field(default_factory=dict)
    parameters: dict = field(default_factory=dict)
    contract: dict = field(default_factory=dict)
    policies: dict = field(default_factory=dict)

    def policy(self, name: str) -> dict:
        return self.policies.get(name, {})

    def process_targets(self) -> set[str]:
        """Ids of successor sub-processes referenced via ``process:`` targets."""
        out: set[str] = set()
        for step in self.steps.values():
            for target in step.next.values():
                if isinstance(target, str) and target.startswith(PROCESS_PREFIX):
                    out.add(target[len(PROCESS_PREFIX) :])
        return out

    def call_targets(self) -> set[str]:
        """Ids of processes started by ``call`` actions (callActivity)."""
        return {
            str(step.config.get("process", ""))
            for step in self.steps.values()
            if step.kind is StepKind.SYSTEM_ACTION and step.config.get("action") == "call"
        }

    def joins(self) -> dict[str, tuple[str, str, tuple[str, ...]]]:
        """Join id -> (fork step, fork outcome, branch heads) for every
        parallel split of the process (ADR-0037 §1). Computed by the
        structural check; raises DefinitionError on malformed pairs."""
        memo: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}
        for step in self.steps.values():
            for outcome in step.forks:
                self._resolve_fork((step.id, outcome), memo, set())
        out: dict[str, tuple[str, str, tuple[str, ...]]] = {}
        for (fork_step, outcome), (join_id, heads) in memo.items():
            if join_id in out:
                raise DefinitionError(
                    f"{self.id}: join {join_id!r} closes two splits ({out[join_id][0]!r} and"
                    f" {fork_step!r}) - every split needs its own join"
                )
            out[join_id] = (fork_step, outcome, heads)
        for step in self.steps.values():
            if step.is_join and step.id not in out:
                raise DefinitionError(f"{self.id}: join {step.id!r} has no parallel split")
        return out

    def branch_steps(self) -> dict[str, str]:
        """Step id -> join id for every step that lies inside a parallel
        branch (the join itself excluded)."""
        inside: dict[str, str] = {}
        for join_id, (_fork, _outcome, heads) in self.joins().items():
            for head in heads:
                stack = [head]
                while stack:
                    sid = stack.pop()
                    if sid is None or sid == join_id or sid in inside:
                        continue
                    step = self.steps.get(sid)
                    if step is None:
                        continue
                    if step.is_join:
                        # An inner join: the outer branch continues behind it.
                        stack.extend(t for t in step.next.values() if isinstance(t, str))
                        continue
                    inside[sid] = join_id
                    for outcome in step.next:
                        stack.extend(t for t in step.targets(outcome) if isinstance(t, str))
        return inside

    def _resolve_fork(self, key, memo, visiting) -> tuple[str, tuple[str, ...]]:
        if key in memo:
            return memo[key]
        if key in visiting:
            raise DefinitionError(f"{self.id}: parallel split {key[0]!r} loops back into itself")
        visiting.add(key)
        fork_step, outcome = key
        heads = self.steps[fork_step].forks[outcome]
        if len(heads) < 2:
            raise DefinitionError(f"{self.id}: split at {fork_step!r} needs at least two branches")
        join: str | None = None
        for head in heads:
            found = self._first_join_from(head, fork_step, memo, visiting)
            if join is None:
                join = found
            elif found != join:
                raise DefinitionError(
                    f"{self.id}: branches of the split at {fork_step!r} end in different joins"
                    f" ({join!r}, {found!r})"
                )
        visiting.discard(key)
        memo[key] = (join, heads)  # type: ignore[assignment]
        return memo[key]

    def _first_join_from(self, head, fork_step, memo, visiting) -> str:
        """The join every path from ``head`` reaches first - through nested
        splits, never through the end of the process or a chain hop."""
        result: str | None = None
        stack: list[str] = [head]
        seen: set[str] = set()
        while stack:
            sid = stack.pop()
            if sid in seen:
                continue
            seen.add(sid)
            if sid not in self.steps:
                raise DefinitionError(f"{self.id}: branch of {fork_step!r} targets unknown {sid!r}")
            step = self.steps[sid]
            if step.is_join:
                if result is not None and result != sid:
                    raise DefinitionError(
                        f"{self.id}: branch of the split at {fork_step!r} reaches two joins"
                        f" ({result!r}, {sid!r})"
                    )
                result = sid
                continue
            for outcome, target in step.next.items():
                if outcome in step.forks:
                    inner_join, _ = self._resolve_fork((sid, outcome), memo, visiting)
                    stack.extend(
                        t for t in self.steps[inner_join].next.values() if isinstance(t, str)
                    )
                    continue
                if target is END or (isinstance(target, str) and target.startswith(PROCESS_PREFIX)):
                    raise DefinitionError(
                        f"{self.id}: branch of the split at {fork_step!r} leaves the process at"
                        f" {sid!r} ({outcome!r}) instead of reaching a join (ADR-0037)"
                    )
                stack.append(target)
        if result is None:
            raise DefinitionError(
                f"{self.id}: branch {head!r} of the split at {fork_step!r} never reaches a join"
            )
        return result

    def start_steps(self) -> list[StepDefinition]:
        """Every entry of the process in file order (ADR-0057): a BPMN
        process may have several start events - one per way it is
        triggered (chat, messenger, schedule …)."""
        triggers = [s for s in self.steps.values() if s.kind is StepKind.TRIGGER]
        if not triggers:
            raise DefinitionError(f"{self.id}: at least one Trigger required, found none")
        return triggers

    def start_step(self) -> StepDefinition:
        """The first entry - the only one for most processes."""
        return self.start_steps()[0]

    def sources(self) -> set[str]:
        """The trigger sources of all entries."""
        return {str(s.config.get("source", "")) for s in self.start_steps()}

    def start_with_source(self, source: str) -> StepDefinition | None:
        return next((s for s in self.start_steps() if s.config.get("source") == source), None)

    def start_for(self, payload: object) -> StepDefinition:
        """The entry a trigger payload fires (ADR-0057): the one whose
        ``channel`` it names, else the one of its ``source`` without a
        channel, else the first without a channel, else the first."""
        starts = self.start_steps()
        if len(starts) == 1:
            return starts[0]
        data = payload if isinstance(payload, dict) else {}
        channel = str(data.get("channel", "") or "")
        if channel:
            for s in starts:
                if str(s.config.get("channel", "")) == channel:
                    return s
        source = str(data.get("source", "") or "")
        plain = [s for s in starts if not s.config.get("channel")]
        for s in plain:
            if source and s.config.get("source") == source:
                return s
        return plain[0] if plain else starts[0]

    def validate(self) -> None:
        """Reject structurally invalid definitions.

        Checks: ids consistent, exactly one Trigger, every transition
        target exists, every step has at least one outcome, timers carry a
        duration - and the ADR-0007 invariant via a gate-aware graph walk.
        """
        for key, step in self.steps.items():
            if key != step.id:
                raise DefinitionError(f"{self.id}: step key {key!r} != step id {step.id!r}")
            if not step.next:
                raise DefinitionError(f"{self.id}: step {key!r} has no outcomes")
            for outcome, target in step.next.items():
                if target is END or target in self.steps:
                    continue
                if isinstance(target, str) and target.startswith(PROCESS_PREFIX):
                    if not target[len(PROCESS_PREFIX) :]:
                        raise DefinitionError(
                            f"{self.id}: step {key!r} outcome {outcome!r}"
                            f" has an empty {PROCESS_PREFIX!r} target"
                        )
                    continue  # existence of the successor is checked engine-side
                raise DefinitionError(
                    f"{self.id}: step {key!r} outcome {outcome!r} points to unknown step {target!r}"
                )
            for outcome, heads in step.forks.items():
                if outcome not in step.next:
                    raise DefinitionError(
                        f"{self.id}: step {key!r} forks on unknown outcome {outcome!r}"
                    )
                for head in heads:
                    if head not in self.steps:
                        raise DefinitionError(
                            f"{self.id}: step {key!r} outcome {outcome!r} forks to unknown"
                            f" step {head!r}"
                        )
                if len(set(heads)) != len(heads):
                    raise DefinitionError(f"{self.id}: step {key!r} forks twice to the same step")
            if step.kind is StepKind.TIMER and not (
                "seconds" in step.config or "until_from" in step.config
            ):
                raise DefinitionError(
                    f"{self.id}: timer {key!r} needs config['seconds'] or config['until_from']"
                )
            if step.is_join and list(step.next) != ["done"]:
                raise DefinitionError(f"{self.id}: join {key!r} has exactly one outcome, 'done'")
            self._validate_for_each(step)

        starts = self.start_steps()  # also enforces at least one Trigger
        channels = [str(s.config["channel"]) for s in starts if s.config.get("channel")]
        if len(set(channels)) != len(channels):
            raise DefinitionError(f"{self.id}: two entries share a channel ({channels})")
        inside = self.branch_steps()  # also proves the split/join pairing
        for sid, join_id in inside.items():
            step = self.steps[sid]
            if step.kind is StepKind.SYSTEM_ACTION and (
                step.config.get("action") in SERIAL_ONLY_ACTIONS
                or step.config.get("exec") == "client"
            ):
                raise DefinitionError(
                    f"{self.id}: step {sid!r} ({step.config.get('action')}) may not run inside a"
                    f" parallel branch (join {join_id!r}) - ADR-0037 §4"
                )
        for start in starts:  # ADR-0007 holds from every entry
            self._validate_gate_invariant(start)

    def _validate_for_each(self, step: StepDefinition) -> None:
        spec = step.config.get("for_each")
        if spec is None:
            return
        if not isinstance(spec, dict):
            raise DefinitionError(f"{self.id}: step {step.id!r}: for_each must be a table")
        unknown = set(spec) - FOR_EACH_KEYS
        if unknown:
            raise DefinitionError(
                f"{self.id}: step {step.id!r}: for_each has unknown keys {sorted(unknown)}"
            )
        if step.kind not in (StepKind.AGENT_STEP, StepKind.SYSTEM_ACTION):
            raise DefinitionError(
                f"{self.id}: step {step.id!r}: multi-instance is for AI tasks and actions only"
            )
        if step.kind is StepKind.SYSTEM_ACTION and (
            step.config.get("action") in SERIAL_ONLY_ACTIONS or step.config.get("exec") == "client"
        ):
            raise DefinitionError(
                f"{self.id}: step {step.id!r}: {step.config.get('action')} cannot be multi-instance"
            )
        over, alias = spec.get("over"), spec.get("as")
        if not isinstance(over, str) or not over or not isinstance(alias, str) or not alias:
            raise DefinitionError(
                f"{self.id}: step {step.id!r}: for_each needs 'over' (a path) and 'as' (a name)"
            )
        if "max" in spec and (not isinstance(spec["max"], int) or spec["max"] < 1):
            raise DefinitionError(f"{self.id}: step {step.id!r}: for_each.max must be >= 1")
        if "failed" not in step.next:
            # Nothing at the path or too many items must have somewhere to
            # go; raising instead would roll the previous step back and
            # repeat its (possibly external) effect every tick.
            raise DefinitionError(
                f"{self.id}: step {step.id!r}: a multi-instance step needs a 'failed' outcome"
            )

    def _validate_gate_invariant(self, start: StepDefinition) -> None:
        """DFS over (step, gate_passed) proving the ADR-0007 invariant.

        ``gate_passed`` becomes true only when traversing an approving
        outcome of a HumanGate. Reaching a SystemAction while it is false
        is an error. The state space is at most 2 * len(steps), so the
        walk always terminates, cycles included.
        """
        seen: set[tuple[str, bool]] = set()
        stack: list[tuple[str, bool]] = [(start.id, False)]
        while stack:
            step_id, gate_passed = stack.pop()
            if (step_id, gate_passed) in seen:
                continue
            seen.add((step_id, gate_passed))
            step = self.steps[step_id]
            if (
                step.kind is StepKind.SYSTEM_ACTION
                and not gate_passed
                and step.config.get("action") not in INTERNAL_ACTIONS
                and step.config.get("mandate") is not True
                and not _reads_only(step.config)
            ):
                # ``mandate = true`` declares the mandate regime (ADR-0016):
                # it grants nothing - at runtime the action still requires a
                # valid, human-granted standing mandate (or a passed gate).
                # The engine enforces that second layer.
                raise DefinitionError(
                    f"{self.id}: SystemAction {step_id!r} is reachable without a"
                    " passed HumanGate (ADR-0007)"
                )
            for outcome in step.next:
                passed_here = (
                    step.kind is StepKind.HUMAN_GATE and outcome in step.approving_outcomes()
                )
                # A split opens every branch head (ADR-0037 §4): a gate
                # before the split covers all branches, a gate inside one
                # branch covers that branch alone.
                for target in step.targets(outcome):
                    if target is END or (
                        isinstance(target, str) and target.startswith(PROCESS_PREFIX)
                    ):
                        # A chain boundary: approvals never cross it (ADR-0014.3),
                        # so traversal stops here; the successor proves its own
                        # invariant independently.
                        continue
                    stack.append((target, gate_passed or passed_here))
