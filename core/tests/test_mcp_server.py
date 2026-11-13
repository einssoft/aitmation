"""MCP channel (ADR-0024): the gateway drives the JSON API like the UI does."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings
from aitmation_core.mcp_server import Gateway, GatewayError, build_server
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"


@pytest.fixture()
def gateway(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        instance_id="privat",
        instance_title="Privat",
        instance_type="private",
    )
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        yield Gateway(client, "http://testserver"), runtime


def test_household_and_pending_across_members(gateway):
    gw, runtime = gateway
    hh = gw.household()
    [member] = hh["members"]
    assert member["id"] == "privat" and member["reachable"] and member["open_gates"] == 0
    assert gw.list_pending() == []
    (runtime.settings.inbox_dir / "brief.txt").write_text("Sehr geehrte Damen", encoding="utf-8")
    runtime.tick()
    [gate] = gw.list_pending("all")
    assert gate["instance"] == "privat" and gate["gate_kind"] == "categorization"
    assert gate["document"]["original_name"] == "brief.txt"
    assert {o["id"] for o in gate["options"]} >= {"letter", "invoice"}
    # No file content ever crosses the channel - only the envelope.
    assert "text" not in gate["document"]


def test_decide_passes_the_human_choice_and_audit_shows_it(gateway):
    gw, runtime = gateway
    (runtime.settings.inbox_dir / "brief.txt").write_text("Sehr geehrte Damen", encoding="utf-8")
    runtime.tick()
    [gate] = gw.list_pending()
    with pytest.raises(GatewayError, match="exactly one"):
        gw.decide(gate["case_id"], gate["step_id"])
    with pytest.raises(GatewayError, match="exactly one"):
        gw.decide(gate["case_id"], gate["step_id"], outcome="x", discard=True)
    assert gw.decide(gate["case_id"], gate["step_id"], category="letter", comment="ok") == {
        "ok": True
    }
    assert gw.list_pending() == []
    trail = gw.case_audit(gate["case_id"])
    assert trail["status"] in ("running", "completed")
    assert any(a["actor"] == "human:mcp" for a in trail["audit"])
    # Deciding again is a conflict the assistant gets told about.
    with pytest.raises(GatewayError, match="409"):
        gw.decide(gate["case_id"], gate["step_id"], category="letter")
    running = gw.list_cases(instance="privat")
    assert all(c["instance"] == "privat" for c in running)
    with pytest.raises(GatewayError, match="unknown instance"):
        gw.list_cases(instance="firma-y")


def test_server_registers_the_five_tools(gateway):
    pytest.importorskip("mcp")
    import anyio

    gw, _runtime = gateway
    server = build_server(gw)
    tools = anyio.run(server.list_tools)
    assert {t.name for t in tools} == {
        "household",
        "list_pending",
        "decide",
        "list_cases",
        "case_audit",
    }
    result = anyio.run(server.call_tool, "household", {})
    assert "privat" in str(result)
