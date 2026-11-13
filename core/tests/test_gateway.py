"""The model gateway (ADR-0047): OpenAI-compatible completions through
the instance's registry, data-class and legal rules, budget and log."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aitmation_core import providers
from aitmation_core.config import Settings
from aitmation_core.model import AnthropicClient, OllamaClient, OpenAICompatibleClient
from aitmation_core.runtime import Runtime
from aitmation_core.secrets import save_secret
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"

REGISTRY = """
[house]
model = "llama-house"
provider = "ollama"
clearance = "sensitive"
role = "cheap"
region = "local"
local = true

[cloud]
model = "claude-haiku-5-5"
provider = "anthropic"
clearance = "internal"
role = "strong"
region = "US"
cost_in_chf = 1.0
cost_out_chf = 5.0
"""


class Backends:
    """Fake Ollama and Anthropic behind one transport."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append((request.url.host, body))
        if request.url.path == "/api/chat":
            return httpx.Response(
                200,
                json={
                    "model": "llama-house",
                    "message": {"role": "assistant", "content": "Hallo aus dem Haus."},
                    "prompt_eval_count": 12,
                    "eval_count": 5,
                    "done_reason": "stop",
                },
            )
        if request.url.path == "/v1/messages":
            return httpx.Response(
                200,
                json={
                    "model": "claude-haiku-5-5",
                    "content": [{"type": "text", "text": "Hello from the cloud."}],
                    "usage": {"input_tokens": 1000, "output_tokens": 100},
                    "stop_reason": "end_turn",
                },
            )
        return httpx.Response(500, text="unexpected")


@pytest.fixture()
def gateway(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "models.toml").write_text(REGISTRY, encoding="utf-8")
    backends = Backends()
    transport = httpx.MockTransport(backends.handler)

    def client_for(entry, settings):
        if entry.provider == "ollama":
            return OllamaClient(entry.model, transport=transport, clearance=entry.clearance)
        return AnthropicClient("key", entry.model, transport=transport, clearance=entry.clearance)

    monkeypatch.setattr(providers, "client_for", client_for)
    runtime = Runtime(
        Settings(data_dir=data, templates_dir=CORE_TEMPLATES, apps_dir=tmp_path / "none")
    )
    save_secret(data, "gateway_token", "gw-secret")
    with TestClient(
        create_app(runtime, model_factory=lambda s: None, auth_secret="aa" * 16)
    ) as client:
        yield client, runtime, backends


AUTH = {"authorization": "Bearer gw-secret"}


