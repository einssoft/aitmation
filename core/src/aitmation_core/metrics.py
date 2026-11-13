"""Key figures ("Kennzahlen", ADR-0029) - aggregation over the trails that
already exist, no new event format.

Three questions an owner asks, in the order they ask them:

* What does it cost? Model calls and frozen CHF from ``llm_interactions``,
  per template, **per case** ("what does one processed document cost"),
  and per model x task (the basis for choosing models).
* Does the automation still fit? Gate decisions from the audit log, per
  template and gate, with the share of approving outcomes - a falling
  share is the early warning that the templates drifted.
* How much is flowing? Cases started, completed and failed.

A monthly budget (instance setting, CHF) turns the first question into a
guard: at 80 % a warning, at 100 % either a warning or - with
``budget_stop`` - a refusal of paid model calls (local models keep
running). Nothing here writes; the budget check is read by the runtime.

Timestamps in the trails are ISO-8601 UTC with a ``Z`` suffix, so a plain
string comparison against an ISO lower bound is exact.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

PERIODS = (7, 30, 90, 0)  # days; 0 = everything
WARN_RATIO = 0.8


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _since(days: int, now: datetime) -> str | None:
    if days <= 0:
        return None
    return _iso(now - timedelta(days=days))


def month_start(now: datetime) -> str:
    return _iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def _round(value: float | None, digits: int = 4) -> float:
    return round(float(value or 0.0), digits)


def month_cost(conn: sqlite3.Connection, now: datetime | None = None) -> float:
    """CHF spent on model calls since the first of the current month."""
    now = now or datetime.now(UTC)
    row = conn.execute(
        "SELECT COALESCE(SUM(kosten_chf), 0) AS chf FROM llm_interactions WHERE at >= ?",
        (month_start(now),),
    ).fetchone()
    return float(row["chf"] if row else 0.0)


def budget_state(
    conn: sqlite3.Connection,
    monthly_budget_chf: float,
    budget_stop: bool,
    now: datetime | None = None,
) -> dict:
    """``ok`` | ``warning`` (>= 80 %) | ``exceeded`` (>= 100 %); ``none``
    when no budget is set. ``stop`` says whether paid calls are refused
    right now."""
    spent = month_cost(conn, now)
    budget = float(monthly_budget_chf or 0.0)
    if budget <= 0:
        state = "none"
    elif spent >= budget:
        state = "exceeded"
    elif spent >= budget * WARN_RATIO:
        state = "warning"
    else:
        state = "ok"
    return {
        "monthly_budget_chf": budget,
        "budget_stop": bool(budget_stop),
        "spent_chf": _round(spent),
        "ratio": _round(spent / budget, 3) if budget > 0 else None,
        "state": state,
        "stop": bool(budget_stop) and state == "exceeded",
    }


def _where(since: str | None, column: str = "at") -> tuple[str, tuple]:
    if since is None:
        return "", ()
    return f" WHERE {column} >= ?", (since,)


def llm_figures(conn: sqlite3.Connection, since: str | None) -> dict:
    where, args = _where(since)
    total = conn.execute(
        "SELECT COUNT(*) AS calls,"
        " SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,"
        " COALESCE(SUM(tokens_in), 0) AS tokens_in,"
        " COALESCE(SUM(tokens_out), 0) AS tokens_out,"
        " COALESCE(SUM(kosten_chf), 0) AS cost_chf,"
        " AVG(duration_ms) AS duration_ms"
        f" FROM llm_interactions{where}",
        args,
    ).fetchone()
    # Per template: calls join their case; calls without a case (OCR vision
    # escalation, tests) are grouped under "-".
    per_template = conn.execute(
        "SELECT COALESCE(c.template_id, '-') AS template_id,"
        " COUNT(*) AS calls,"
        " SUM(CASE WHEN l.status = 'error' THEN 1 ELSE 0 END) AS errors,"
        " COALESCE(SUM(l.kosten_chf), 0) AS cost_chf,"
        " AVG(l.duration_ms) AS duration_ms,"
        " COUNT(DISTINCT l.case_id) AS cases"
        " FROM llm_interactions l LEFT JOIN cases c ON c.id = l.case_id"
        f"{_where(since, 'l.at')[0]}"
        " GROUP BY COALESCE(c.template_id, '-') ORDER BY cost_chf DESC, calls DESC",
        args,
    ).fetchall()
    per_model_task = conn.execute(
        "SELECT COALESCE(model_ref, '-') AS model_ref, task, COUNT(*) AS calls,"
        " SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,"
        " COALESCE(SUM(tokens_in), 0) AS tokens_in,"
        " COALESCE(SUM(tokens_out), 0) AS tokens_out,"
        " COALESCE(SUM(kosten_chf), 0) AS cost_chf, AVG(duration_ms) AS duration_ms"
        f" FROM llm_interactions{where}"
        " GROUP BY COALESCE(model_ref, '-'), task ORDER BY cost_chf DESC, calls DESC",
        args,
    ).fetchall()
    per_class = conn.execute(
        "SELECT COALESCE(datenklasse, '-') AS data_class, COUNT(*) AS calls,"
        " COALESCE(SUM(kosten_chf), 0) AS cost_chf"
        f" FROM llm_interactions{where}"
        " GROUP BY COALESCE(datenklasse, '-') ORDER BY calls DESC",
        args,
    ).fetchall()
    return {
        "calls": int(total["calls"] or 0),
        "errors": int(total["errors"] or 0),
        "tokens_in": int(total["tokens_in"] or 0),
        "tokens_out": int(total["tokens_out"] or 0),
        "cost_chf": _round(total["cost_chf"]),
        "duration_ms": int(total["duration_ms"] or 0),
        "per_template": [
            {
                "template_id": r["template_id"],
                "calls": int(r["calls"]),
                "errors": int(r["errors"] or 0),
                "cost_chf": _round(r["cost_chf"]),
                "duration_ms": int(r["duration_ms"] or 0),
                "cases": int(r["cases"] or 0),
                # The management number: model cost per case of this template.
                "cost_per_case_chf": _round(r["cost_chf"] / r["cases"]) if r["cases"] else None,
            }
            for r in per_template
        ],
        "per_model_task": [
            {
                "model_ref": r["model_ref"],
                "task": r["task"],
                "calls": int(r["calls"]),
                "errors": int(r["errors"] or 0),
                "tokens_in": int(r["tokens_in"] or 0),
                "tokens_out": int(r["tokens_out"] or 0),
                "cost_chf": _round(r["cost_chf"]),
                "duration_ms": int(r["duration_ms"] or 0),
            }
            for r in per_model_task
        ],
        "per_data_class": [
            {
                "data_class": r["data_class"],
                "calls": int(r["calls"]),
                "cost_chf": _round(r["cost_chf"]),
            }
            for r in per_class
        ],
    }


def gate_figures(
    conn: sqlite3.Connection, since: str | None, approving: dict[tuple[str, str], set[str]]
) -> list[dict]:
    """Decisions at human gates per template and gate. ``approving`` maps
    ``(template_id, step_id)`` to the gate's approving outcomes (from the
    loaded definitions); unknown gates count ``approved`` as approving."""
    rows = conn.execute(
        "SELECT c.template_id, a.details FROM audit_log a JOIN cases c ON c.id = a.case_id"
        f" WHERE a.event = 'step_completed'{' AND a.at >= ?' if since else ''}",
        (since,) if since else (),
    ).fetchall()
    counts: dict[tuple[str, str], dict] = {}
    for r in rows:
        try:
            details = json.loads(r["details"] or "{}")
        except ValueError:
            continue
        if details.get("kind") != "human_gate":
            continue
        key = (r["template_id"], str(details.get("step")))
        bucket = counts.setdefault(key, {"decisions": 0, "approved": 0, "outcomes": {}})
        outcome = str(details.get("outcome"))
        bucket["decisions"] += 1
        bucket["outcomes"][outcome] = bucket["outcomes"].get(outcome, 0) + 1
        if outcome in approving.get(key, {"approved"}):
            bucket["approved"] += 1
    result = []
    for (template_id, step_id), b in sorted(counts.items()):
        result.append(
            {
                "template_id": template_id,
                "gate": step_id,
                "decisions": b["decisions"],
                "approved": b["approved"],
                "approved_share": _round(b["approved"] / b["decisions"], 3),
                "outcomes": b["outcomes"],
            }
        )
    return result


def briefing_figures(
    conn: sqlite3.Connection, since: str | None, approving: dict[tuple[str, str], set[str]]
) -> list[dict]:
    """How people judged the work of each AI step, per version of its
    prompt. Every decision a person takes at a gate is a verdict on the
    AI steps that ran before it in the same chain (since the previous
    gate); counted per ``(template, step, prompt_hash)`` this shows
    whether a changed briefing is approved more or less often than the
    one before. Decisions taken by a standing mandate are not verdicts
    and are left out. Reads the audit log only - no content."""
    rows = conn.execute(
        "SELECT COALESCE(c.chain_id, c.id) AS chain, c.template_id, a.at, a.actor_type,"
        " a.details, a.model_ref FROM audit_log a JOIN cases c ON c.id = a.case_id"
        f" WHERE a.event = 'step_completed'{' AND a.at >= ?' if since else ''}"
        " ORDER BY chain, a.at, a.id",
        (since,) if since else (),
    ).fetchall()
    versions: dict[tuple[str, str, str], dict] = {}
    waiting: list[tuple[str, str, str]] = []  # AI steps not yet judged in this chain
    chain = None
    for r in rows:
        if r["chain"] != chain:
            chain, waiting = r["chain"], []
        try:
            details = json.loads(r["details"] or "{}")
        except ValueError:
            continue
        kind = details.get("kind")
        if kind == "agent_step":
            digest = str(details.get("prompt_hash") or "")
            key = (r["template_id"], str(details.get("step")), digest)
            bucket = versions.setdefault(
                key,
                {"decisions": 0, "approved": 0, "runs": 0, "first_seen": r["at"],
                 "last_seen": r["at"], "models": set()},
            )
            bucket["runs"] += 1
            bucket["last_seen"] = r["at"]
            if r["model_ref"]:
                bucket["models"].add(r["model_ref"])
            if key not in waiting:
                waiting.append(key)
        elif kind == "human_gate":
            if r["actor_type"] == "human":
                gate = (r["template_id"], str(details.get("step")))
                approved = str(details.get("outcome")) in approving.get(gate, {"approved"})
                for key in waiting:
                    versions[key]["decisions"] += 1
                    versions[key]["approved"] += int(approved)
            waiting = []
    steps: dict[tuple[str, str], list[dict]] = {}
    for (template_id, step_id, digest), b in versions.items():
        steps.setdefault((template_id, step_id), []).append(
            {
                "prompt_hash": digest,
                "first_seen": b["first_seen"],
                "last_seen": b["last_seen"],
                "runs": b["runs"],
                "decisions": b["decisions"],
                "approved": b["approved"],
                "approved_share": _round(b["approved"] / b["decisions"], 3)
                if b["decisions"] else None,
                "models": sorted(b["models"]),
            }
        )
    result = []
    for (template_id, step_id), listed in sorted(steps.items()):
        listed.sort(key=lambda v: v["first_seen"])
        result.append({"template_id": template_id, "step": step_id, "versions": listed})
    return result


def case_figures(conn: sqlite3.Connection, since: str | None) -> dict:
    where, args = _where(since, "created_at")
    row = conn.execute(
        "SELECT COUNT(*) AS started,"
        " SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,"
        " SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,"
        " SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running"
        f" FROM cases{where}",
        args,
    ).fetchone()
    per_template = conn.execute(
        "SELECT template_id, COUNT(*) AS started,"
        " SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,"
        " SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed"
        f" FROM cases{where} GROUP BY template_id ORDER BY started DESC",
        args,
    ).fetchall()
    return {
        "started": int(row["started"] or 0),
        "completed": int(row["completed"] or 0),
        "failed": int(row["failed"] or 0),
        "running": int(row["running"] or 0),
        "per_template": [
            {
                "template_id": r["template_id"],
                "started": int(r["started"]),
                "completed": int(r["completed"] or 0),
                "failed": int(r["failed"] or 0),
            }
            for r in per_template
        ],
    }


def security_figures(conn: sqlite3.Connection, since: str | None) -> dict:
    """Guardrail outcomes (ADR-0038 §2): flagged, redacted, blocked - per
    guardrail name, from the diagnosis trail; plus refused calls (budget,
    call cap)."""
    where, params = _where(since)
    rows = conn.execute(
        "SELECT event, COUNT(*) AS n FROM diagnostic_events"
        f" WHERE source IN ('guardrail', 'security', 'budget') {where.replace('WHERE', 'AND')}"
        " GROUP BY event ORDER BY n DESC",
        params,
    ).fetchall()
    per_guardrail: list[dict] = []
    totals = {"flagged": 0, "redacted": 0, "blocked": 0, "refused": 0}
    for r in rows:
        event = str(r["event"])
        name, _sep, action = event.partition(":")
        n = int(r["n"])
        if action == "flag":
            totals["flagged"] += n
        elif action == "redact":
            totals["redacted"] += n
        elif action in ("block", "escalate"):
            totals["blocked"] += n
        elif event in ("call_refused", "call_cap"):
            totals["refused"] += n
            action = "refused"
        per_guardrail.append({"guardrail": name, "action": action or event, "count": n})
    return {**totals, "per_guardrail": per_guardrail}


def overview(
    conn: sqlite3.Connection,
    days: int,
    *,
    approving: dict[tuple[str, str], set[str]] | None = None,
    monthly_budget_chf: float = 0.0,
    budget_stop: bool = False,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    days = days if days in PERIODS else 30
    since = _since(days, now)
    return {
        "days": days,
        "since": since,
        "llm": llm_figures(conn, since),
        "gates": gate_figures(conn, since, approving or {}),
        "briefings": briefing_figures(conn, since, approving or {}),
        "cases": case_figures(conn, since),
        "budget": budget_state(conn, monthly_budget_chf, budget_stop, now),
        "security": security_figures(conn, since),
    }
