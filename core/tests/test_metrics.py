"""Key figures and the model budget (ADR-0029): cost per template and per
case, gate shares, case counts, and the budget guard that refuses paid
calls once the month is spent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from aitmation_core import metrics
from aitmation_core.config import Settings
from aitmation_core.model import ModelResponse
from aitmation_core.runtime import Runtime
from aitmation_core.template import parse_template
from aitmation_core.web import create_app

TEMPLATE = """
id = "p"
version = 1
[meta]
title = "Prüfen"
[policy.privacy]
category = "internal"
[steps.a]
kind = "trigger"
config = { source = "folder" }
next = { fired = "work" }
[steps.work]
kind = "agent_step"
config = { task = "analyze" }
next = { done = "review", failed = "end" }
[steps.review]
kind = "human_gate"
config = { approving_outcomes = ["approved"] }
next = { approved = "end", rejected = "end" }
"""


class FakeModel:
    clearance = "confidential"

    def __init__(self):
        self.calls = 0

    def complete_structured(self, task, payload, schema_hint=None):
        self.calls += 1
        return ModelResponse(
            data={"ok": True, "confidence": 0.9},
            model_ref="claude-haiku-4-5",
            confidence=0.9,
            tokens_in=1000,
            tokens_out=200,
        )


def make_runtime(tmp_path, **settings):
    cfg = Settings(data_dir=tmp_path / "data", templates_dir=tmp_path / "unused", **settings)
    model = FakeModel()
    runtime = Runtime(cfg, definitions={"p": parse_template(TEMPLATE)}, model_client=model)
    return runtime, model


def run_case(runtime, name="f.txt"):
    (runtime.settings.inbox_dir / name).write_text("hallo", encoding="utf-8")
    runtime.tick()


def test_overview_aggregates_costs_gates_and_cases(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    try:
        run_case(runtime, "one.txt")
        run_case(runtime, "two.txt")
        gates = runtime.open_gates()
        assert len(gates) == 2
        runtime.decide(gates[0]["case_id"], "review", "approved", actor_id="alex")
        runtime.decide(gates[1]["case_id"], "review", "rejected", actor_id="alex")
        # An old call outside every window but "all".
        old = (datetime.now(UTC) - timedelta(days=200)).isoformat().replace("+00:00", "Z")
        runtime.conn.execute(
            "INSERT INTO llm_interactions (id, at, task, status, kosten_chf, model_ref)"
            " VALUES ('old', ?, 'analyze', 'ok', 5.0, 'claude-haiku-4-5')",
            (old,),
        )
        approving = {("p", "review"): {"approved"}}
        data = metrics.overview(runtime.conn, 30, approving=approving)
        assert data["llm"]["calls"] == 2 and data["llm"]["errors"] == 0
        assert data["llm"]["tokens_in"] == 2000
        [tpl] = data["llm"]["per_template"]
        assert tpl["template_id"] == "p" and tpl["cases"] == 2
        assert tpl["cost_per_case_chf"] == round(tpl["cost_chf"] / 2, 4)
        assert data["llm"]["per_model_task"][0]["model_ref"] == "claude-haiku-4-5"
        assert data["llm"]["per_data_class"] == [
            {"data_class": "internal", "calls": 2, "cost_chf": data["llm"]["cost_chf"]}
        ]
        [gate] = data["gates"]
        assert gate == {
            "template_id": "p",
            "gate": "review",
            "decisions": 2,
            "approved": 1,
            "approved_share": 0.5,
            "outcomes": {"approved": 1, "rejected": 1},
        }
        assert data["cases"]["started"] == 2 and data["cases"]["completed"] == 2
        # "All" includes the old row; 7 days does not.
        assert metrics.overview(runtime.conn, 0)["llm"]["calls"] == 3
        assert metrics.overview(runtime.conn, 7)["llm"]["cost_chf"] == data["llm"]["cost_chf"]
        assert metrics.overview(runtime.conn, 0)["llm"]["cost_chf"] > 5.0
        # Unknown period falls back to 30 days.
        assert metrics.overview(runtime.conn, 11)["days"] == 30
    finally:
        runtime.close()


def test_briefing_versions_carry_the_verdicts_of_the_gate(tmp_path):
    """A person's decision at the gate judges the AI step before it; a new
    prompt version starts its own count, a mandate's decision counts for none."""
    runtime, _ = make_runtime(tmp_path)
    try:
        work = runtime.engine.definitions["p"].steps["work"]

        def decide_all(outcome):
            for gate in runtime.open_gates():
                runtime.decide(gate["case_id"], "review", outcome, actor_id="alex")

        work.config["prompt_hash"] = "v1"
        run_case(runtime, "a.txt")
        run_case(runtime, "b.txt")
        gates = runtime.open_gates()
        runtime.decide(gates[0]["case_id"], "review", "approved", actor_id="alex")
        runtime.decide(gates[1]["case_id"], "review", "rejected", actor_id="alex")
        work.config["prompt_hash"] = "v2"
        run_case(runtime, "c.txt")
        decide_all("approved")
        run_case(runtime, "d.txt")  # ran, not judged yet
        [step] = metrics.overview(runtime.conn, 30, approving={("p", "review"): {"approved"}})[
            "briefings"
        ]
        assert (step["template_id"], step["step"]) == ("p", "work")
        first, second = step["versions"]
        assert (first["prompt_hash"], first["runs"], first["decisions"], first["approved"]) == (
            "v1", 2, 2, 1)
        assert first["approved_share"] == 0.5 and first["models"] == ["claude-haiku-4-5"]
        assert (second["prompt_hash"], second["runs"], second["decisions"]) == ("v2", 2, 1)
        assert second["approved_share"] == 1.0
    finally:
        runtime.close()


