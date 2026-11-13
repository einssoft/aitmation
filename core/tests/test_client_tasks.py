"""K5: client-executed actions - queue, claim, result, stale reclaim.

The actuator pattern: a SystemAction
with ``exec = "client"`` pauses as a queued task; an external actuator
claims and executes it; the result completes the step through the SAME
engine guard - ADR-0007 stays intact.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings
from aitmation_core.engine import EngineError
from aitmation_core.runtime import Runtime
from aitmation_core.template import parse_template
from aitmation_core.web import create_app

TEMPLATE = """
id = "portal"
version = 1

[steps.intake]
kind = "trigger"
config = { source = "folder" }
next = { fired = "approve" }

[steps.approve]
kind = "human_gate"
next = { approved = "record", rejected = "end" }

[steps.record]
kind = "system_action"
config = { action = "portal_fill", exec = "client", params = { field = "total" } }
next = { done = "end", failed = "end" }
"""


@pytest.fixture()
def runtime(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=tmp_path / "unused",
        client_task_stale_seconds=300,
    )
    rt = Runtime(settings, definitions={"portal": parse_template(TEMPLATE)})
    yield rt
    rt.close()


def _reach_client_step(runtime):
    (runtime.settings.inbox_dir / "f.txt").write_text("x", encoding="utf-8")
    runtime.tick()
    [gate] = runtime.open_gates()
    runtime.decide(gate["case_id"], "approve", "approved", actor_id="alex")
    runtime.tick()  # queues the client task instead of executing anything
    return gate["case_id"]


def test_full_client_task_loop(runtime):
    case_id = _reach_client_step(runtime)
    [task] = runtime.open_client_tasks()
    assert task["action"] == "portal_fill"
    assert task["params"] == {"field": "total"}

    claimed = runtime.claim_client_task(task["id"], actor_id="extension-1")
    assert claimed["params"] == {"field": "total"}
    # Claiming twice is a conflict, not a silent double execution.
    with pytest.raises(EngineError, match="not open"):
        runtime.claim_client_task(task["id"], actor_id="extension-2")
    # Only the claimer may post the result.
    with pytest.raises(EngineError, match="not claimed by"):
        runtime.submit_client_task_result(task["id"], True, {}, actor_id="extension-2")

    runtime.submit_client_task_result(task["id"], True, {"ok": 1}, actor_id="extension-1")
    runtime.tick()  # the result completes the step
    case = runtime.conn.execute("SELECT status FROM cases WHERE id = ?", (case_id,)).fetchone()
    assert case["status"] == "completed"
    row = runtime.conn.execute(
        "SELECT outcome, result FROM steps WHERE step_id = 'record'"
    ).fetchone()
    assert row["outcome"] == "done"
    assert json.loads(row["result"]) == {"ok": 1}


def test_error_result_takes_the_failed_path(runtime):
    _reach_client_step(runtime)
    [task] = runtime.open_client_tasks()
    runtime.claim_client_task(task["id"], actor_id="ext")
    runtime.submit_client_task_result(
        task["id"], False, {"error": "Feld nicht gefunden"}, actor_id="ext"
    )
    runtime.tick()
    row = runtime.conn.execute(
        "SELECT outcome, result FROM steps WHERE step_id = 'record'"
    ).fetchone()
    assert row["outcome"] == "failed"
    assert "Feld nicht gefunden" in row["result"]


def test_stale_claim_reopens(runtime):
    _reach_client_step(runtime)
    [task] = runtime.open_client_tasks()
    runtime.claim_client_task(task["id"], actor_id="tote-extension")
    # Age the claim beyond the stale threshold.
    old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    runtime.conn.execute(
        "UPDATE client_tasks SET claimed_at = ? WHERE id = ?", (old, task["id"])
    )
    runtime.tick()
    assert [t["id"] for t in runtime.open_client_tasks()] == [task["id"]]
    diag = list(runtime.conn.execute("SELECT event FROM diagnostic_events"))
    assert any(r["event"] == "stale_claim_reopened" for r in diag)


def test_client_action_still_respects_adr_0007():
    """exec = 'client' is no back door: an ungated external client action
    is rejected statically like any other SystemAction."""
    from aitmation_core.template import TemplateError

    ungated = TEMPLATE.replace(
        'next = { fired = "approve" }', 'next = { fired = "record" }'
    )
    with pytest.raises(TemplateError, match="ADR-0007"):
        parse_template(ungated)


def test_http_endpoints(runtime):
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        _reach_client_step(runtime)
        [task] = client.get("/client-tasks").json()
        assert task["action"] == "portal_fill"
        resp = client.post(
            f"/client-tasks/{task['id']}/claim",
            content="actor=ext",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200
        assert resp.json()["params"] == {"field": "total"}
        assert client.post(f"/client-tasks/{task['id']}/claim").status_code == 409
        resp = client.post(
            f"/client-tasks/{task['id']}/result",
            json={"ok": True, "result": {"eingetragen": True}, "actor": "ext"},
        )
        assert resp.status_code == 200
        runtime.tick()
        assert client.get("/client-tasks").json() == []
