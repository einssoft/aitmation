"""The two log trails next to the audit log.

* ``audit_log`` (engine) stays content-free - the compliance trail.
* ``llm_interactions`` is the content trail: every model call with task,
  input, response, tokens and the CHF cost frozen at call time. It exists
  for debugging and later model comparison, not for compliance - hence
  retention instead of append-only.
* ``diagnostic_events`` are structured warnings/errors with context IDs,
  so a stuck user (or their supporter) sees failures in the admin UI
  instead of needing shell access to logs.

Writes are best-effort by design: a failing log write must never take
down the processing path it observes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .guardrails import redact_for_log

log = logging.getLogger("aitmation.observability")


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now_iso() -> str:
    return _iso(datetime.now(UTC))


def log_llm_interaction(
    conn: sqlite3.Connection,
    *,
    task: str,
    payload: Any,
    status: str,
    case_id: str | None = None,
    step_id: str | None = None,
    response: Any = None,
    model_ref: str | None = None,
    data_class: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_chf: float | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    try:
        conn.execute(
            "INSERT INTO llm_interactions (id, at, case_id, step_id, task, input,"
            # Column names are frozen by the two-release rule (ADR-0013):
            # datenklasse/kosten_chf stay as created in migration 0004.
            " response, model_ref, datenklasse, tokens_in, tokens_out, kosten_chf,"
            " duration_ms, status, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                _now_iso(),
                case_id,
                step_id,
                task,
                # Personal identifiers and secrets never reach the log
                # (ADR-0038 §1.7); the document itself lives with the case.
                json.dumps(redact_for_log(payload), ensure_ascii=False, default=str),
                json.dumps(redact_for_log(response), ensure_ascii=False, default=str)
                if response is not None
                else None,
                model_ref,
                data_class,
                tokens_in,
                tokens_out,
                cost_chf,
                duration_ms,
                status,
                error,
            ),
        )
    except sqlite3.Error:  # pragma: no cover - observability must not break work
        log.exception("failed to write llm interaction log")


def log_diagnostic(
    conn: sqlite3.Connection,
    level: str,
    source: str,
    event: str,
    detail: str = "",
    **context: Any,
) -> None:
    try:
        conn.execute(
            "INSERT INTO diagnostic_events (id, at, level, source, event, detail, context)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                _now_iso(),
                level,
                source,
                event,
                detail,
                json.dumps(context, ensure_ascii=False, default=str) if context else None,
            ),
        )
    except sqlite3.Error:  # pragma: no cover
        log.exception("failed to write diagnostic event")


def cleanup(conn: sqlite3.Connection, retention_days: int) -> int:
    """Delete content-trail rows past retention. Returns rows removed.

    Deliberately NOT applied to the audit_log - that one is append-only
    and permanent. Retention applies only to the content/diagnostic
    trails.
    """
    cutoff = _iso(datetime.now(UTC) - timedelta(days=retention_days))
    removed = conn.execute("DELETE FROM llm_interactions WHERE at < ?", (cutoff,)).rowcount
    removed += conn.execute("DELETE FROM diagnostic_events WHERE at < ?", (cutoff,)).rowcount
    return removed
