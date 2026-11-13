"""LLM management (ADR-0027): editable registry with provider, region and
DPA, secrets through the API, Ollama / OpenAI-compatible clients, and the
legal-space rule enforced fail-closed next to the dominance rule."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aitmation_core import legal, providers
from aitmation_core.config import Settings
from aitmation_core.model import ModelError, OllamaClient, OpenAICompatibleClient
from aitmation_core.model_registry import (
    ModelEntry,
    RegistryError,
    entry_from_dict,
    load_registry,
    save_registry,
)
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"


# -- registry ------------------------------------------------------------------


def test_registry_roundtrip_with_provider_region_and_dpa(tmp_path):
    entries = {
        "haiku": entry_from_dict(
            "haiku",
            {
                "model": "claude-haiku-4-5",
                "clearance": "confidential",
                "role": "cheap",
                "region": "US",
                "vision": True,
            },
        ),
        "llama": entry_from_dict(
            "llama",
            {
                "model": "llama3.1:8b",
                "provider": "ollama",
                "clearance": "sensitive",
                "role": "strong",
            },
        ),
        "mistral": entry_from_dict(
            "mistral",
            {
                "model": "mistral-large-latest",
                "provider": "openai",
                "base_url": "https://api.mistral.ai/v1/",
                "clearance": "confidential",
                "role": "strong",
                "region": "EU",
                "dpa": True,
                "status": "experimental",
                "note": "EU-Anbieter",
            },
        ),
    }
    path = tmp_path / "models.toml"
    save_registry(path, entries)
    loaded = load_registry(path)
    assert loaded == entries
    assert loaded["llama"].local and loaded["llama"].region == "local"
    assert loaded["mistral"].base_url == "https://api.mistral.ai/v1"


def test_registry_validation_names_the_problem():
    with pytest.raises(RegistryError, match="unknown provider"):
        entry_from_dict("x", {"model": "m", "provider": "gemini"})
    with pytest.raises(RegistryError, match="unknown region"):
        entry_from_dict("x", {"model": "m", "region": "Mars"})
    with pytest.raises(RegistryError, match="local = true means region"):
        entry_from_dict("x", {"model": "m", "local": True, "region": "EU"})
    with pytest.raises(RegistryError, match="requires local"):
        entry_from_dict("x", {"model": "m", "clearance": "sensitive", "region": "EU"})
    with pytest.raises(RegistryError, match="needs a model name"):
        entry_from_dict("x", {})


# -- legal space ---------------------------------------------------------------


def _remote(region: str, dpa: bool, clearance: str = "confidential") -> ModelEntry:
    return entry_from_dict(
        "m", {"model": "m", "region": region, "dpa": dpa, "clearance": clearance}
    )


def test_legal_space_rules_per_country():
    local = entry_from_dict("l", {"model": "m", "provider": "ollama", "clearance": "sensitive"})
    # Internal data may go anywhere released; confidential is gated.
    assert legal.violation("CH", _remote("US", False), "internal") is None
    assert "may not leave CH" in legal.violation("CH", _remote("US", True), "confidential")
    assert "data processing agreement" in legal.violation(
        "CH", _remote("EU", False), "confidential"
    )
    assert legal.violation("CH", _remote("EU", True), "confidential") is None
    assert legal.violation("CH", _remote("CH", True), "confidential") is None
    assert legal.violation("DE", _remote("CH", True), "confidential") is None
    assert "may not leave DE" in legal.violation("DE", _remote("US", True), "confidential")
    # Local models are never a legal-space problem; unknown country = no rule.
    assert legal.violation("CH", local, "sensitive") is None
    assert legal.violation(None, _remote("US", False), "confidential") is None
    assert legal.permitted_up_to("CH", _remote("US", False)) == "internal"
    assert legal.permitted_up_to("CH", _remote("EU", True)) == "confidential"
    assert legal.permitted_up_to("AT", local) == "sensitive"
    assert legal.permitted_up_to(None, _remote("US", False)) == "confidential"


# -- clients -------------------------------------------------------------------


def test_ollama_client_speaks_json_chat():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3.1:8b"}]})
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "llama3.1:8b",
                "prompt_eval_count": 12,
                "eval_count": 5,
                "message": {"role": "assistant", "content": '{"summary": "ok", "confidence": 0.9}'},
            },
        )

    client = OllamaClient("llama3.1:8b", transport=httpx.MockTransport(handler))
    assert client.check() == ["llama3.1:8b"]
    response = client.complete_structured("Summarize", {"text": "hello"}, {"summary": "one line"})
    assert response.data["summary"] == "ok" and response.confidence == 0.9
    assert response.tokens_in == 12 and response.tokens_out == 5
    assert seen["url"].endswith("/api/chat") and seen["body"]["format"] == "json"
    assert seen["body"]["stream"] is False
    assert "<input_data>" in seen["body"]["messages"][1]["content"]
    assert client.local and client.clearance == "sensitive"


def test_openai_compatible_client_wire_format():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "mistral-large-2411",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '```json\n{"ok": true, "confidence": 1}\n```',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    client = OpenAICompatibleClient(
        "sk-test",
        "mistral-large-latest",
        base_url="https://api.mistral.ai/v1",
        transport=httpx.MockTransport(handler),
    )
    response = client.complete_structured("Ping", {"ping": True})
    assert response.data["ok"] is True and response.model_ref == "mistral-large-2411"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert response.tokens_in == 7

    failing = OpenAICompatibleClient(
        "k", "m", transport=httpx.MockTransport(lambda r: httpx.Response(500, json={}))
    )
    with pytest.raises(ModelError):
        failing.complete_structured("x", {})


# -- API -----------------------------------------------------------------------


@pytest.fixture()
def instance(tmp_path, monkeypatch):
    monkeypatch.delenv("AITMATION_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AITMATION_OPENAI_API_KEY", raising=False)
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        instance_type="company",
    )
    runtime = Runtime(settings)
    with TestClient(create_app(runtime)) as client:
        yield client, runtime


def test_models_api_edits_the_registry_and_rebuilds_clients(instance):
    client, runtime = instance
    rows = {m["id"]: m for m in client.get("/api/v1/models").json()}
    assert set(rows) == {"cheap", "strong"}
    assert rows["cheap"]["provider"] == "anthropic" and rows["cheap"]["region"] == "US"
    assert rows["cheap"]["missing_secret"] == "anthropic_api_key"
    assert rows["cheap"]["permitted_up_to"] == "confidential"  # no country yet
    info = client.get("/api/v1/models/jurisdiction").json()
    assert info["country"] is None and info["jurisdiction"] is None
    assert info["secrets"]["anthropic_api_key"] == "none"

    # Add a local Ollama model, released as the strong role.
    resp = client.put(
        "/api/v1/models/llama",
        json={
            "model": "llama3.1:8b",
            "provider": "ollama",
            "clearance": "sensitive",
            "role": "strong",
            "status": "released",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["local"] and resp.json()["region"] == "local"
    assert resp.json()["missing_secret"] is None
    assert (runtime.settings.models_path).is_file()
    assert "llama" in runtime.registry  # live after reconfigure
    assert runtime.model_client is None  # cheap still needs the Anthropic key

    # Invalid entries are refused and the file stays as it was.
    before = runtime.settings.models_path.read_text(encoding="utf-8")
    bad = client.put("/api/v1/models/bad", json={"model": "x", "region": "Mars"})
    assert bad.status_code == 422 and "region" in bad.json()["detail"]
    assert runtime.settings.models_path.read_text(encoding="utf-8") == before

    assert client.delete("/api/v1/models/llama").json() == {"ok": True}
    assert "llama" not in runtime.registry
    assert client.delete("/api/v1/models/llama").status_code == 404


def test_secrets_api_sets_and_clears_without_revealing(instance):
    client, runtime = instance
    assert client.get("/api/v1/secrets").json()["anthropic_api_key"] == "none"
    resp = client.put("/api/v1/secrets/anthropic_api_key", json={"value": "sk-ant-test"})
    assert resp.status_code == 200 and resp.json()["anthropic_api_key"] == "file"
    assert "sk-ant-test" not in resp.text
    secrets_file = (runtime.settings.data_dir / "secrets.toml").read_text(encoding="utf-8")
    assert "sk-ant-test" in secrets_file
    # With a key the router exists and carries its registry entries.
    assert runtime.model_client is not None
    assert [e.id for e in runtime.model_client.entries] == ["cheap", "strong"]
    resp = client.put("/api/v1/secrets/anthropic_api_key", json={"value": ""})
    assert resp.json()["anthropic_api_key"] == "none"
    assert runtime.model_client is None
    assert client.put("/api/v1/secrets/Nope", json={"value": "x"}).status_code == 404
    # A tool secret (ADR-0041) with a valid name is stored and only reported by origin.
    stored = client.put("/api/v1/secrets/scheduler_token", json={"value": "tok"}).json()
    assert stored["scheduler_token"] == "file"


def test_secret_from_environment_cannot_be_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("AITMATION_ANTHROPIC_API_KEY", "from-env")
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        assert client.get("/api/v1/secrets").json()["anthropic_api_key"] == "env"
        resp = client.put("/api/v1/secrets/anthropic_api_key", json={"value": "x"})
        assert resp.status_code == 409


def test_model_test_endpoint_reports_missing_secret_and_unpulled_model(instance, monkeypatch):
    client, _ = instance
    assert client.post("/api/v1/models/cheap/test").json() == {
        "ok": False,
        "reason": "missing_secret",
        "secret": "anthropic_api_key",
    }
    client.put(
        "/api/v1/models/llama",
        json={
            "model": "llama3.1:8b",
            "provider": "ollama",
            "clearance": "sensitive",
            "role": "strong",
        },
    )

    class FakeOllama:
        def check(self):
            return ["phi3:latest"]

        def complete_structured(self, *a, **k):  # pragma: no cover - not reached
            raise AssertionError

    monkeypatch.setattr(providers, "client_for", lambda entry, settings: FakeOllama())
    result = client.post("/api/v1/models/llama/test").json()
    assert result == {"ok": False, "reason": "model_not_pulled", "available": ["phi3:latest"]}


def test_admin_role_is_required_when_users_exist(tmp_path, monkeypatch):
    import time

    from aitmation_core import auth

    monkeypatch.delenv("AITMATION_ANTHROPIC_API_KEY", raising=False)
    secret = "00" * 32
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    runtime = Runtime(settings)
    app = create_app(runtime, model_factory=lambda s: None, auth_secret=secret)

    def token(sub: str, role: str) -> str:
        return auth.sign(
            bytes.fromhex(secret),
            auth.Principal(sub=sub, role=role, instances=("*",), exp=int(time.time()) + 60),
        )

    member = token("erika", "member")
    admin = token("alex", "admin")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {member}"}
        assert client.get("/api/v1/models", headers=headers).status_code == 200
        assert client.get("/api/v1/secrets", headers=headers).status_code == 403
        assert (
            client.put("/api/v1/models/x", json={"model": "m"}, headers=headers).status_code == 403
        )
        headers = {"Authorization": f"Bearer {admin}"}
        assert client.get("/api/v1/secrets", headers=headers).status_code == 200


# -- runtime enforcement -------------------------------------------------------


class _Router:
    clearance = "confidential"

    def __init__(self, entries):
        self.entries = entries


def test_dominance_and_legal_space_are_enforced_before_a_call(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        instance_type="company",
    )
    runtime = Runtime(settings)
    definition = runtime.engine.definitions["document-read"]
    us = _remote("US", False)
    # No country known: only the dominance rule applies.
    runtime.set_model_client(_Router((us,)))
    assert runtime._dominanz_violation(definition, {"data_class": "confidential"}) is None
    assert "exceeds" in runtime._dominanz_violation(definition, {"data_class": "sensitive"})
    # A Swiss company: confidential data may not go to a US deployment.
    runtime.save_record(
        "company-profile",
        {
            "name": "Einssoft AG",
            "country": "CH",
            "street": "s",
            "postal_code": "8001",
            "city": "Zürich",
        },
        "alex",
    )
    reason = runtime._dominanz_violation(definition, {"data_class": "confidential"})
    assert reason and "legal space" in reason and "may not leave CH" in reason
    assert runtime._dominanz_violation(definition, {"data_class": "internal"}) is None
    runtime.set_model_client(_Router((_remote("EU", True),)))
    assert runtime._dominanz_violation(definition, {"data_class": "confidential"}) is None
    runtime.close()


def test_model_test_call_is_logged_with_the_raw_answer(instance, monkeypatch):
    """The connection test is a model call like any other: request, raw
    answer and error land in llm_interactions, and the detail endpoint
    shows them in full (Alex: a detailed request/response log is a must)."""
    from aitmation_core.model import ModelError

    client, _ = instance

    class Broken:
        def complete_structured(self, *a, **k):
            raise ModelError("m: response is not valid JSON", raw="Sure, here you go: 42")

    monkeypatch.setattr(providers, "client_for", lambda entry, settings: Broken())
    result = client.post("/api/v1/models/cheap/test").json()
    assert result["reason"] == "call_failed"
    assert result["raw"] == "Sure, here you go: 42"

    rows = client.get("/api/v1/log/llm").json()
    assert rows[0]["task"] == "model-test" and rows[0]["status"] == "error"
    assert "input" not in rows[0]  # the list stays metadata-only
    detail = client.get(f"/api/v1/log/llm/{rows[0]['id']}").json()
    assert detail["response"] == {"raw": "Sure, here you go: 42"}
    assert "Reply with" in detail["input"]["task"]
    assert client.get("/api/v1/log/llm/nope").status_code == 404
