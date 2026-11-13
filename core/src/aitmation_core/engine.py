"""The state machine over the database.

The engine is stateless: the complete state of every case lives in the
database (cases, steps, audit_log). A process restart loses nothing - any
engine instance constructed over the same database continues exactly where
the previous one stopped (ARCHITECTURE §3: "Der Zustand ist jederzeit
vollständig serialisiert").

The engine advances cases; it does not execute work. Executors (the agent
runtime, connector calls, the approval UI) pick up pending steps via
:meth:`Engine.pending_steps` / :meth:`Engine.due_timers` and report results
via :meth:`Engine.complete_step` / :meth:`Engine.decide_gate` /
:meth:`Engine.fire_timer`. That keeps the core domain-neutral: what an
AgentStep *does* is someone else's concern; the engine only guarantees
ordering, gates and the audit trail.

Every transition writes an audit entry carrying the application version
and, where applicable, the model reference (ADR-0013).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aitmation_core import __version__

from .definition import (
    DEFAULT_MAX_INSTANCES,
    END,
    INTERNAL_ACTIONS,
    PROCESS_PREFIX,
    ProcessDefinition,
    StepDefinition,
    StepKind,
    _reads_only,
)

# Step states an executor may act on. HumanGates and Timers are "waiting"
# (on a person / on the clock); AgentSteps and SystemActions are "pending"
# (work to be picked up).
_INITIAL_STEP_STATUS = {
    StepKind.AGENT_STEP: "pending",
    StepKind.SYSTEM_ACTION: "pending",
    StepKind.HUMAN_GATE: "waiting",
    StepKind.TIMER: "waiting",
}


class EngineError(RuntimeError):
    """A request that the current case state does not allow."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _lookup_path(context: dict, path: str):
    """``a.b.c`` into nested dicts (lists by index); None when absent."""
    node: Any = context
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit():
            node = node[int(part)] if int(part) < len(node) else None
        else:
            return None
        if node is None:
            return None
    return node


def origin_trigger(trigger: dict) -> dict:
    """The envelope the chain or call started from: unwrap as many hops as
    there are (a chain after a call nests twice)."""
    seen = 0
    while (
        isinstance(trigger, dict) and trigger.get("source") in ("chain", "call")
        and isinstance(trigger.get("trigger"), dict) and seen < 16
    ):
        trigger = trigger["trigger"]
        seen += 1
    return trigger if isinstance(trigger, dict) else {}


def effective_trigger(trigger: dict) -> dict:
    """The trigger as paths see it: across a chain or a call, the fields of
    the origin trigger stay reachable at the top (``trigger.slot`` in a
    called process), while the chain's own keys (source, chain_id,
    outputs, trigger) win on collision."""
    if trigger.get("source") in ("chain", "call") and isinstance(trigger.get("trigger"), dict):
        return {**origin_trigger(trigger), **trigger}
    return trigger


def _merge_extra(extra: dict | None, mandate_id: str | None) -> dict | None:
    merged = dict(extra or {})
    if mandate_id:
        merged["mandate_id"] = mandate_id
    return merged or None