def test_budget_state_thresholds(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    try:
        assert metrics.budget_state(runtime.conn, 0, False)["state"] == "none"
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        runtime.conn.execute(
            "INSERT INTO llm_interactions (id, at, task, status, kosten_chf)"
            " VALUES ('a', ?, 't', 'ok', 8.5)",
            (now,),
        )
        state = metrics.budget_state(runtime.conn, 10.0, False)
        assert state["state"] == "warning" and state["ratio"] == 0.85 and not state["stop"]
        assert metrics.budget_state(runtime.conn, 8.0, True)["stop"] is True
        assert metrics.budget_state(runtime.conn, 100.0, True)["state"] == "ok"
        # Last month's spending does not count.
        last_month = (datetime.now(UTC).replace(day=1) - timedelta(days=1)).isoformat()
        runtime.conn.execute(
            "INSERT INTO llm_interactions (id, at, task, status, kosten_chf)"
            " VALUES ('b', ?, 't', 'ok', 50.0)",
            (last_month.replace("+00:00", "Z"),),
        )
        assert metrics.month_cost(runtime.conn) == 8.5
    finally:
        runtime.close()


def test_budget_stop_refuses_paid_calls_and_warns_once(tmp_path):
    runtime, model = make_runtime(tmp_path, monthly_budget_chf=0.001, budget_stop=True)
    try:
        run_case(runtime, "one.txt")  # first call goes through (nothing spent yet)
        assert model.calls == 1
        assert runtime.budget_state()["state"] == "exceeded"
        run_case(runtime, "two.txt")
        assert model.calls == 1  # refused
        rows = list(runtime.conn.execute("SELECT * FROM diagnostic_events"))
        assert any(r["source"] == "budget" and r["event"] == "call_refused" for r in rows)
        assert sum(1 for r in rows if r["event"] == "exceeded") == 1
        # Without stop, the same budget only warns.
        runtime.budget_config["budget_stop"] = False
        run_case(runtime, "three.txt")
        assert model.calls == 2
    finally:
        runtime.close()


def test_metrics_api_and_budget_setting(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=tmp_path / "unused")
    runtime = Runtime(settings, definitions={"p": parse_template(TEMPLATE)}, model_client=None)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        data = client.get("/api/v1/metrics", params={"days": 90}).json()
        assert data["days"] == 90 and data["budget"]["state"] == "none"
        resp = client.put(
            "/api/v1/metrics/budget", json={"monthly_budget_chf": 25, "budget_stop": True}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["monthly_budget_chf"] == 25.0 and resp.json()["state"] == "ok"
        text = (settings.data_dir / "config.toml").read_text(encoding="utf-8")
        assert "monthly_budget_chf = 25.0" in text and "budget_stop = true" in text
        assert (
            client.put("/api/v1/metrics/budget", json={"monthly_budget_chf": -1}).status_code == 422
        )


def test_operations_overview_aggregates_running_recent_errors_and_tokens(tmp_path, house_model):
    """The operations page is a view over existing trails: running cases
    with what they wait on, recent runs, upcoming schedules, device tasks,
    errors of the last day, tokens per day and per process."""
    import shutil
    from pathlib import Path

    import pytest

    repo = Path(__file__).resolve().parents[2]
    apps_dir = tmp_path / "apps"
    source = repo / "docs" / "examples" / "content-pipeline"
    if not source.is_dir():
        pytest.skip("needs the content-pipeline example")
    shutil.copytree(source, apps_dir / "content-pipeline")
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=repo / "core" / "templates", apps_dir=apps_dir
    )
    runtime = Runtime(settings)
    runtime._http_transport = httpx.MockTransport(lambda request: httpx.Response(404))
    client = TestClient(create_app(runtime))
    try:
        case_id = runtime.run_now(
            "make-post", "alex", {"weekday": 5, "slot": 1, "topic": "t", "platforms": ["tiktok"]}
        )
        for _ in range(5):  # table, research (search fails here), draft, gate
            runtime.tick()
        runtime.diag("error", "connector", "http_failed", "boom", case_id=case_id)
        data = client.get("/api/v1/operations?days=7").json()
        assert data["days"] == 7
        (running,) = [r for r in data["running"] if r["case_id"] == case_id]
        assert running["waiting_on"] == "gate" and running["step_id"] == "approve"
        assert running["template_title"]
        assert data["waiting"]["gate"] >= 1
        # Two errors: the search that found no engine here, and ours.
        assert data["errors_24h"]["errors"] == 2
        assert data["errors_24h"]["last"][0]["event"] == "http_failed"
        assert data["errors_24h"]["last"][0]["detail"] == "boom"
        assert len(data["tokens"]["per_day"]) == 7
        assert {s["template_id"] for s in data["schedules"]} >= {"slot-noon", "measure-all"}
        assert data["client_tasks"] == {"open": 0, "claimed": 0}
        assert "budget" in data
    finally:
        runtime.close()
