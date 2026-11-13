"""The operations view ("Betrieb"): what ran, what is running and where it
waits, what is scheduled next, what failed, and what the models cost -
on one page, aggregated from the trails the instance already keeps
(cases/steps, audit, llm_interactions, diagnostic_events, schedules,
client_tasks). No new tables: an operator's dashboard is a view, never
a second truth (ADR-0029).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from . import metrics

WAIT_KINDS = {
    "human_gate": "gate",
    "timer": "timer",
}


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def running_cases(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Every running case with the step it sits at and why it waits: on a
    person (gate), on the clock (timer), on the device (client task), or
    on the runtime (agent/action pending)."""
    rows = conn.execute(
        "SELECT c.id, c.template_id, c.created_at, c.updated_at, c.current_step_id,"
        " s.kind, s.status, s.started_at"
        " FROM cases c LEFT JOIN steps s ON s.case_id = c.id AND s.step_id = c.current_step_id"
        " AND s.status IN ('pending', 'waiting')"
        " WHERE c.status = 'running' ORDER BY c.updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    seen = set()
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        waiting_on = "runtime"
        if r["kind"] in WAIT_KINDS and r["status"] == "waiting":
            waiting_on = WAIT_KINDS[r["kind"]]
        elif r["kind"] == "system_action":
            task = conn.execute(
                "SELECT status FROM client_tasks WHERE case_id = ? AND step_id = ?"
                " AND status IN ('open', 'claimed') LIMIT 1",
                (r["id"], r["current_step_id"]),
            ).fetchone()
            if task is not None:
                waiting_on = "device"
        out.append(
            {
                "case_id": r["id"],
                "template_id": r["template_id"],
                "step_id": r["current_step_id"],
                "waiting_on": waiting_on,
                "since": r["started_at"] or r["updated_at"],
                "created_at": r["created_at"],
            }
        )
    return out


def recent_cases(conn: sqlite3.Connection, since: str | None, limit: int = 30) -> list[dict]:
    where = " AND closed_at >= ?" if since else ""
    args: tuple = (since, limit) if since else (limit,)
    rows = conn.execute(
        "SELECT id, template_id, status, created_at, closed_at FROM cases"
        f" WHERE status IN ('completed', 'failed', 'cancelled'){where}"
        " ORDER BY closed_at DESC LIMIT ?",
        args,
    ).fetchall()
    out = []
    for r in rows:
        last = conn.execute(
            "SELECT step_id, outcome FROM steps WHERE case_id = ? AND status = 'completed'"
            " ORDER BY finished_at DESC LIMIT 1",
            (r["id"],),
        ).fetchone()
        duration = None
        try:
            a = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            b = datetime.fromisoformat((r["closed_at"] or r["created_at"]).replace("Z", "+00:00"))
            duration = int((b - a).total_seconds())
        except (TypeError, ValueError):
            pass
        out.append(
            {
                "case_id": r["id"],
                "template_id": r["template_id"],
                "status": r["status"],
                "closed_at": r["closed_at"],
                "duration_s": duration,
                "last_step": last["step_id"] if last else None,
                "last_outcome": last["outcome"] if last else None,
            }
        )
    return out


def errors(conn: sqlite3.Connection, since: str | None, limit: int = 30) -> dict:
    """Warnings and errors only - the diagnosis trail also carries info
    events (a saved row, a saved element), which are not trouble."""
    where = " WHERE level IN ('warning', 'error')" + (" AND at >= ?" if since else "")
    args: tuple = (since,) if since else ()
    by = conn.execute(
        "SELECT level, source, event, COUNT(*) AS n FROM diagnostic_events"
        f"{where} GROUP BY level, source, event ORDER BY n DESC LIMIT 20",
        args,
    ).fetchall()
    last = conn.execute(
        "SELECT at, level, source, event, detail, context FROM diagnostic_events"
        f"{where} ORDER BY at DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    total = conn.execute(
        "SELECT SUM(CASE WHEN level = 'error' THEN 1 ELSE 0 END) AS errors,"
        " SUM(CASE WHEN level = 'warning' THEN 1 ELSE 0 END) AS warnings"
        f" FROM diagnostic_events{where}",
        args,
    ).fetchone()
    return {
        "errors": int(total["errors"] or 0),
        "warnings": int(total["warnings"] or 0),
        "by_event": [
            {"level": r["level"], "source": r["source"], "event": r["event"], "count": int(r["n"])}
            for r in by
        ],
        "last": [
            {
                "at": r["at"],
                "level": r["level"],
                "source": r["source"],
                "event": r["event"],
                "detail": (r["detail"] or "")[:300],
                "context": json.loads(r["context"]) if r["context"] else {},
            }
            for r in last
        ],
    }


def tokens_per_day(conn: sqlite3.Connection, days: int, now: datetime) -> list[dict]:
    """Model usage per calendar day (UTC), oldest first, days without a
    call included so a chart has no holes."""
    start = (now - timedelta(days=days - 1)).date()
    rows = conn.execute(
        "SELECT substr(at, 1, 10) AS day, COUNT(*) AS calls,"
        " COALESCE(SUM(tokens_in), 0) AS tokens_in, COALESCE(SUM(tokens_out), 0) AS tokens_out,"
        " COALESCE(SUM(kosten_chf), 0) AS cost_chf,"
        " SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors"
        " FROM llm_interactions WHERE at >= ? GROUP BY day ORDER BY day",
        (start.isoformat(),),
    ).fetchall()
    by_day = {r["day"]: r for r in rows}
    out = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        r = by_day.get(day)
        out.append(
            {
                "day": day,
                "calls": int(r["calls"]) if r else 0,
                "tokens_in": int(r["tokens_in"]) if r else 0,
                "tokens_out": int(r["tokens_out"]) if r else 0,
                "cost_chf": metrics._round(r["cost_chf"]) if r else 0.0,
                "errors": int(r["errors"] or 0) if r else 0,
            }
        )
    return out


def tokens_per_template(conn: sqlite3.Connection, since: str | None) -> list[dict]:
    where = " WHERE l.at >= ?" if since else ""
    args: tuple = (since,) if since else ()
    rows = conn.execute(
        "SELECT COALESCE(c.template_id, '-') AS template_id, COUNT(*) AS calls,"
        " COALESCE(SUM(l.tokens_in), 0) AS tokens_in, COALESCE(SUM(l.tokens_out), 0) AS tokens_out,"
        " COALESCE(SUM(l.kosten_chf), 0) AS cost_chf, COUNT(DISTINCT l.case_id) AS cases"
        " FROM llm_interactions l LEFT JOIN cases c ON c.id = l.case_id"
        f"{where} GROUP BY COALESCE(c.template_id, '-') ORDER BY tokens_in + tokens_out DESC",
        args,
    ).fetchall()
    return [
        {
            "template_id": r["template_id"],
            "calls": int(r["calls"]),
            "tokens_in": int(r["tokens_in"]),
            "tokens_out": int(r["tokens_out"]),
            "cost_chf": metrics._round(r["cost_chf"]),
            "cases": int(r["cases"] or 0),
        }
        for r in rows
    ]


def client_task_counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open,"
        " SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END) AS claimed FROM client_tasks"
    ).fetchone()
    return {"open": int(row["open"] or 0), "claimed": int(row["claimed"] or 0)}


