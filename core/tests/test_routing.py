"""Routing per model role (ADR-0035 §3): a briefing names a role, the
instance binds the role to a registry entry, the step calls that entry -
never the instance's cost router, never a live model under ``test``."""

from pathlib import Path

import pytest

from aitmation_core import providers
from aitmation_core.config import Settings
from aitmation_core.model import ModelError, ModelResponse
from aitmation_core.recordings import RecordingClient
from aitmation_core.runtime import Runtime

CORE_TEMPLATES = Path(__file__).resolve().parents[2] / "core" / "templates"
SOLUTION = Path(__file__).resolve().parents[2] / "apps" / "document-basics"

REGISTRY = """
[cheap]
model = "claude-cheap"
provider = "anthropic"
clearance = "confidential"
role = "cheap"
region = "US"

[strong]
model = "claude-strong"
provider = "anthropic"
clearance = "confidential"
role = "strong"
region = "US"

[house]
model = "llama-house"
provider = "ollama"
clearance = "sensitive"
role = "cheap"
region = "local"
local = true
"""


class FakeClient:
    def __init__(self, entry):
        self.entry = entry
        self.clearance = entry.clearance
        self.local = entry.local
        self.calls = 0

    def complete_structured(self, task, payload, schema_hint=None):
        self.calls += 1
        return ModelResponse(
            data={"summary": f"by {self.entry.id}", "anomalies": [], "rules_applied": []},
            confidence=0.95,
            model_ref=self.entry.model,
            tokens_in=10,
            tokens_out=5,
        )


@pytest.fixture
def fake_clients(monkeypatch):
    built: dict[str, FakeClient] = {}

    def client_for(entry, settings):
        if entry.provider != "ollama" and not (settings.data_dir / "secrets.toml").exists():
            return None  # remote entries need a key, like the real builder
        built.setdefault(entry.id, FakeClient(entry))
        return built[entry.id]

    monkeypatch.setattr(providers, "client_for", client_for)
    return built


def _runtime(tmp_path, registry=REGISTRY, configuration="standard", client=None):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "models.toml").write_text(registry, encoding="utf-8")
    settings = Settings(
        data_dir=data,
        templates_dir=CORE_TEMPLATES,
        apps_dir=SOLUTION.parent,
        configuration=configuration,
    )
    return Runtime(settings, model_client=client)


def _read(runtime, tmp_path):
    doc = tmp_path / "letter.txt"
    doc.write_text("Dear team, please find the enclosed invoice.", encoding="utf-8")
    case_id = runtime.engine.start_case(
        "document-read", {"source": "manual", "subject": "x", "path": str(doc)}
    )
    for _ in range(4):
        runtime.tick()
    row = runtime.conn.execute(
        "SELECT status, outcome, result FROM steps WHERE case_id = ? AND step_id = 'read'",
        (case_id,),
    ).fetchone()
    audit = runtime.conn.execute(
        "SELECT details FROM audit_log WHERE case_id = ? AND details LIKE '%model_role%'",
        (case_id,),
    ).fetchone()
    return row, audit


def test_role_bound_step_calls_the_bound_entry_not_the_router(tmp_path, fake_clients):
    router = FakeClient(type("E", (), {"id": "router", "clearance": "confidential",
                                       "local": False, "model": "router"})())
    runtime = _runtime(tmp_path, client=router)
    try:
        bound = runtime.model_role_bindings()["document-basics"]["default"]
        assert bound["model"] == "house"  # local first
        row, audit = _read(runtime, tmp_path)
        assert row["outcome"] == "done"
        assert fake_clients["house"].calls == 1
        assert router.calls == 0
        assert audit is not None and '"model_role": "default"' in audit["details"]
    finally:
        runtime.close()


def test_without_any_model_the_role_bound_step_fails_with_the_reason(tmp_path, fake_clients):
    """A role is a requirement: no bound model means
    the step fails and the diagnosis says which role is missing - never a
    "done" without content that only moves the gap to the next gate."""
    registry = REGISTRY.replace(
        'role = "cheap"\nregion = "local"', 'role = "cheap"\nstatus = "blocked"\nregion = "local"'
    )
    runtime = _runtime(tmp_path, registry=registry)
    try:
        row, _ = _read(runtime, tmp_path)
        assert row["outcome"] == "failed"
        assert "model role 'default'" in row["result"] and "needs secret" in row["result"]
        diag = runtime.conn.execute(
            "SELECT detail FROM diagnostic_events WHERE event = 'agent_step_failed'"
        ).fetchone()
        assert diag and "model role" in diag["detail"]
    finally:
        runtime.close()


