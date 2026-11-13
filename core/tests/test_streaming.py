# ruff: noqa: E501 - test data
"""ADR-0058: live text. Provider clients stream; a run someone listens to
gets the answer field sentence by sentence through the post-guardrails;
…/run with ``stream`` answers as NDJSON ending in the report."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings
from aitmation_core.model import (
    AnthropicClient,
    ModelResponse,
    OllamaClient,
    OpenAICompatibleClient,
    partial_json_field,
)
from aitmation_core.runtime import Runtime, _AnswerStream
from aitmation_core.web import create_app

try:
    from test_website_chat import OPENING, REPO, WASTE, Desk
except ModuleNotFoundError:  # the website-chat solution lives in the solutions repository
    OPENING = REPO = WASTE = None
    Desk = object

needs_chat = pytest.mark.skipif(Desk is object, reason="needs the website-chat solution")

ANSWER = {"answer": "Das Rathaus ist Montag offen [1]. Mehr unter https://evil.example/x steht nichts. Fragen an max.muster@example.ch bitte. Ende", "cited": [1]}


def _pieces(text: str, size: int = 7) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


def _sse(events: list[dict]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


def test_anthropic_openai_and_ollama_stream_the_same_answer():
    raw = json.dumps({**ANSWER, "confidence": 0.9})
    anthropic = _sse([{"type": "message_start", "message": {"model": "claude-x", "usage": {"input_tokens": 12}}}]
                     + [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": p}} for p in _pieces(raw)]
                     + [{"type": "message_delta", "usage": {"output_tokens": 34}}])
    openai = _sse([{"model": "mistral-small", "choices": [{"delta": {"content": p}}]} for p in _pieces(raw)]
                  + [{"model": "mistral-small", "choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 34}}])
    ollama = b"".join(json.dumps({"model": "gemma", "message": {"content": p}, "done": False}).encode() + b"\n" for p in _pieces(raw)) \
        + json.dumps({"model": "gemma", "done": True, "prompt_eval_count": 12, "eval_count": 34}).encode() + b"\n"
    seen: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen[request.url.path] = body
        payload = {"/v1/messages": anthropic, "/chat/completions": openai, "/api/chat": ollama}[request.url.path]
        return httpx.Response(200, content=payload)

    transport = httpx.MockTransport(handler)
    clients = [AnthropicClient("k", "claude-x", transport=transport),
               OpenAICompatibleClient("k", "mistral-small", base_url="https://x/", transport=transport),
               OllamaClient("gemma", transport=transport)]
    for client in clients:
        got: list[str] = []
        response = client.complete_structured("t", {"q": 1}, {"answer": "a"}, on_text=got.append)
        assert "".join(got) == raw and len(got) > 3
        assert response.data["answer"] == ANSWER["answer"] and response.tokens_in == 12 and response.tokens_out == 34
    assert all(b["stream"] is True for b in seen.values())
    # without on_text nothing streams
    plain = httpx.MockTransport(lambda r: httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": raw}}]}))
    assert OpenAICompatibleClient("k", "m", base_url="https://x/", transport=plain).complete_structured("t", {}).data["cited"] == [1]


def test_a_stream_error_is_a_model_error():
    from aitmation_core.model import ModelError

    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}))
    with pytest.raises(ModelError, match="401"):
        AnthropicClient("k", "claude-x", transport=transport).complete_structured("t", {}, on_text=lambda s: None)


def test_partial_field_and_sentence_release_with_guardrails():
    assert partial_json_field('{"cited": [1], "answer": "Ab\\u00e4', "answer") == "Abä"
    events: list[dict] = []
    stream = _AnswerStream(events.append, "answer", "answer", ["no_external_links", "no_personal_data"])
    for piece in _pieces(json.dumps(ANSWER), 5):
        stream.feed(piece)
    text = "".join(e["delta"] for e in events)
    assert text.startswith("Das Rathaus ist Montag offen [1]. ")
    assert "evil.example" not in text and "max.muster" not in text  # filtered before it went out
    assert "Ende" not in text  # the unfinished last sentence waits for the report
    assert len(events) == 3 and all(e["event"] == "text" and e["step"] == "answer" for e in events)


class StreamingDesk(Desk):
    streams = True

    def complete_structured(self, task, payload, schema_hint=None, on_text=None):
        response = super().complete_structured(task, payload, schema_hint)
        if on_text is not None and "verdict" not in (schema_hint or {}):
            data = {"answer": "Das Rathaus hat Montag bis Freitag offen. Am Donnerstag auch nachmittags. Mehr weiss ich nicht [1]", "cited": [1]}
            for piece in _pieces(json.dumps(data), 9):
                on_text(piece)
            return ModelResponse(data=data, confidence=0.9, model_ref="fake-desk@1", tokens_in=10, tokens_out=10)
        return response


@pytest.fixture()
def live(tmp_path, monkeypatch):
    from aitmation_core import providers

    data = tmp_path / "data"
    data.mkdir()
    (data / "models.toml").write_text(
        '[house]\nmodel = "llama-house"\nprovider = "ollama"\nclearance = "sensitive"\n'
        'role = "cheap"\nregion = "local"\nlocal = true\n', encoding="utf-8")
    desk = StreamingDesk()
    monkeypatch.setattr(providers, "client_for", lambda entry, settings: desk)
    runtime = Runtime(Settings(data_dir=data, templates_dir=Path(REPO) / "core" / "templates", apps_dir=Path(REPO) / "apps"))
    runtime.knowledge.save_space("townhall", title="Gemeinde Townhall", data_class="public")
    runtime.knowledge.add_document("townhall", "rathaus.txt", OPENING.encode())
    runtime.knowledge.add_document("townhall", "abfall.txt", WASTE.encode())
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        yield client, runtime, desk


@needs_chat
def test_run_streams_the_answer_then_reports(live):
    client, _rt, _desk = live
    body = {"payload": {"question": "Wann hat das Rathaus geöffnet?", "space": "townhall"}, "actor": "test", "stream": True}
    with client.stream("POST", "/api/v1/templates/website-chat/run", json=body) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "started" and kinds[-1] == "report" and kinds.count("text") == 2
    live_text = "".join(e["delta"] for e in events if e["event"] == "text")
    assert live_text == "Das Rathaus hat Montag bis Freitag offen. Am Donnerstag auch nachmittags. "
    report = events[-1]
    assert report["status"] == "completed" and report["outputs"]["answer"]["result"]["answer"].endswith("[1]")
    # the check step does not stream (its briefing names no stream field)
    assert {e["step"] for e in events if e["event"] == "text"} == {"answer"}
    # nobody listening: same run, no live text, plain JSON
    plain = client.post("/api/v1/templates/website-chat/run", json={**body, "stream": False}).json()
    assert plain["status"] == "completed"
    # what the run spent on models - the caller counts it against its budget
    assert plain["usage"]["calls"] >= 1 and set(plain["usage"]) == {"calls", "tokens_in", "tokens_out", "chf"}


@needs_chat
def test_stream_with_breakpoint_and_bad_template(live):
    client, _rt, _desk = live
    body = {"payload": {"question": "Wann hat das Rathaus geöffnet?", "space": "townhall"}, "stream": True, "breakpoints": ["check"]}
    with client.stream("POST", "/api/v1/templates/website-chat/run", json=body) as r:
        events = [json.loads(line) for line in r.iter_lines() if line.strip()]
    assert events[-1]["event"] == "report" and events[-1]["status"] == "paused"
    assert client.post("/api/v1/templates/nope/run", json={"stream": True}).status_code == 422