def overview(
    conn: sqlite3.Connection,
    days: int,
    *,
    schedules: list[dict],
    monthly_budget_chf: float = 0.0,
    budget_stop: bool = False,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    days = max(1, min(int(days or 7), 90))
    since = _iso(now - timedelta(days=days))
    day_ago = _iso(now - timedelta(days=1))
    running = running_cases(conn)
    waiting = {"gate": 0, "device": 0, "timer": 0, "runtime": 0}
    for r in running:
        waiting[r["waiting_on"]] = waiting.get(r["waiting_on"], 0) + 1
    upcoming = sorted(
        (s for s in schedules if s.get("next_at")), key=lambda s: str(s.get("next_at"))
    )
    return {
        "days": days,
        "since": since,
        "now": _iso(now),
        "cases": metrics.case_figures(conn, since),
        "running": running,
        "waiting": waiting,
        "recent": recent_cases(conn, since),
        "schedules": upcoming[:20],
        "client_tasks": client_task_counts(conn),
        "errors_24h": errors(conn, day_ago),
        "tokens": {
            "per_day": tokens_per_day(conn, days, now),
            "per_template": tokens_per_template(conn, since),
            "window": {
                k: v
                for k, v in metrics.llm_figures(conn, since).items()
                if k in ("calls", "errors", "tokens_in", "tokens_out", "cost_chf")
            },
        },
        "budget": metrics.budget_state(conn, monthly_budget_chf, budget_stop, now),
    }
