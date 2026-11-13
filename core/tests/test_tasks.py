"""The tasks bundle (ADR-0049): a collection every path can write to, an
intake process the chat starts, and the board reads the rows."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core.apps import validate_app
from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"


@pytest.fixture()
def instance(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        yield client, runtime


def test_bundle_is_valid_and_loaded(instance):
    _client, runtime = instance
    assert validate_app(APPS / "tasks").ok
    assert "tasks" in runtime.records and runtime.records["tasks"].kind == "collection"
    assert "task-intake" in runtime.engine.definitions


def test_chat_creates_a_task_through_the_intake_process(instance):
    client, runtime = instance
    answer = client.post(
        "/api/v1/chat", json={"message": "Erstelle Aufgabe: Offerte Müller nachfassen"}
    ).json()
    assert answer["intent"] == "start" and answer["answer"] == "chat.answer.start", answer
    assert answer["start"]["process_id"] == "task-intake"
    assert answer["start"]["prefill"]["topic"] == "Offerte Müller nachfassen"
    assert answer["start"]["prefill"]["kind"] == "task"
    started = client.post(
        "/api/v1/templates/task-intake/start",
        json={"payload": {**answer["start"]["prefill"], "priority": "high", "due": "2026-12-01"}},
    )
    assert started.status_code == 200, started.text
    runtime.tick()
    rows = client.get("/api/v1/records/tasks/rows").json()
    assert len(rows) == 1
    values = rows[0]["values"]
    assert values["title"] == "Offerte Müller nachfassen" and values["status"] == "open"
    assert (
        values["priority"] == "high"
        and values["due"] == "2026-12-01"
        and values["origin"] == "chat"
    )
    assert "assignee" not in values  # empty optional fields stay out
    # The board moves a task along by saving the row with a new status.
    moved = client.put(
        f"/api/v1/records/tasks/rows/{rows[0]['row_id']}",
        json={"values": {**values, "status": "done"}},
    )
    assert (
        moved.status_code == 200
        and moved.json()["values"]["status"] == "done"
        and moved.json()["version"] == 2
    )


def test_a_bad_due_date_fails_the_intake_visibly(instance):
    client, runtime = instance
    started = client.post(
        "/api/v1/templates/task-intake/start", json={"payload": {"topic": "x", "due": "morgen"}}
    )
    assert started.status_code == 200
    runtime.tick()
    row = runtime.conn.execute(
        "SELECT outcome, result FROM steps WHERE step_id = 'file'"
    ).fetchone()
    assert row["outcome"] == "failed" and "due" in row["result"]
    assert client.get("/api/v1/records/tasks/rows").json() == []
