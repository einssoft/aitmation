# ruff: noqa: E501 - test data
"""ADR-0054: a case driven through within the request (no tick), the tick
leaving such a chain alone, and rules from repeated decisions for any
gate that declares its facts - proposed after three equal decisions,
granted by a person, deciding later gates as a standing mandate."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app
from conftest import needs_solution

pytestmark = needs_solution("appointments")


REPO = Path(__file__).resolve().parents[2]
APPS = REPO / "apps"


def request(service: str, name: str = "Anna Muster") -> dict:
    return {"name": name, "email": "anna@example.ch", "service": service, "wish": "Dienstag",
            "phone": "", "note": "", "origin": "assistant"}


@pytest.fixture()
def instance(tmp_path):
    runtime = Runtime(Settings(data_dir=tmp_path / "data", templates_dir=REPO / "core" / "templates", apps_dir=APPS))
    sent: list = []
    runtime.outbound_mailer = lambda msg: sent.append(msg)
    # the confirm gate of booking-request declares which facts make two decisions "the same"
    runtime.engine.definitions["booking-request"].steps["confirm"].config["rule_fields"] = ["trigger.service"]
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        yield client, runtime, sent


def test_run_goes_as_far_as_it_may_without_a_tick(instance):
    client, rt, _sent = instance
    started = time.monotonic()
    r = client.post("/api/v1/templates/booking-request/run", json={"payload": request("Thai Massage 60 Min.")})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["status"] == "waiting" and report["waiting_on"] == [{"step_id": "confirm", "kind": "human_gate"}]
    assert time.monotonic() - started < 5
    # the gate is an ordinary gate: a person decides, the tick carries on
    rt.engine.decide_gate(report["case_id"], "confirm", "confirmed", actor_id="salon", comment="Di 15:30")
    for _ in range(6):
        rt.tick()
    assert rt.conn.execute("SELECT status FROM cases WHERE id = ?", (report["case_id"],)).fetchone()[0] == "completed"
    # unknown or non-manual templates are refused like a start
    assert client.post("/api/v1/templates/nope/run", json={}).status_code == 422
    # reserved trigger keys never travel from the body
    rep = client.post("/api/v1/templates/booking-request/run", json={"payload": {**request("x"), "file": "/etc/passwd"}}).json()
    trigger, _ = rt.engine.step_outputs(rep["case_id"])
    assert "file" not in trigger


def test_tick_leaves_a_running_chain_alone(instance):
    _client, rt, _sent = instance
    case_id = rt.run_now("booking-request", "test", request("Hot Stone"))
    chain = rt.engine._case(case_id)["chain_id"] or case_id
    rt._sync_chains.add(chain)
    try:
        assert rt.engine.pending_steps(exclude_chains=rt._sync_chains) == [] or all(
            (rt.engine._case(s["case_id"])["chain_id"] or s["case_id"]) != chain
            for s in rt.engine.pending_steps(exclude_chains=rt._sync_chains))
        assert [s["case_id"] for s in rt.engine.pending_steps(chain_id=chain)] in ([], [case_id])
    finally:
        rt._sync_chains.discard(chain)


def test_three_equal_decisions_become_a_proposed_rule(instance):
    client, rt, sent = instance
    service = "Thai Massage 60 Min."
    for i in range(3):
        rep = client.post("/api/v1/templates/booking-request/run", json={"payload": request(service, f"K{i}")}).json()
        assert rep["status"] == "waiting"
        rt.engine.decide_gate(rep["case_id"], "confirm", "confirmed", actor_id="salon", comment="ok")
    # a different service decided once does not count
    other = client.post("/api/v1/templates/booking-request/run", json={"payload": request("Hot Stone")}).json()
    rt.engine.decide_gate(other["case_id"], "confirm", "declined", actor_id="salon", comment="nein")
    for _ in range(8):
        rt.tick()
    proposals = [p for p in client.get("/api/v1/mandates/suggestions").json() if p["action"].startswith("gate:")]
    assert proposals == [{
        "action": "gate:booking-request:confirm", "target": "confirmed",
        "condition": {"trigger.service": service}, "count": 3,
        "template_id": "booking-request", "step_id": "confirm",
        "title": proposals[0]["title"], "reason": "3x gleich entschieden",
    }]
    # the grant is checked: a real gate, one of its outcomes, exactly its facts
    bad = [
        {"action": "gate:booking-request:nope", "target": "confirmed", "condition": {"trigger.service": service}},
        {"action": "gate:booking-request:confirm", "target": "maybe", "condition": {"trigger.service": service}},
        {"action": "gate:booking-request:confirm", "target": "confirmed", "condition": {"trigger.name": "x"}},
    ]
    for body in bad:
        assert client.post("/api/v1/mandates", json={**body, "actor": "alex"}).status_code == 422, body
    granted = client.post("/api/v1/mandates", json={**proposals[0], "actor": "alex"})
    assert granted.status_code == 200, granted.text
    mandate_id = granted.json()["id"]
    assert [p for p in client.get("/api/v1/mandates/suggestions").json() if p["action"].startswith("gate:")] == []
    # from now on the same facts pass the gate without a person - within the run
    sent.clear()
    rep = client.post("/api/v1/templates/booking-request/run", json={"payload": request(service, "Neu")}).json()
    assert rep["status"] == "completed", rep
    assert rep["outputs"]["confirm"]["outcome"] == "confirmed"
    assert rep["outputs"]["confirm"]["result"]["mandate"] == mandate_id
    assert len(sent) == 1 and sent[0]["To"] == "anna@example.ch"
    audit = rt.conn.execute(
        "SELECT actor_type, actor_id FROM audit_log WHERE case_id = ? AND event = 'step_completed'"
        " AND json_extract(details, '$.step') = 'confirm'", (rep["case_id"],)).fetchone()
    assert (audit["actor_type"], audit["actor_id"]) == ("mandate", mandate_id)
    # other facts still wait for a person; after revoking, so does the same
    assert client.post("/api/v1/templates/booking-request/run", json={"payload": request("Hot Stone")}).json()["status"] == "waiting"
    client.post(f"/api/v1/mandates/{mandate_id}/revoke", params={"actor": "alex"})
    assert client.post("/api/v1/templates/booking-request/run", json={"payload": request(service)}).json()["status"] == "waiting"


def test_a_waiting_gate_is_decided_when_the_rule_arrives(instance):
    client, rt, _sent = instance
    rep = client.post("/api/v1/templates/booking-request/run", json={"payload": request("Fussreflex")}).json()
    assert rep["status"] == "waiting"
    client.post("/api/v1/mandates", json={"action": "gate:booking-request:confirm", "target": "confirmed",
                                          "condition": {"trigger.service": "Fussreflex"}, "actor": "alex"})
    for _ in range(6):
        rt.tick()
    assert rt.conn.execute("SELECT status FROM cases WHERE id = ?", (rep["case_id"],)).fetchone()[0] == "completed"


def test_parallel_runs_and_the_tick_do_not_block_each_other(instance):
    import threading

    _client, rt, _sent = instance
    reports: list = []
    errors: list = []

    def run(i: int) -> None:
        try:
            reports.append(rt.run_through("booking-request", "test", request(f"Massage {i}", f"K{i}"), timeout=20))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(6)]
    ticker = threading.Thread(target=lambda: [rt.tick() for _ in range(10)])
    for t in [*threads, ticker]:
        t.start()
    for t in [*threads, ticker]:
        t.join(timeout=30)
    assert not errors and len(reports) == 6
    assert all(r["status"] == "waiting" for r in reports)
    assert rt._sync_chains == set() and rt._runners == set() and rt._released == set()
    # each case reached its gate exactly once
    for r in reports:
        n = rt.conn.execute("SELECT COUNT(*) FROM steps WHERE case_id = ? AND step_id = 'confirm'", (r["case_id"],)).fetchone()[0]
        assert n == 1
