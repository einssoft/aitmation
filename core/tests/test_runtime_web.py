"""End-to-end walking skeleton: folder -> stub agent -> gate -> export.

The decisive test is the template swap: a *different* declarative template
runs through the identical runtime without a single code change - the
criterion for "Konfiguration statt Code" from the implementation plan.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings, load_settings
from aitmation_core.runtime import Runtime
from aitmation_core.template import parse_template
from aitmation_core.web import create_app

REPO_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
REPO_APPS = Path(__file__).resolve().parents[2] / "apps"
APP_DOCTYPES = REPO_APPS / "document-basics" / "doctypes"


@pytest.fixture()
def runtime(tmp_path, house_model):
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=REPO_TEMPLATES, apps_dir=REPO_APPS
    )
    rt = Runtime(settings)
    yield rt
    rt.close()


@pytest.fixture()
def client(runtime):
    with TestClient(create_app(runtime)) as client:
        yield client


def _drop_file(runtime: Runtime, name: str = "document.txt") -> None:
    (runtime.settings.inbox_dir / name).write_text("payload", encoding="utf-8")


def test_settings_env_wins_over_file(tmp_path, monkeypatch):
    data_dir = tmp_path / "d"
    data_dir.mkdir()
    (data_dir / "config.toml").write_text('port = 1111\nhost = "0.0.0.0"\n', encoding="utf-8")
    monkeypatch.setenv("AITMATION_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AITMATION_PORT", "2222")
    settings = load_settings()
    assert settings.port == 2222  # env wins
    assert settings.host == "0.0.0.0"  # file value survives where env is silent
    assert settings.db_path == data_dir / "aitmation.db"


def _categorize_first_gate(runtime, category="letter"):
    """Walk the offline intake: stub classifier -> sort-out gate -> category."""
    [gate] = runtime.open_gates()
    assert (gate["template_id"], gate["step_id"]) == ("document-intake", "categorize")
    runtime.categorize(gate["case_id"], "categorize", category, actor_id="alex")
    runtime.tick()  # dispatch starts the chain
    runtime.tick()  # stub agent in document-read -> handoff -> review gate


def test_folder_to_archive_walking_skeleton(runtime):
    _drop_file(runtime)
    runtime.tick()  # ingest -> stub classifier -> sort-out gate is waiting

    # The inbox file was moved into the case file store.
    assert not any(runtime.settings.inbox_dir.iterdir())
    _categorize_first_gate(runtime)

    [gate] = runtime.open_gates()
    assert (gate["template_id"], gate["step_id"]) == ("document-review", "review")
    runtime.decide(gate["case_id"], "review", "approved", actor_id="alex")
    runtime.tick()  # archive system action runs in the successor case

    [archive_dir] = list(runtime.settings.archive_dir.iterdir())
    data = json.loads((archive_dir / "vorgang.json").read_text(encoding="utf-8"))
    # The summary is written while "archive" is still running, so it holds
    # everything up to and including the trigger of the archive case.
    assert any(s["step"] == "intake" for s in data["steps"])
    statuses = {r["status"] for r in runtime.conn.execute("SELECT status FROM cases")}
    assert statuses == {"completed"}


def test_rejection_ends_case_without_archive(runtime):
    _drop_file(runtime)
    runtime.tick()
    _categorize_first_gate(runtime)
    [gate] = runtime.open_gates()
    runtime.decide(gate["case_id"], "review", "rejected", actor_id="alex")
    runtime.tick()
    assert not any(runtime.settings.archive_dir.iterdir())


SECOND_TEMPLATE = """
id = "second"
version = 1

[steps.arrival]
kind = "trigger"
config = { source = "folder" }
next = { fired = "check" }

[steps.check]
kind = "human_gate"
next = { approved = "publish", rejected = "end" }

[steps.publish]
kind = "system_action"
config = { action = "export" }
next = { done = "end" }
"""


def test_template_swap_without_code_change(tmp_path):
    """Different template, identical code path - the session-4 criterion."""
    settings = Settings(data_dir=tmp_path / "data", templates_dir=tmp_path / "unused")
    runtime = Runtime(settings, definitions={"second": parse_template(SECOND_TEMPLATE)})
    try:
        _drop_file(runtime)
        runtime.tick()
        [gate] = runtime.open_gates()
        assert (gate["template_id"], gate["step_id"]) == ("second", "check")
        runtime.decide(gate["case_id"], "check", "approved", actor_id="alex")
        runtime.tick()
        assert (settings.export_dir / f"{gate['case_id']}.json").exists()
    finally:
        runtime.close()


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"


def test_gates_api_shows_gate_and_accepts_decision(client, runtime):
    _drop_file(runtime)
    runtime.tick()
    # First stop: the sort-out gate offers the loaded categories.
    [gate] = client.get("/api/v1/gates").json()
    assert gate["template_id"] == "document-intake" and gate["gate_kind"] == "categorization"
    assert "letter" in [o["id"] for o in gate["options"]]

    resp = client.post(
        f"/api/v1/cases/{gate['case_id']}/steps/categorize/decide",
        json={"category": "letter", "actor": "alex"},
    )
    assert resp.status_code == 200, resp.text
    runtime.tick()  # dispatch
    runtime.tick()  # stub agent -> review gate

    [gate] = client.get("/api/v1/gates").json()
    assert gate["template_id"] == "document-review" and "approved" in gate["outcomes"]
    resp = client.post(
        f"/api/v1/cases/{gate['case_id']}/steps/review/decide",
        json={"outcome": "approved", "actor": "alex"},
    )
    assert resp.status_code == 200
    assert runtime.open_gates() == []
    # Deciding twice is a conflict, not a silent success.
    resp = client.post(
        f"/api/v1/cases/{gate['case_id']}/steps/review/decide", json={"outcome": "approved"}
    )
    assert resp.status_code == 409


def test_audit_endpoint(client, runtime):
    _drop_file(runtime)
    runtime.tick()
    [gate] = runtime.open_gates()
    entries = client.get(f"/cases/{gate['case_id']}/audit").json()
    assert entries[0]["event"] == "case_started"
    assert all(e["app_version"] for e in entries)
    assert client.get("/cases/nope/audit").status_code == 404
