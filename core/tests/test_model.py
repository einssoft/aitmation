"""Model layer: structured parsing, Anthropic client wire format, router."""

import json

import httpx
import pytest

from aitmation_core.model import AnthropicClient, ModelError, ModelResponse, ModelRouter


class FakeModel:
    def __init__(self, data=None, confidence=1.0, error=False, model_ref="fake@1"):
        self.data = data or {"answer": 42}
        self.confidence = confidence
        self.error = error
        self.model_ref = model_ref
        self.calls = 0

    def complete_structured(self, task, payload, schema_hint=None):
        self.calls += 1
        if self.error:
            raise ModelError("broken output")
        return ModelResponse(
            data={**self.data, "confidence": self.confidence},
            model_ref=self.model_ref,
            confidence=self.confidence,
        )


def test_router_keeps_confident_cheap_answer():
    cheap, strong = FakeModel(confidence=0.9, model_ref="cheap@1"), FakeModel()
    result = ModelRouter(cheap, strong, threshold=0.7).complete_structured("t", {})
    assert result.model_ref == "cheap@1"
    assert strong.calls == 0


def test_router_escalates_on_low_confidence():
    cheap = FakeModel(confidence=0.3, model_ref="cheap@1")
    strong = FakeModel(confidence=0.95, model_ref="strong@1")
    result = ModelRouter(cheap, strong, threshold=0.7).complete_structured("t", {})
    assert result.model_ref == "strong@1"
    assert cheap.calls == 1


def test_router_escalates_on_model_error():
    cheap = FakeModel(error=True)
    strong = FakeModel(confidence=0.8, model_ref="strong@1")
    result = ModelRouter(cheap, strong, threshold=0.7).complete_structured("t", {})
    assert result.model_ref == "strong@1"


def _anthropic_transport(reply_text: str, capture: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        capture["request"] = json.loads(request.content)
        capture["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "model": "claude-test-1",
                "content": [{"type": "text", "text": reply_text}],
            },
        )

    return httpx.MockTransport(handler)


def test_anthropic_client_wire_format_and_parsing():
    capture: dict = {}
    client = AnthropicClient(
        "secret-key",
        "claude-test-1",
        transport=_anthropic_transport('{"amount": 12, "confidence": 0.85}', capture),
    )
    result = client.complete_structured(
        "Extract the amount.", {"text": "total 12"}, schema_hint={"amount": "number"}
    )
    assert result.data["amount"] == 12
    assert result.confidence == 0.85
    assert result.model_ref == "claude-test-1"

    body = capture["request"]
    assert "temperature" not in body  # newer Claude models refuse it
    # Untrusted-data framing: payload is wrapped, system prompt pins rules.
    assert "<input_data>" in body["messages"][0]["content"]
    assert "untrusted DATA" in body["system"]
    assert "amount: number" in body["system"]
    # The API key travels in the header, never in the prompt.
    assert capture["headers"]["x-api-key"] == "secret-key"
    assert "secret-key" not in body["system"]


def test_anthropic_client_rejects_non_json():
    client = AnthropicClient(
        "k", "m", transport=_anthropic_transport("Sure! Here is the result: 42", {})
    )
    with pytest.raises(ModelError, match="not valid JSON"):
        client.complete_structured("t", {})


def test_anthropic_client_http_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(429, json={}))
    client = AnthropicClient("k", "m", transport=transport)
    with pytest.raises(ModelError, match="API call failed"):
        client.complete_structured("t", {})


def test_anthropic_client_tolerates_fences_and_prose_around_the_object():
    fenced = '```json\n{"ok": true, "confidence": 1.0}\n```'
    client = AnthropicClient("k", "m", transport=_anthropic_transport(fenced, {}))
    assert client.complete_structured("t", {}).data == {"ok": True, "confidence": 1.0}

    prose = 'Here is the JSON you asked for:\n\n{"ok": true, "confidence": 0.9}\n\nAnything else?'
    client = AnthropicClient("k", "m", transport=_anthropic_transport(prose, {}))
    assert client.complete_structured("t", {}).data["ok"] is True


def test_invalid_json_and_http_errors_carry_the_raw_provider_answer():
    client = AnthropicClient(
        "k", "m", transport=_anthropic_transport("Sure! Here is the result: 42", {})
    )
    with pytest.raises(ModelError) as info:
        client.complete_structured("t", {})
    assert info.value.raw == "Sure! Here is the result: 42"

    body = {"type": "error", "error": {"type": "not_found_error", "message": "model: nope"}}
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json=body))
    client = AnthropicClient("k", "nope", transport=transport)
    with pytest.raises(ModelError, match="HTTP 404: model: nope") as info:
        client.complete_structured("t", {})
    assert '"not_found_error"' in (info.value.raw or "")


def test_anthropic_requests_carry_no_temperature():
    """Newer Claude models answer HTTP 400 "temperature is deprecated"."""
    import json as _json

    import httpx as _httpx

    from aitmation_core.model import AnthropicClient as _Client

    bodies: list[dict] = []

    def handler(request):
        bodies.append(_json.loads(request.content))
        content = [{"type": "text", "text": '{"ok": true}'}]
        return _httpx.Response(200, json={"model": "claude-haiku-5-5", "content": content,
                                          "usage": {"input_tokens": 1, "output_tokens": 1}})

    client = _Client("k", "claude-haiku-5-5", transport=_httpx.MockTransport(handler))
    client.complete_structured("t", {})
    client.complete_chat([{"role": "user", "content": "hi"}], temperature=0.7)
    assert bodies and all("temperature" not in b for b in bodies)
