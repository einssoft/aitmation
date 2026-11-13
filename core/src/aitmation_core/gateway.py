"""The model gateway (ADR-0047): one OpenAI-compatible door through which
other programs - the assistant service, a script, a tool - call the
models this instance has released, under this instance's rules.

What LiteLLM does as a product, this module does with what the core
already has: the registry decides *which* deployments exist and what they
are cleared for (ADR-0017/0027), the legal space says where data of a
class may go, the monthly budget says when paid calls stop (ADR-0029),
the pre-guardrails look at the input (ADR-0038), and every call lands in
the same content trail as a process step (Verwaltung → Protokoll). No
second key store, no second price table, no second log.

Endpoint shape: ``POST /api/v1/gateway/chat/completions`` with the OpenAI
request body (``model``, ``messages``, ``max_tokens``, ``temperature``);
``model`` is a registry entry id or a role (``cheap``, ``strong``, …).
Streaming is not offered - a gateway that logs and prices every answer
needs the whole answer.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass

from . import data_classes, guardrails, legal, model_registry
from .model import ChatResult, ModelClient, ModelError

MAX_MESSAGES = 60
MAX_CHARS = 200_000
MAX_TOKENS_CAP = 8192
ROLES = ("cheap", "strong")


class GatewayError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GatewayRequest:
    model: str
    messages: list[dict]
    max_tokens: int = 1024
    temperature: float = 0.2
    data_class: str = "internal"
    task: str = "gateway"


def parse_request(body: dict, *, data_class: str, task: str) -> GatewayRequest:
    if not isinstance(body, dict):
        raise GatewayError(400, "body must be an object")
    if body.get("stream"):
        raise GatewayError(400, "streaming is not offered by this gateway")
    model = str(body.get("model") or "").strip()
    if not model:
        raise GatewayError(400, "model is required (a registry entry id or a role)")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(400, "messages must be a non-empty list")
    if len(messages) > MAX_MESSAGES:
        raise GatewayError(413, f"more than {MAX_MESSAGES} messages")
    clean: list[dict] = []
    total = 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant"):
            raise GatewayError(400, "every message needs a role of system, user or assistant")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        content = str(content)
        total += len(content)
        clean.append({"role": m["role"], "content": content})
    if total > MAX_CHARS:
        raise GatewayError(413, f"messages exceed {MAX_CHARS} characters")
    try:
        max_tokens = max(1, min(int(body.get("max_tokens") or 1024), MAX_TOKENS_CAP))
        temperature = max(0.0, min(float(body.get("temperature", 0.2)), 2.0))
    except (TypeError, ValueError) as exc:
        raise GatewayError(400, "max_tokens and temperature must be numbers") from exc
    try:
        data_classes.rank(data_class)
    except Exception as exc:
        raise GatewayError(400, f"unknown data class {data_class!r}") from exc
    return GatewayRequest(model, clean, max_tokens, temperature, data_class, task[:60] or "gateway")


def resolve_entry(
    registry: dict[str, model_registry.ModelEntry], name: str
) -> model_registry.ModelEntry:
    """A registry entry by id, or the released entry of a role."""
    entry = registry.get(name)
    if entry is None and name in ROLES:
        entry = model_registry.released(registry, name)
    if entry is None:
        raise GatewayError(404, f"no released model {name!r} in this instance's registry")
    if entry.status != "released":
        raise GatewayError(403, f"model {entry.id!r} is {entry.status}, not released")
    return entry


def admission(entry: model_registry.ModelEntry, data_class: str, country: str | None) -> str | None:
    """Why this call may not go to this deployment: dominance rule and
    legal space, fail-closed like before a process step."""
    if not data_classes.clearance_suffices(entry.clearance, data_class):
        return (
            f"data class {data_class!r} exceeds the clearance {entry.clearance!r} of {entry.id!r}"
        )
    return legal.violation(country, entry, data_class)


def token_ok(given: str, expected: str | None) -> bool:
    if not expected or not given:
        return False
    return secrets.compare_digest(given, expected)


def pre_check(names: list[str], messages: list[dict]) -> tuple[list[str], str | None]:
    """The instance's pre-guardrails over the messages: flags are reported,
    a block or escalation refuses the call."""
    flags: list[str] = []
    for name in names:
        result = guardrails.run(name, {"messages": messages}, "pre")
        if result.action == "flag":
            flags.append(f"{name}: {result.detail}")
        elif result.action in ("block", "escalate"):
            return flags, f"guardrail {name!r} ({result.action}): {result.detail}"
    return flags, None


def call(client: ModelClient, request: GatewayRequest) -> ChatResult:
    complete = getattr(client, "complete_chat", None)
    if complete is None:
        raise GatewayError(501, "this deployment's client has no chat completion")
    try:
        return complete(
            request.messages, max_tokens=request.max_tokens, temperature=request.temperature
        )
    except ModelError as exc:
        raise GatewayError(502, str(exc)) from exc


def openai_response(result: ChatResult, entry_id: str) -> dict:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": entry_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text},
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": result.tokens_in or 0,
            "completion_tokens": result.tokens_out or 0,
            "total_tokens": (result.tokens_in or 0) + (result.tokens_out or 0),
        },
    }


def model_list(registry: dict[str, model_registry.ModelEntry], country: str | None) -> dict:
    """``GET /gateway/models`` in the OpenAI shape, with what callers need
    to pick: role, region, clearance and the class permitted by law."""
    data = []
    for entry in registry.values():
        if entry.status != "released":
            continue
        data.append(
            {
                "id": entry.id,
                "object": "model",
                "owned_by": entry.provider,
                "role": entry.role,
                "model": entry.model,
                "region": entry.region,
                "clearance": entry.clearance,
                "permitted_up_to": legal.permitted_up_to(country, entry),
                "local": entry.local,
            }
        )
    return {"object": "list", "data": data}
