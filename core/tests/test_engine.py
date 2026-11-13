"""Engine behavior: happy path, gates, timers, audit trail, resumability."""

import pytest

from aitmation_core import db
from aitmation_core.definition import StepKind
from aitmation_core.engine import Engine, EngineError
from conftest import dummy_definition


def _current(engine, case_id):
    return engine.conn.execute(
        "SELECT status, current_step_id, gate_passed_at FROM cases WHERE id = ?",
        (case_id,),
    ).fetchone()


def test_happy_path_to_completion(engine):
    case_id = engine.start_case("dummy", {"payload": 42})

    row = _current(engine, case_id)
    assert (row["status"], row["current_step_id"]) == ("running", "prepare")

    [pending] = engine.pending_steps(StepKind.AGENT_STEP)
    assert pending["step_id"] == "prepare"
    engine.complete_step(
        case_id,
        "prepare",
        "done",
        result={"proposal": "x"},
        model_ref="model-a@1",
        payload={"payload": 42},
    )
    assert _current(engine, case_id)["current_step_id"] == "approve"

    engine.decide_gate(case_id, "approve", "approved", actor_id="alex")
    assert _current(engine, case_id)["gate_passed_at"] is not None

    engine.complete_step(case_id, "export", "done", actor_type="system", actor_id="connector")
    row = _current(engine, case_id)
    assert (row["status"], row["current_step_id"]) == ("completed", None)


def test_rejection_loops_via_timer(engine):
    case_id = engine.start_case("dummy", {})
    engine.complete_step(case_id, "prepare", "done")
    engine.decide_gate(case_id, "approve", "rejected", actor_id="alex")

    # The cool-off timer (0 s) is due immediately and loops back.
    [timer] = engine.due_timers()
    assert timer["step_id"] == "cool_off"
    engine.fire_timer(case_id, "cool_off")
    assert _current(engine, case_id)["current_step_id"] == "prepare"
    # Rejection must not count as a passed gate.
    assert _current(engine, case_id)["gate_passed_at"] is None


def test_undefined_outcome_is_rejected(engine):
    case_id = engine.start_case("dummy", {})
    with pytest.raises(EngineError, match="not defined"):
        engine.complete_step(case_id, "prepare", "banana")


def test_gate_decision_on_agent_step_is_rejected(engine):
    case_id = engine.start_case("dummy", {})
    with pytest.raises(EngineError, match=r"no open step|not a HumanGate"):
        engine.decide_gate(case_id, "prepare", "approved", actor_id="alex")


def test_audit_trail_is_complete_and_versioned(engine):
    case_id = engine.start_case("dummy", {})
    engine.complete_step(case_id, "prepare", "done", model_ref="model-a@1")
    rows = list(
        engine.conn.execute(
            "SELECT seq, event, app_version, model_ref FROM audit_log"
            " WHERE case_id = ? ORDER BY seq",
            (case_id,),
        )
    )
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    events = [r["event"] for r in rows]
    assert events[0] == "case_started"
    assert "step_completed" in events
    assert all(r["app_version"] for r in rows)
    completed = next(r for r in rows if r["event"] == "step_completed")
    assert completed["model_ref"] == "model-a@1"


def test_step_results_are_stored_comparably(engine):
    """ADR-0013: result, model reference and input digest are persisted."""
    case_id = engine.start_case("dummy", {"invoice-ish": "but the core doesn't know"})
    engine.complete_step(
        case_id,
        "prepare",
        "done",
        result={"field": "value"},
        model_ref="model-a@1",
        payload={"in": 1},
    )
    row = engine.conn.execute(
        "SELECT result, model_ref, input_digest FROM steps"
        " WHERE case_id = ? AND step_id = 'prepare'",
        (case_id,),
    ).fetchone()
    assert row["result"] == '{"field": "value"}'
    assert row["model_ref"] == "model-a@1"
    assert len(row["input_digest"]) == 64


def test_restart_resumes_from_database(tmp_path):
    """A fresh engine over the same file continues mid-case (ARCHITECTURE §3)."""
    path = tmp_path / "resume.db"

    conn1 = db.connect(path)
    db.migrate(conn1)
    engine1 = Engine(conn1, {"dummy": dummy_definition()})
    case_id = engine1.start_case("dummy", {"n": 1})
    engine1.complete_step(case_id, "prepare", "done")
    conn1.close()  # simulated shutdown mid-case, gate still open

    conn2 = db.connect(path)
    engine2 = Engine(conn2, {"dummy": dummy_definition()})
    engine2.decide_gate(case_id, "approve", "approved", actor_id="alex")
    engine2.complete_step(case_id, "export", "done", actor_type="system")
    row = conn2.execute("SELECT status FROM cases WHERE id = ?", (case_id,)).fetchone()
    assert row["status"] == "completed"
    conn2.close()