def input_digest(payload: Any) -> str:
    """Stable digest of a step input, for later comparability (ADR-0013)."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Engine:
    """State machine over one database connection and one definition set.

    ``definitions`` maps template id to a validated
    :class:`ProcessDefinition`. The template loader (session 4) will fill
    this from declarative templates; tests construct it directly.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        definitions: dict[str, ProcessDefinition],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        # The engine's clock - a pinned one under a proving run (ADR-0035
        # §7), per instance, never a module global shared across threads.
        self._now: Callable[[], datetime] = clock or _now
        self.definitions = {}
        for template_id, definition in definitions.items():
            definition.validate()
            self.definitions[template_id] = definition
        # Cross-template check: every process: target must exist in this set.
        for definition in self.definitions.values():
            for successor in definition.process_targets() | definition.call_targets():
                if successor not in self.definitions:
                    raise EngineError(
                        f"{definition.id}: successor process {successor!r} is not loaded"
                    )

    # -- case lifecycle ----------------------------------------------------

    def start_case(
        self,
        template_id: str,
        trigger_payload: Any,
        actor_id: str = "system",
        chain_id: str | None = None,
    ) -> str:
        """Record a trigger event and start a new case. Returns the case id."""
        self.conn.execute("BEGIN")
        try:
            case_id = self._start_case(template_id, trigger_payload, actor_id, chain_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return case_id

    def _start_case(
        self,
        template_id: str,
        trigger_payload: Any,
        actor_id: str,
        chain_id: str | None = None,
        parent: tuple[str, str] | None = None,
    ) -> str:
        """Transaction-less inner start, shared by start_case, chaining and
        the call activity (``parent`` = the waiting case and step)."""
        definition = self._definition(template_id)
        start = definition.start_for(trigger_payload)  # ADR-0057: which entry fired
        case_id = uuid.uuid4().hex
        now = _iso(self._now())
        if True:
            self.conn.execute(
                "INSERT INTO cases (id, template_id, template_version, status,"
                " current_step_id, chain_id, created_at, updated_at, parent_case_id,"
                " parent_step_id) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)",
                (
                    case_id,
                    template_id,
                    definition.version,
                    start.id,
                    chain_id or case_id,
                    now,
                    now,
                    parent[0] if parent else None,
                    parent[1] if parent else None,
                ),
            )
            # The trigger itself is already complete: the event happened.
            self.conn.execute(
                "INSERT INTO steps (id, case_id, step_id, kind, status, outcome,"
                " result, input_digest, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, 'completed', 'fired', ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    case_id,
                    start.id,
                    start.kind.value,
                    json.dumps(trigger_payload, ensure_ascii=False, default=str),
                    input_digest(trigger_payload),
                    now,
                    now,
                ),
            )
            self._audit(
                case_id,
                "system",
                actor_id,
                "case_started",
                {"template_id": template_id, "trigger": start.id},
            )
            if "fired" in start.next:
                self._advance_outcome(case_id, definition, start, "fired", branch=None)
            else:
                self._advance(case_id, definition, END)
        return case_id

    # -- executor-facing API ----------------------------------------------

    def pending_steps(
        self,
        kind: StepKind | None = None,
        *,
        chain_id: str | None = None,
        exclude_chains: set[str] | frozenset[str] = frozenset(),
    ) -> list[sqlite3.Row]:
        """Steps waiting for an executor (agent runtime, connector layer).

        ``chain_id`` narrows to one chain (a synchronous run, ADR-0054);
        ``exclude_chains`` leaves the chains such runs own to them."""
        sql = "SELECT s.* FROM steps s JOIN cases c ON c.id = s.case_id WHERE s.status = 'pending'"
        args: list[Any] = []
        if kind is not None:
            sql += " AND s.kind = ?"
            args.append(kind.value)
        if chain_id is not None:
            sql += " AND COALESCE(c.chain_id, c.id) = ?"
            args.append(chain_id)
        if exclude_chains:
            sql += f" AND COALESCE(c.chain_id, c.id) NOT IN ({','.join('?' * len(exclude_chains))})"
            args.extend(sorted(exclude_chains))
        return list(self.conn.execute(sql + " ORDER BY s.started_at", args))

    def complete_step(
        self,
        case_id: str,
        step_id: str,
        outcome: str,
        result: Any = None,
        *,
        actor_type: str = "agent",
        actor_id: str = "unknown",
        model_ref: str | None = None,
        payload: Any = None,
        mandate_id: str | None = None,
        audit_extra: dict | None = None,
        row_id: str | None = None,
    ) -> None:
        """Report the result of a pending AgentStep or SystemAction.

        ``mandate_id`` names the standing mandate legitimizing an external
        SystemAction without a per-case gate (ADR-0016); it is verified
        against the mandates table and recorded in the audit entry.
        ``audit_extra`` adds structured facts to that entry - the prompt
        digest of an AI task (ADR-0034 §4), the configuration a tool ran
        under (ADR-0035 §7).
        """
        definition, step = self._open_step(case_id, step_id, {"pending"}, row_id)
        step_config = definition.steps[step_id].config
        # Defense in depth: statically impossible for validated
        # definitions, but never trust a single layer for ADR-0007. The
        # second legitimate source of approval is a valid standing mandate
        # for this action, on a step declared mandate-capable (ADR-0016).
        if (
            step["kind"] == StepKind.SYSTEM_ACTION.value
            and step_config.get("action") not in INTERNAL_ACTIONS
            and not _reads_only(step_config)
            and self._case(case_id)["gate_passed_at"] is None
            and not (
                step_config.get("mandate") is True
                and mandate_id
                and self._mandate_is_valid(mandate_id, step_config.get("action", ""))
            )
        ):
            raise EngineError(
                f"SystemAction {step_id!r} before any passed HumanGate"
                " and without a valid standing mandate"
            )
        self._finish_step(
            case_id,
            step,
            outcome,
            result,
            actor_type=actor_type,
            actor_id=actor_id,
            model_ref=model_ref,
            payload=payload,
            definition=definition,
            audit_extra=_merge_extra(audit_extra, mandate_id),
        )

    # -- standing mandates (ADR-0016) --------------------------------------
    #
    # A mandate moves the approval from "per case" to "per rule": granted
    # once by a human, deterministic in its condition, visible, revocable,
    # and referenced in the audit trail of every execution it legitimizes.
    # Grant/revoke history lives in the mandates table itself (rows are
    # never deleted; revocation is a timestamp).

    def grant_mandate(
        self,
        action: str,
        target: str,
        condition: dict | None = None,
        *,
        actor_id: str,
        comment: str = "",
    ) -> str:
        """Record a standing mandate. Returns the mandate id.

        ``condition`` maps envelope fields to exact required values; an
        empty condition is a catch-all (used for the single-mandate
        default installation, docs/VERMITTLER.md).
        """
        if not action or not target:
            raise EngineError("a mandate needs both an action and a target")
        mandate_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO mandates (id, action, target, condition, comment,"
            " granted_by, granted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mandate_id,
                action,
                target,
                json.dumps(condition or {}, ensure_ascii=False, sort_keys=True),
                comment,
                actor_id,
                _iso(self._now()),
            ),
        )
        return mandate_id

    def revoke_mandate(self, mandate_id: str, actor_id: str) -> None:
        row = self.conn.execute(
            "SELECT id, revoked_at FROM mandates WHERE id = ?", (mandate_id,)
        ).fetchone()
        if row is None:
            raise EngineError(f"unknown mandate {mandate_id!r}")
        if row["revoked_at"] is not None:
            raise EngineError(f"mandate {mandate_id!r} is already revoked")
        self.conn.execute(
            "UPDATE mandates SET revoked_by = ?, revoked_at = ? WHERE id = ?",
            (actor_id, _iso(self._now()), mandate_id),
        )

    def active_mandates(self, action: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM mandates WHERE revoked_at IS NULL"
        args: tuple[Any, ...] = ()
        if action is not None:
            sql += " AND action = ?"
            args = (action,)
        return list(self.conn.execute(sql + " ORDER BY granted_at", args))

    def find_mandate(self, action: str, envelope: dict) -> sqlite3.Row | None:
        """The active mandate matching an envelope, most specific first.

        Matching is deliberately deterministic: every condition field must
        equal the envelope value exactly. No model is involved - that is
        the point of a mandate (ADR-0016)."""
        candidates = []
        for row in self.active_mandates(action):
            condition: dict = json.loads(row["condition"])
            if all(envelope.get(field) == value for field, value in condition.items()):
                candidates.append((len(condition), row["granted_at"], row))
        if not candidates:
            return None
        candidates.sort(key=lambda c: (-c[0], c[1]))
        return candidates[0][2]

    def _mandate_is_valid(self, mandate_id: str, action: str) -> bool:
        row = self.conn.execute(
            "SELECT action, revoked_at FROM mandates WHERE id = ?", (mandate_id,)
        ).fetchone()
        return row is not None and row["revoked_at"] is None and row["action"] == action

    def decide_gate(
        self,
        case_id: str,
        step_id: str,
        outcome: str,
        actor_id: str,
        comment: str = "",
        extra: dict | None = None,
        row_id: str | None = None,
    ) -> None:
        """Record a human decision on a waiting HumanGate (ADR-0007).

        ``extra`` carries structured decision data beyond the comment,
        e.g. the chosen category at a categorization gate.
        """
        definition, step = self._open_step(case_id, step_id, {"waiting"}, row_id)
        if step["kind"] != StepKind.HUMAN_GATE.value:
            raise EngineError(f"step {step_id!r} is not a HumanGate")
        gate = definition.steps[step_id]
        if outcome in gate.approving_outcomes():
            self.conn.execute(
                "UPDATE cases SET gate_passed_at = ? WHERE id = ?",
                (_iso(self._now()), case_id),
            )
        result = {"comment": comment, **(extra or {})}
        self._finish_step(
            case_id,
            step,
            outcome,
            result,
            actor_type="human",
            actor_id=actor_id,
            definition=definition,
        )

    def decide_gate_by_mandate(
        self, case_id: str, step_id: str, mandate_id: str, row_id: str | None = None
    ) -> None:
        """Decide a waiting HumanGate by a standing mandate (ADR-0016,
        ADR-0054 §3): a person's decision given once for all cases with the
        same facts. The mandate must be active, name this very gate and an
        outcome the gate has; the audit records it as the actor."""
        definition, step = self._open_step(case_id, step_id, {"waiting"}, row_id)
        if step["kind"] != StepKind.HUMAN_GATE.value:
            raise EngineError(f"step {step_id!r} is not a HumanGate")
        case = self._case(case_id)
        action = f"gate:{case['template_id']}:{step_id}"
        mandate = self.conn.execute("SELECT * FROM mandates WHERE id = ?", (mandate_id,)).fetchone()
        if mandate is None or mandate["revoked_at"] is not None or mandate["action"] != action:
            raise EngineError(f"mandate {mandate_id!r} does not cover gate {step_id!r}")
        outcome = str(mandate["target"])
        gate = definition.steps[step_id]
        if outcome not in gate.next and outcome not in gate.forks:
            raise EngineError(f"mandate outcome {outcome!r} is not an outcome of gate {step_id!r}")
        if outcome in gate.approving_outcomes():
            self.conn.execute(
                "UPDATE cases SET gate_passed_at = ? WHERE id = ?", (_iso(self._now()), case_id)
            )
        self._finish_step(
            case_id,
            step,
            outcome,
            {"comment": "", "mandate": mandate_id},
            actor_type="mandate",
            actor_id=mandate_id,
            definition=definition,
            audit_extra={"mandate_id": mandate_id},
        )

    def start_successor(self, case_id: str, template_id: str, actor_id: str = "dispatch") -> str:
        """Start a successor sub-process for a completed case (dispatch).

        Unlike ``process:`` targets (declared in the template), the
        successor here is chosen at runtime from data - the document
        type's declared chain. Chain semantics are identical to ADR-0014:
        shared ``chain_id``, hand-over payload with the original trigger
        on top, and no gate inheritance across the boundary.
        """
        case = self._case(case_id)
        definition = self._definition(case["template_id"])
        target = self._definition(template_id)  # fail before touching the db
        chain_id = case["chain_id"] or case_id
        handoff = self._handoff_payload(case_id, definition, chain_id)
        # Called from a call activity? The successor inherits the parent
        # pointer, so the caller resumes when the dispatched chain ends.
        parent = (
            (case["parent_case_id"], case["parent_step_id"]) if case["parent_case_id"] else None
        )
        self.conn.execute("BEGIN")
        try:
            self._audit(case_id, "system", actor_id, "case_dispatched", {"to": target.id})
            if parent:
                self.conn.execute(
                    "UPDATE cases SET parent_case_id = NULL, parent_step_id = NULL WHERE id = ?",
                    (case_id,),
                )
            new_case = self._start_case(template_id, handoff, actor_id, chain_id, parent=parent)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return new_case

    def call_subprocess(self, case_id: str, step_id: str, template_id: str) -> str:
        """Call activity (ADR-0034 §1): start ``template_id`` as a child of
        the pending ``call`` step and put that step to ``waiting``. The
        child shares the chain id; when it - or the chain it hands over
        to - completes, :meth:`_advance` completes the parent's step with
        outcome ``done`` and the child's outputs as result."""
        definition, step = self._open_step(case_id, step_id, {"pending"})
        target = self._definition(template_id)
        chain_id = self._case(case_id)["chain_id"] or case_id
        handoff = self._handoff_payload(case_id, definition, chain_id)
        handoff["source"] = "call"
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("UPDATE steps SET status = 'waiting' WHERE id = ?", (step["id"],))
            self._audit(
                case_id, "system", "engine", "subprocess_called",
                {"step": step_id, "process": target.id},
            )
            child = self._start_case(
                template_id, handoff, "call", chain_id, parent=(case_id, step_id)
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return child

    def _return_to_parent(self, case_id: str, definition: ProcessDefinition) -> None:
        """A case with a parent pointer completed: finish the parent's
        waiting ``call`` step with the child's outputs. Runs inside the
        caller's transaction."""
        case = self._case(case_id)
        parent_case, parent_step = case["parent_case_id"], case["parent_step_id"]
        if not parent_case:
            return
        row = self.conn.execute(
            "SELECT * FROM steps WHERE case_id = ? AND step_id = ? AND status = 'waiting'",
            (parent_case, parent_step),
        ).fetchone()
        if row is None:
            return
        parent_definition = self._definition(self._case(parent_case)["template_id"])
        outputs = self._handoff_payload(case_id, definition, case["chain_id"])["outputs"]
        step_def = parent_definition.steps[parent_step]
        outcome = "done" if "done" in step_def.next else next(iter(step_def.next))
        now = _iso(self._now())
        self.conn.execute(
            "UPDATE steps SET status = 'completed', outcome = ?, result = ?, finished_at = ?"
            " WHERE id = ?",
            (
                outcome,
                json.dumps({"child_case": case_id, "outputs": outputs}, default=str),
                now,
                row["id"],
            ),
        )
        self._audit(
            parent_case,
            "system",
            "engine",
            "step_completed",
            {
                "step": parent_step,
                "kind": step_def.kind.value,
                "outcome": outcome,
                "child_case": case_id,
            },
        )
        self._advance_outcome(
            parent_case, parent_definition, step_def, outcome, branch=row["branch"]
        )

    def due_timers(self, now: datetime | None = None) -> list[sqlite3.Row]:
        """Waiting timers whose deadline has passed."""
        return list(
            self.conn.execute(
                "SELECT * FROM steps WHERE status = 'waiting' AND kind = 'timer'"
                " AND deadline_at <= ? ORDER BY deadline_at",
                (_iso(now or self._now()),),
            )
        )

    def fire_timer(self, case_id: str, step_id: str, row_id: str | None = None) -> None:
        """Complete a due timer with the outcome ``elapsed``."""
        definition, step = self._open_step(case_id, step_id, {"waiting"}, row_id)
        if step["kind"] != StepKind.TIMER.value:
            raise EngineError(f"step {step_id!r} is not a Timer")
        self._finish_step(
            case_id,
            step,
            "elapsed",
            None,
            actor_type="system",
            actor_id="scheduler",
            definition=definition,
        )

    # -- internals ---------------------------------------------------------

    def _definition(self, template_id: str) -> ProcessDefinition:
        try:
            return self.definitions[template_id]
        except KeyError as exc:
            raise EngineError(f"unknown template {template_id!r}") from exc

    def _case(self, case_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            raise EngineError(f"unknown case {case_id!r}")
        return row

    def _open_step(
        self, case_id: str, step_id: str, allowed_status: set[str], row_id: str | None = None
    ) -> tuple[ProcessDefinition, sqlite3.Row]:
        case = self._case(case_id)
        if case["status"] != "running":
            raise EngineError(f"case {case_id!r} is {case['status']}, not running")
        definition = self._definition(case["template_id"])
        if row_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM steps WHERE id = ? AND case_id = ? AND step_id = ?"
                " AND finished_at IS NULL",
                (row_id, case_id, step_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM steps WHERE case_id = ? AND step_id = ? AND finished_at IS NULL"
                " ORDER BY instance_index",
                (case_id, step_id),
            ).fetchall()
        if len(rows) > 1:
            # Several instances of one step are open (ADR-0037 §2): the
            # caller has to say which row it means.
            raise EngineError(
                f"case {case_id!r} has {len(rows)} open instances of {step_id!r} - name the row"
            )
        row = rows[0] if rows else None
        if row is None or row["status"] not in allowed_status:
            raise EngineError(f"case {case_id!r} has no open step {step_id!r} in {allowed_status}")
        return definition, row

    def _finish_step(
        self,
        case_id: str,
        step: sqlite3.Row,
        outcome: str,
        result: Any,
        *,
        actor_type: str,
        actor_id: str,
        definition: ProcessDefinition,
        model_ref: str | None = None,
        payload: Any = None,
        audit_extra: dict | None = None,
    ) -> None:
        step_def = definition.steps[step["step_id"]]
        if outcome not in step_def.next:
            raise EngineError(
                f"outcome {outcome!r} not defined for step {step['step_id']!r}"
                f" (allowed: {sorted(step_def.next)})"
            )
        now = _iso(self._now())
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "UPDATE steps SET status = 'completed', outcome = ?, result = ?,"
                " model_ref = ?, input_digest = COALESCE(?, input_digest),"
                " finished_at = ? WHERE id = ?",
                (
                    outcome,
                    json.dumps(result, ensure_ascii=False, default=str),
                    model_ref,
                    input_digest(payload) if payload is not None else None,
                    now,
                    step["id"],
                ),
            )
            details = {"step": step["step_id"], "kind": step["kind"], "outcome": outcome}
            if step["branch"]:
                details["branch"] = step["branch"]
            if step["instance_total"] is not None:
                details["instance"] = step["instance_index"]
                details["total"] = step["instance_total"]
            if audit_extra:
                details.update(audit_extra)
            self._audit(
                case_id, actor_type, actor_id, "step_completed", details, model_ref=model_ref
            )
            if step["instance_total"] is not None:
                # One instance of a multi-instance step (ADR-0037 §2): the
                # step as a whole moves on when the last instance has.
                open_left = self.conn.execute(
                    "SELECT COUNT(*) FROM steps WHERE case_id = ? AND step_id = ?"
                    " AND finished_at IS NULL",
                    (case_id, step["step_id"]),
                ).fetchone()[0]
                if open_left:
                    self.conn.execute("COMMIT")
                    return
                outcome = self._aggregate_instances(case_id, step_def)
            # A completed dispatch hands the chain over at runtime
            # (start_successor): a waiting caller is answered by the
            # successor's end, not by this hop's.
            dispatching = (
                step_def.kind is StepKind.SYSTEM_ACTION
                and step_def.config.get("action") == "dispatch"
                and outcome == "done"
            )
            self._advance_outcome(
                case_id, definition, step_def, outcome, branch=step["branch"],
                hold_return=dispatching,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _aggregate_instances(self, case_id: str, step_def: StepDefinition) -> str:
        """The outcome of a multi-instance step once every instance is
        done: ``done`` if all are, else ``failed`` when the step has it,
        else the outcome of the first instance that did not succeed."""
        rows = self.conn.execute(
            "SELECT instance_index, outcome FROM steps WHERE case_id = ? AND step_id = ?"
            " ORDER BY instance_index",
            (case_id, step_def.id),
        ).fetchall()
        outcomes = [r["outcome"] for r in rows]
        success = "done" if "done" in step_def.next else next(iter(step_def.next))
        aggregate = success
        for o in outcomes:
            if o != success:
                aggregate = "failed" if "failed" in step_def.next else o
                break
        self._audit(
            case_id, "system", "engine", "instances_completed",
            {"step": step_def.id, "total": len(rows), "outcomes": outcomes, "outcome": aggregate},
        )
        return aggregate

    def _advance_outcome(
        self,
        case_id: str,
        definition: ProcessDefinition,
        step_def: StepDefinition,
        outcome: str,
        *,
        branch: str | None,
        hold_return: bool = False,
    ) -> None:
        """Follow an outcome: a parallel split opens every branch head with
        its own branch id (ADR-0037 §3); anything else is one target."""
        if outcome in step_def.forks:
            heads = step_def.forks[outcome]
            self._audit(
                case_id, "system", "engine", "branches_opened",
                {"step": step_def.id, "outcome": outcome, "branches": list(heads)},
            )
            for head in heads:
                child = f"{branch + '/' if branch else ''}{step_def.id}.{head}"
                self._advance(case_id, definition, head, branch=child)
            return
        self._advance(
            case_id, definition, step_def.next[outcome], branch=branch, hold_return=hold_return
        )

    def _timer_deadline(self, case_id: str, config: dict, now: datetime) -> str:
        """A timer waits ``seconds`` - or, with ``until_from = "<key>"``, until
        the moment the trigger payload carries under that key (a date or a
        date-time; a bare date means the start of that day, local time),
        shifted by ``offset_seconds`` (negative = before: a reminder a day
        ahead is ``-86400``). A missing or unreadable value falls back to
        ``seconds`` or fires at once, so a deadline never blocks a case
        forever."""
        key = config.get("until_from")
        if key:
            row = self.conn.execute(
                "SELECT result FROM steps WHERE case_id = ? AND kind = 'trigger'", (case_id,)
            ).fetchone()
            payload = json.loads(row["result"]) if row and row["result"] else {}
            raw = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(raw, str) and raw.strip():
                try:
                    parsed = datetime.fromisoformat(raw.strip())
                except ValueError:
                    parsed = None
                if parsed is not None:
                    if parsed.tzinfo is None:
                        parsed = parsed.astimezone()  # local wall time
                    try:
                        shift = float(config.get("offset_seconds", 0) or 0)
                    except (TypeError, ValueError):
                        shift = 0.0
                    return _iso(parsed.astimezone(UTC) + timedelta(seconds=shift))
        seconds = float(config.get("seconds", 0))
        return _iso(now + timedelta(seconds=seconds))

    def _advance(
        self,
        case_id: str,
        definition: ProcessDefinition,
        target: str | None,
        *,
        hold_return: bool = False,
        branch: str | None = None,
    ) -> None:
        """Move the case to ``target``: open its step row, close the case,
        or - for a ``process:`` target - close it and start the successor
        sub-process with the hand-over payload (ADR-0014 chaining).
        ``hold_return`` keeps a waiting caller waiting because a dispatch
        is about to hand the chain over. ``branch`` is the parallel branch
        the token travels on (ADR-0037); a join collects the branches of
        its split and continues on the enclosing one."""
        now = self._now()
        if isinstance(target, str) and target.startswith(PROCESS_PREFIX):
            successor = target[len(PROCESS_PREFIX) :]
            chain_id = self._case(case_id)["chain_id"] or case_id
            self.conn.execute(
                "UPDATE cases SET status = 'completed', current_step_id = NULL,"
                " updated_at = ?, closed_at = ? WHERE id = ?",
                (_iso(now), _iso(now), case_id),
            )
            self._audit(case_id, "system", "engine", "case_completed", {"handoff_to": successor})
            handoff = self._handoff_payload(case_id, definition, chain_id)
            case = self._case(case_id)
            parent = (
                (case["parent_case_id"], case["parent_step_id"]) if case["parent_case_id"] else None
            )
            # A called chain returns to the caller only when it ends: the
            # successor inherits the parent pointer, the completed hop
            # clears its own so it cannot return twice.
            if parent:
                self.conn.execute(
                    "UPDATE cases SET parent_case_id = NULL, parent_step_id = NULL WHERE id = ?",
                    (case_id,),
                )
            self._start_case(successor, handoff, actor_id="chain", chain_id=chain_id, parent=parent)
            return
        if target is END:
            self.conn.execute(
                "UPDATE cases SET status = 'completed', current_step_id = NULL,"
                " updated_at = ?, closed_at = ? WHERE id = ?",
                (_iso(now), _iso(now), case_id),
            )
            self._audit(case_id, "system", "engine", "case_completed", {})
            if not hold_return:
                self._return_to_parent(case_id, definition)
            return
        step_def = definition.steps[target]
        if step_def.is_join:
            self._arrive_at_join(case_id, definition, step_def, branch)
            return
        if step_def.for_each is not None:
            self._open_instances(case_id, definition, step_def, branch, now)
            return
        deadline: str | None = None
        if step_def.kind is StepKind.TIMER:
            deadline = self._timer_deadline(case_id, step_def.config, now)
        elif step_def.kind is StepKind.HUMAN_GATE and "deadline_seconds" in step_def.config:
            deadline = _iso(now + timedelta(seconds=float(step_def.config["deadline_seconds"])))
        self.conn.execute(
            "INSERT INTO steps (id, case_id, step_id, kind, status, deadline_at, started_at,"
            " branch) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                case_id,
                step_def.id,
                step_def.kind.value,
                _INITIAL_STEP_STATUS[step_def.kind],
                deadline,
                _iso(now),
                branch,
            ),
        )
        self.conn.execute(
            "UPDATE cases SET current_step_id = ?, updated_at = ? WHERE id = ?",
            (step_def.id, _iso(now), case_id),
        )
        details = {"step": step_def.id, "kind": step_def.kind.value}
        if branch:
            details["branch"] = branch
        self._audit(case_id, "system", "engine", "step_opened", details)

    def _arrive_at_join(
        self, case_id: str, definition: ProcessDefinition, join: StepDefinition, branch: str | None
    ) -> None:
        """A branch reached its join (ADR-0037 §3): the first arrival opens
        the join row, every arrival signs in, the last one completes the
        join and continues on the enclosing branch."""
        _fork_step, _outcome, heads = definition.joins()[join.id]
        expected = len(heads)
        parent_branch = branch.rsplit("/", 1)[0] if branch and "/" in branch else None
        now = _iso(self._now())
        row = self.conn.execute(
            "SELECT * FROM steps WHERE case_id = ? AND step_id = ? AND finished_at IS NULL",
            (case_id, join.id),
        ).fetchone()
        arrived: list[str] = []
        if row is None:
            self.conn.execute(
                "INSERT INTO steps (id, case_id, step_id, kind, status, started_at, branch)"
                " VALUES (?, ?, ?, ?, 'waiting', ?, ?)",
                (uuid.uuid4().hex, case_id, join.id, join.kind.value, now, parent_branch),
            )
            row = self.conn.execute(
                "SELECT * FROM steps WHERE case_id = ? AND step_id = ? AND finished_at IS NULL",
                (case_id, join.id),
            ).fetchone()
            self._audit(
                case_id, "system", "engine", "step_opened",
                {"step": join.id, "kind": join.kind.value, "expects": expected},
            )
        else:
            arrived = list(json.loads(row["result"] or "{}").get("arrived", []))
        if branch in arrived:
            raise EngineError(f"branch {branch!r} arrived twice at join {join.id!r}")
        arrived.append(branch or "")
        if len(arrived) > expected:
            raise EngineError(
                f"join {join.id!r} received {len(arrived)} branches, expected {expected}"
            )
        self.conn.execute(
            "UPDATE cases SET current_step_id = ?, updated_at = ? WHERE id = ?",
            (join.id, now, case_id),
        )
        if len(arrived) < expected:
            self.conn.execute(
                "UPDATE steps SET result = ? WHERE id = ?",
                (json.dumps({"arrived": arrived, "expected": expected}), row["id"]),
            )
            self._audit(
                case_id, "system", "engine", "branch_arrived",
                {"step": join.id, "branch": branch, "arrived": len(arrived), "expected": expected},
            )
            return
        self.conn.execute(
            "UPDATE steps SET status = 'completed', outcome = 'done', result = ?, finished_at = ?"
            " WHERE id = ?",
            (json.dumps({"arrived": arrived, "expected": expected}), now, row["id"]),
        )
        self._audit(
            case_id, "system", "engine", "step_completed",
            {"step": join.id, "kind": join.kind.value, "outcome": "done",
             "branches_joined": arrived},
        )
        self._advance(case_id, definition, join.next["done"], branch=parent_branch)

    def _open_instances(
        self,
        case_id: str,
        definition: ProcessDefinition,
        step_def: StepDefinition,
        branch: str | None,
        now: datetime,
    ) -> None:
        """Open one row per element of ``for_each.over`` (ADR-0037 §2). An
        empty list passes the step with ``done`` and an empty result; a
        list beyond ``max`` fails the step - loudly, never silently cut."""
        spec = step_def.for_each or {}
        trigger, outputs = self.step_outputs(case_id)
        context = {"trigger": effective_trigger(trigger),
                   "steps": {k: v["result"] for k, v in outputs.items()},
                   "outputs": {k: v["result"] for k, v in outputs.items()}}
        items = _lookup_path(context, str(spec["over"]))
        missing = items is None  # nothing at the path: a bug, not an empty list
        if items is None:
            items = []
        if not isinstance(items, list):
            items = [items]
        limit = int(spec.get("max", DEFAULT_MAX_INSTANCES))
        stamp = _iso(now)
        if missing or not items or len(items) > limit:
            fine = items == [] and not missing
            outcome = "done" if fine else ("failed" if "failed" in step_def.next else None)
            if outcome is None:
                raise EngineError(
                    f"step {step_def.id!r}: for_each over {spec['over']!r} cannot run"
                    f" ({'nothing at the path' if missing else f'{len(items)} exceed max {limit}'})"
                    " and the step has no 'failed' outcome"
                )
            error = None if fine else (
                f"nothing at {spec['over']!r}" if missing
                else f"{len(items)} instances exceed max {limit}"
            )
            result = {"instances": 0 if fine or missing else len(items), "error": error}
            self.conn.execute(
                "INSERT INTO steps (id, case_id, step_id, kind, status, outcome, result,"
                " started_at, finished_at, branch, instance_index, instance_total)"
                " VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, 0, 0)",
                (uuid.uuid4().hex, case_id, step_def.id, step_def.kind.value, outcome,
                 json.dumps(result), stamp, stamp, branch),
            )
            self._audit(
                case_id, "system", "engine", "step_completed",
                {
                    "step": step_def.id, "kind": step_def.kind.value, "outcome": outcome,
                    "instance": 0, "total": 0,
                    **({"error": result["error"]} if result["error"] else {}),
                },
            )
            self._advance_outcome(case_id, definition, step_def, outcome, branch=branch)
            return
        for index, item in enumerate(items):
            self.conn.execute(
                "INSERT INTO steps (id, case_id, step_id, kind, status, started_at, branch,"
                " instance_index, instance_total, instance_input)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex, case_id, step_def.id, step_def.kind.value,
                    _INITIAL_STEP_STATUS[step_def.kind], stamp, branch, index, len(items),
                    json.dumps({str(spec["as"]): item}, ensure_ascii=False, default=str),
                ),
            )
        self.conn.execute(
            "UPDATE cases SET current_step_id = ?, updated_at = ? WHERE id = ?",
            (step_def.id, stamp, case_id),
        )
        details = {"step": step_def.id, "kind": step_def.kind.value, "instances": len(items)}
        if branch:
            details["branch"] = branch
        self._audit(case_id, "system", "engine", "step_opened", details)

    def step_outputs(self, case_id: str) -> tuple[dict, dict[str, dict]]:
        """The trigger payload and every completed step's ``{outcome,
        result}`` of a case. A multi-instance step appears once, with the
        list of instance results in input order (ADR-0037 §2) and the
        aggregate outcome, plus ``instances`` with the per-instance detail."""
        rows = self.conn.execute(
            "SELECT step_id, kind, outcome, result, instance_index, instance_total FROM steps"
            " WHERE case_id = ? AND status = 'completed' ORDER BY started_at, instance_index",
            (case_id,),
        ).fetchall()
        trigger: dict = {}
        out: dict[str, dict] = {}
        grouped: dict[str, list] = {}
        for row in rows:
            result = json.loads(row["result"]) if row["result"] else None
            if row["kind"] == StepKind.TRIGGER.value:
                trigger = result or {}
            elif row["instance_total"] is not None:
                grouped.setdefault(row["step_id"], []).append(
                    (row["instance_index"], row["outcome"], result, row["instance_total"])
                )
            else:
                out[row["step_id"]] = {"outcome": row["outcome"], "result": result}
        for sid, items in grouped.items():
            items.sort(key=lambda i: i[0])
            total = items[0][3]
            if total == 0:
                out[sid] = {"outcome": items[0][1], "result": [], "instances": []}
                continue
            if len(items) < total:
                continue  # still running: not part of the outputs yet
            outcomes = [i[1] for i in items]
            success = "done"
            aggregate = success
            for o in outcomes:
                if o != success:
                    aggregate = o
                    break
            out[sid] = {
                "outcome": aggregate,
                "result": [i[2] for i in items],
                "instances": [{"index": i[0], "outcome": i[1], "result": i[2]} for i in items],
            }
        return trigger, out

    def _handoff_payload(self, case_id: str, definition: ProcessDefinition, chain_id: str) -> dict:
        """What the successor receives: the original trigger, this case's
        step outputs, and chain bookkeeping. The original trigger is kept at
        the top even across several hops, so a file dropped at the start of
        the chain stays reachable at its end."""
        trigger, completed = self.step_outputs(case_id)
        outputs: dict = {
            sid: entry["result"] for sid, entry in completed.items() if entry["result"] is not None
        }
        for sid, entry in completed.items():
            spec = definition.steps[sid].for_each if sid in definition.steps else None
            if spec and spec.get("collect") and entry["result"] is not None:
                outputs[str(spec["collect"])] = entry["result"]
        trigger = origin_trigger(trigger)  # unwrap: keep the chain's origin on top
        return {
            "source": "chain",
            "chain_id": chain_id,
            "from_case": case_id,
            "from_template": definition.id,
            "trigger": trigger,
            "outputs": outputs,
        }

    def _audit(
        self,
        case_id: str,
        actor_type: str,
        actor_id: str,
        event: str,
        details: dict,
        model_ref: str | None = None,
    ) -> None:
        # Gapless per-case sequence; safe under SQLite's single-writer model.
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM audit_log WHERE case_id = ?",
            (case_id,),
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO audit_log (case_id, seq, at, actor_type, actor_id, event,"
            " details, app_version, model_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                case_id,
                seq,
                _iso(self._now()),
                actor_type,
                actor_id,
                event,
                json.dumps(details, ensure_ascii=False, default=str),
                __version__,
                model_ref,
            ),
        )
