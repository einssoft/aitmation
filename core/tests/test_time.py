"""Time as a trigger (Stufe 1.3): schedules, timers until a date, follow-ups."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core import schedule
from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.template import parse_template
from aitmation_core.web import create_app
from conftest import needs_solution

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"

MONTHLY = """
id = "month-close"
version = 1
[meta]
title = "Monatsabschluss"
[steps.start]
kind = "trigger"
config = { source = "schedule", every = "month", day = 1, at = "08:00" }
next = { fired = "check" }
[steps.check]
kind = "human_gate"
config = { }
next = { done = "end" }
"""


def test_schedule_arithmetic_is_local_and_never_retroactive():
    spec = schedule.parse_schedule({"every": "day", "at": "08:00"})
    monday_noon = datetime(2026, 9, 7, 12, 0).astimezone()
    nxt = spec.next_after(monday_noon)
    assert (nxt.day, nxt.hour) == (8, 8)
    weekly = schedule.parse_schedule({"every": "week", "weekday": 5, "at": "17:30"})
    nxt = weekly.next_after(monday_noon)
    assert nxt.isoweekday() == 5 and (nxt.hour, nxt.minute) == (17, 30) and nxt > monday_noon
    monthly = schedule.parse_schedule({"every": "month", "day": 1})
    nxt = monthly.next_after(monday_noon)
    assert (nxt.year, nxt.month, nxt.day) == (2026, 10, 1)
    december = datetime(2026, 12, 15, 9, 0).astimezone()
    assert monthly.next_after(december).year == 2027
    with pytest.raises(schedule.ScheduleError, match="every"):
        schedule.parse_schedule({"every": "fortnight"})
    with pytest.raises(schedule.ScheduleError, match="day must be"):
        schedule.parse_schedule({"every": "month", "day": 31})
    with pytest.raises(schedule.ScheduleError, match="HH:MM"):
        schedule.parse_schedule({"every": "day", "at": "8 Uhr"})


@pytest.fixture()
def runtime(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    rt = Runtime(settings, definitions={"month-close": parse_template(MONTHLY)})
    yield rt
    rt.close()


def test_schedule_fires_once_when_its_slot_passes(runtime):
    # First sight plans the next slot; nothing fires retroactively.
    assert runtime.tick() == 0
    [row] = [s for s in runtime.schedules() if s["template_id"] == "month-close"]
    assert row["every"] == "month" and row["day"] == 1 and row["last_fired_at"] is None
    assert row["next_at"] > datetime.now(UTC).isoformat()
    # Pretend the slot passed (the machine was off for a month).
    past = (datetime.now(UTC) - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    runtime.conn.execute("UPDATE schedules SET next_at = ?", (past,))
    runtime.conn.commit()
    runtime.tick()
    [gate] = runtime.open_gates()
    assert gate["template_id"] == "month-close"
    runtime.tick()
    assert len(runtime.open_gates()) == 1  # once, not per missed day
    [row] = [s for s in runtime.schedules() if s["template_id"] == "month-close"]
    assert row["last_fired_at"] is not None and row["next_at"] > row["last_fired_at"]


def test_schedule_trigger_carries_calendar_facts_and_declared_payload(tmp_path):
    """ADR-0041 example: a scheduled slot hands weekday/hour and its
    declared payload (slot, platforms) to the process it starts."""
    daily = MONTHLY.replace('id = "month-close"', 'id = "slot"').replace(
        'config = { source = "schedule", every = "month", day = 1, at = "08:00" }',
        'config = { source = "schedule", every = "day", at = "13:00",'
        ' payload = { slot = 1, platforms = ["instagram"] } }',
    )
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    rt = Runtime(settings, definitions={"slot": parse_template(daily)})
    try:
        rt.tick()
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        rt.conn.execute("UPDATE schedules SET next_at = ?", (past,))
        rt.conn.commit()
        rt.tick()
        [gate] = rt.open_gates()
        trigger, _ = rt.engine.step_outputs(gate["case_id"])
        assert trigger["source"] == "schedule" and trigger["slot"] == 1
        assert trigger["platforms"] == ["instagram"]
        assert 1 <= trigger["weekday"] <= 7 and 0 <= trigger["hour"] <= 23
    finally:
        rt.close()


def test_timer_until_from_takes_the_date_from_the_trigger(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    rt = Runtime(settings)
    try:
        due = (datetime.now().astimezone() + timedelta(days=2)).replace(microsecond=0)
        case_id = rt.run_now(
            "follow-up", "alex", {"title": "MWST", "due_at": due.isoformat(), "note": "Q3"}
        )
        [item] = rt.deadlines()
        assert item["case_id"] == case_id and item["title"] == "MWST" and item["note"] == "Q3"
        assert datetime.fromisoformat(item["deadline_at"]) == due.astimezone(UTC)
        rt.tick()
        assert rt.open_gates() == []  # not due yet
        # A follow-up for today fires on the next tick and opens the gate.
        rt.run_now("follow-up", "alex", {"title": "Heute", "due_at": "2020-01-01"})
        rt.tick()
        [gate] = rt.open_gates()
        assert gate["template_id"] == "follow-up"
        # A malformed date never blocks: the timer fires at once.
        rt.run_now("follow-up", "alex", {"title": "Kaputt", "due_at": "irgendwann"})
        rt.tick()
        assert len(rt.open_gates()) == 2
    finally:
        rt.close()


def test_run_now_refuses_templates_started_by_documents(tmp_path):
    from aitmation_core.engine import EngineError

    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    rt = Runtime(settings)
    try:
        with pytest.raises(EngineError, match="not started by hand"):
            rt.run_now("document-intake", "alex")
        with pytest.raises(EngineError, match="unknown template"):
            rt.run_now("nope", "alex")
    finally:
        rt.close()


def test_followups_and_schedules_through_the_api(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    rt = Runtime(settings, definitions={"month-close": parse_template(MONTHLY)})
    with TestClient(create_app(rt, model_factory=lambda s: None)) as client:
        rt.tick()
        rows = client.get("/api/v1/schedules").json()
        [row] = [s for s in rows if s["template_id"] == "month-close"]
        assert row["template_id"] == "month-close" and row["at"] == "08:00"
        resp = client.post("/api/v1/schedules/month-close/run", json={"actor": "alex"})
        assert resp.status_code == 200 and resp.json()["case_id"]
        [gate] = client.get("/api/v1/gates").json()
        assert gate["template_id"] == "month-close"
        # follow-up with a note, visible under deadlines and at the gate later
        resp = client.post(
            "/api/v1/followups",
            json={"title": "Steuererklärung", "due_at": "2020-06-30", "note": "Frist Kanton"},
        )
        assert resp.status_code == 200, resp.text
        [item] = client.get("/api/v1/deadlines").json()
        assert item["title"] == "Steuererklärung" and item["template_id"] == "follow-up"
        rt.tick()
        gates = client.get("/api/v1/gates").json()
        followup = next(g for g in gates if g["template_id"] == "follow-up")
        assert followup["subject"] == "Steuererklärung" and followup["note"] == "Frist Kanton"
        assert client.get("/api/v1/deadlines").json() == []
        bad = client.post("/api/v1/followups", json={"title": "x", "due_at": "morgen"})
        assert bad.status_code == 422
        assert client.post("/api/v1/schedules/document-intake/run", json={}).status_code == 422


@needs_solution("appointment-reminders")
def test_timer_offset_shifts_the_moment_from_the_trigger(tmp_path):
    """appointment-reminders: a day before the appointment, two hours after."""
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    rt = Runtime(settings)
    try:
        start = (datetime.now().astimezone() + timedelta(days=3)).replace(microsecond=0)
        case_id = rt.run_now("reminder", "alex", {
            "name": "Anna", "email": "anna@example.ch", "service": "Massage",
            "starts_at": start.isoformat(), "when": "bald",
        })
        rt.engine.decide_gate(case_id, "plan", "plan", actor_id="alex")
        rt.tick()
        row = rt.conn.execute(
            "SELECT deadline_at FROM steps WHERE case_id = ? AND step_id = 'before'", (case_id,)
        ).fetchone()
        day_before = (start - timedelta(days=1)).astimezone(UTC)
        assert datetime.fromisoformat(row["deadline_at"]) == day_before
        assert rt.open_gates() == []  # the reminder is two days away
    finally:
        rt.close()