def test_unbindable_role_fails_the_step_when_the_instance_has_a_model(tmp_path, fake_clients):
    # The only entry that could serve the role has no secret -> loud failure,
    # no silent fallback to the router.
    registry = REGISTRY.replace("[house]", "[house_unused]").replace(
        'clearance = "sensitive"', 'clearance = "sensitive"\nstatus = "blocked"'
    )
    router = FakeClient(type("E", (), {"id": "router", "clearance": "confidential",
                                       "local": False, "model": "router"})())
    runtime = _runtime(tmp_path, registry=registry, client=router)
    try:
        row, _ = _read(runtime, tmp_path)
        assert row["outcome"] == "failed"
        assert "needs secret" in row["result"]
        assert router.calls == 0
    finally:
        runtime.close()


def test_configuration_may_pin_a_role_to_an_entry(tmp_path, fake_clients, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secrets.toml").write_text('anthropic_api_key = "k"\n', encoding="utf-8")
    runtime = _runtime(tmp_path)
    try:
        runtime.configuration.model_roles["default"] = "strong"
        row, _ = _read(runtime, tmp_path)
        assert row["outcome"] == "done"
        assert fake_clients["strong"].calls == 1
        assert "house" not in fake_clients
    finally:
        runtime.close()


def test_test_configuration_never_calls_a_live_model(tmp_path, fake_clients):
    router = FakeClient(type("E", (), {"id": "router", "clearance": "confidential",
                                       "local": False, "model": "router"})())
    runtime = _runtime(tmp_path, configuration="test", client=router)
    try:
        assert runtime.configuration.replays_models
        row, _ = _read(runtime, tmp_path)
        assert row["outcome"] == "failed"
        assert "live call refused" in row["result"]
        assert not fake_clients and router.calls == 0
    finally:
        runtime.close()


def test_recording_client_serves_every_role(tmp_path, fake_clients):
    client = RecordingClient(tmp_path / "rec", mode="replay")
    runtime = _runtime(tmp_path, configuration="test", client=client)
    try:
        row, _ = _read(runtime, tmp_path)
        assert row["outcome"] == "failed"
        assert "no recording" in row["result"]
        assert not fake_clients
    finally:
        runtime.close()


def test_role_client_errors_are_model_errors(tmp_path, fake_clients):
    runtime = _runtime(tmp_path)
    try:
        definition = runtime.engine.definitions["document-read"]
        config = dict(definition.steps["read"].config)
        config["briefing"] = {**config["briefing"], "model_role": "nonexistent"}
        with pytest.raises(ModelError, match="not declared"):
            runtime._client_for(definition, config)
    finally:
        runtime.close()


def test_a_transient_failure_falls_back_to_the_next_qualifying_entry(tmp_path, fake_clients):
    """ADR-0047 §6: the local entry is first in line; when it is unreachable,
    the next released entry that satisfies the role answers - visibly
    (diagnostic), with the real model_ref in the log. A non-transient
    failure (bad answer, bad key) is not retried elsewhere."""
    from aitmation_core.secrets import save_secret

    runtime = _runtime(tmp_path)
    try:
        save_secret(runtime.settings.data_dir, "anthropic_api_key", "k")
        runtime.reconfigured()
        bound = runtime.model_role_bindings()["document-basics"]["default"]
        assert bound["model"] == "house" and bound["fallbacks"] == ["cheap", "strong"]

        house = fake_clients.get("house") or FakeClient(runtime.registry["house"])
        fake_clients["house"] = house
        calls = {"n": 0}

        def flaky(task, payload, schema_hint=None):
            calls["n"] += 1
            raise ModelError("llama-house: Ollama call failed: connection refused")

        house.complete_structured = flaky  # type: ignore[method-assign]
        row, _audit = _read(runtime, tmp_path)
        assert row["outcome"] == "done", row["result"]
        assert calls["n"] == 1 and fake_clients["cheap"].calls == 1
        log = runtime.conn.execute(
            "SELECT model_ref FROM llm_interactions WHERE status = 'ok'"
        ).fetchone()
        assert log["model_ref"] == "claude-cheap"
        diag = runtime.conn.execute(
            "SELECT detail AS message FROM diagnostic_events WHERE event = 'fallback_used'"
        ).fetchone()
        assert diag is not None and "claude-cheap answered after 1 failure" in diag["message"]

        def broken(task, payload, schema_hint=None):
            raise ModelError("llama-house: response is not valid JSON")

        house.complete_structured = broken  # type: ignore[method-assign]
        before = fake_clients["cheap"].calls
        row, _audit = _read(runtime, tmp_path)
        assert row["outcome"] == "failed" and fake_clients["cheap"].calls == before
    finally:
        runtime.close()