def test_models_and_a_completion_through_the_local_model(gateway):
    client, runtime, backends = gateway
    assert client.get("/api/v1/gateway/models").status_code == 401
    models = client.get("/api/v1/gateway/models", headers=AUTH).json()
    assert {m["id"] for m in models["data"]} == {"house", "cloud"}
    r = client.post(
        "/api/v1/gateway/chat/completions",
        headers=AUTH,
        json={
            "model": "cheap",
            "messages": [
                {"role": "system", "content": "Kurz."},
                {"role": "user", "content": "Hallo?"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["object"] == "chat.completion" and out["model"] == "house"
    assert out["choices"][0]["message"]["content"] == "Hallo aus dem Haus."
    assert out["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
    _host, body = backends.calls[-1]
    assert (
        body["messages"][0] == {"role": "system", "content": "Kurz."}
        and body["options"]["num_predict"] == 1024
    )
    rows = runtime.conn.execute(
        "SELECT task, model_ref, status, kosten_chf FROM llm_interactions"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("gateway:", "llama-house", "ok", 0.0)]


def test_data_class_and_budget_rules_apply(gateway):
    client, runtime, _backends = gateway
    # Confidential data may not go to the cloud entry (clearance internal).
    r = client.post(
        "/api/v1/gateway/chat/completions",
        headers={**AUTH, "x-aitmation-data-class": "confidential"},
        json={"model": "cloud", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 403 and "clearance" in r.json()["detail"]
    # Internal data may; the price is frozen from the registry.
    r = client.post(
        "/api/v1/gateway/chat/completions",
        headers={**AUTH, "x-aitmation-task": "assistant"},
        json={"model": "cloud", "messages": [{"role": "user", "content": "x"}], "max_tokens": 50},
    )
    assert r.status_code == 200, r.text
    row = runtime.conn.execute(
        "SELECT task, kosten_chf, tokens_in FROM llm_interactions ORDER BY at DESC LIMIT 1"
    ).fetchone()
    assert (
        row["task"] == "gateway:assistant"
        and row["kosten_chf"] == pytest.approx(0.0015)
        and row["tokens_in"] == 1000
    )
    # Budget stop: paid calls refused, the local one still answers.
    runtime.budget_config.update({"monthly_budget_chf": 0.001, "budget_stop": True})
    r = client.post(
        "/api/v1/gateway/chat/completions",
        headers=AUTH,
        json={"model": "cloud", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 402
    r = client.post(
        "/api/v1/gateway/chat/completions",
        headers=AUTH,
        json={"model": "house", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 200


def test_request_validation_and_unknown_models(gateway):
    client, _runtime, _backends = gateway
    assert (
        client.post(
            "/api/v1/gateway/chat/completions",
            headers=AUTH,
            json={"model": "nope", "messages": [{"role": "user", "content": "x"}]},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/gateway/chat/completions",
            headers=AUTH,
            json={"model": "house", "messages": []},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/gateway/chat/completions",
            headers=AUTH,
            json={"model": "house", "stream": True, "messages": [{"role": "user", "content": "x"}]},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/gateway/chat/completions",
            headers={"authorization": "Bearer wrong"},
            json={"model": "house", "messages": [{"role": "user", "content": "x"}]},
        ).status_code
        == 401
    )


def test_openai_compatible_chat_client():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "mistral-small",
                "choices": [{"message": {"content": "Salut"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    client = OpenAICompatibleClient(
        "k",
        "mistral-small",
        base_url="https://api.mistral.ai/v1",
        transport=httpx.MockTransport(handler),
    )
    result = client.complete_chat(
        [{"role": "user", "content": [{"type": "text", "text": "Bonjour"}]}],
        max_tokens=10,
        temperature=0.5,
    )
    assert result.text == "Salut" and result.tokens_in == 3 and result.finish_reason == "stop"
    assert (
        seen["body"]["messages"] == [{"role": "user", "content": "Bonjour"}]
        and seen["body"]["max_tokens"] == 10
    )


def test_access_key_locks_the_container_door(tmp_path, monkeypatch):
    """AITMATION_ACCESS_KEY: Basic for browsers, Bearer <password> for
    programs; /healthz stays open; the gateway accepts the same key."""
    import base64

    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "models.toml").write_text(REGISTRY, encoding="utf-8")
    monkeypatch.setenv("AITMATION_ACCESS_KEY", "alex:sehr-geheim")
    monkeypatch.setattr(providers, "client_for", lambda entry, settings: None)
    runtime = Runtime(
        Settings(data_dir=data, templates_dir=CORE_TEMPLATES, apps_dir=tmp_path / "none")
    )
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        assert client.get("/healthz").status_code == 200
        r = client.get("/api/v1/apps")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        basic = {"authorization": "Basic " + base64.b64encode(b"alex:sehr-geheim").decode()}
        assert client.get("/api/v1/apps", headers=basic).status_code == 200
        assert (
            client.get("/api/v1/apps", headers={"authorization": "Bearer sehr-geheim"}).status_code
            == 200
        )
        assert (
            client.get("/api/v1/apps", headers={"authorization": "Bearer falsch"}).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/gateway/models", headers={"authorization": "Bearer sehr-geheim"}
            ).status_code
            == 200
        )
