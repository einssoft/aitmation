"""Chat as an input path (ZIELBILD §9): intents, and deciding stays at the gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core import chat
from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("Was ist offen?", chat.KIND_PENDING),
        ("Zeig mir die Pendenzen", chat.KIND_PENDING),
        ("Qu'est-ce qui est en attente ?", chat.KIND_PENDING),
        ("Che cosa è in sospeso?", chat.KIND_PENDING),
        ("what is pending", chat.KIND_PENDING),
        ("Welche Vorgänge laufen?", chat.KIND_CASES),
        ("mostrami le pratiche", chat.KIND_CASES),
        ("hilfe", chat.KIND_HELP),
        ("Wie ist das Wetter morgen?", chat.KIND_UNKNOWN),
        ("", chat.KIND_UNKNOWN),
    ],
)
def test_intents(message, kind):
    assert chat.parse(message).kind == kind


def test_case_id_and_status_are_picked_up():
    intent = chat.parse("Vorgang 47d90a7bc6f0455290ab365de165a15b bitte")
    assert intent.kind == chat.KIND_CASE
    assert intent.case_id == "47d90a7bc6f0455290ab365de165a15b"
    assert chat.parse("abgeschlossene Vorgänge").status == "completed"
    assert chat.parse("gescheiterte Fälle").status == "failed"


@pytest.fixture()
def instance(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        yield client, runtime


def ask(client, message: str) -> dict:
    resp = client.post("/api/v1/chat", json={"message": message})
    assert resp.status_code == 200
    return resp.json()


def test_chat_answers_with_data_and_the_gate_still_decides(instance):
    client, runtime = instance
    empty = ask(client, "Was ist offen?")
    assert empty["answer"] == "chat.answer.noPending" and empty["gates"] == []

    (runtime.settings.inbox_dir / "brief.txt").write_text("Sehr geehrte Damen", encoding="utf-8")
    runtime.tick()
    answer = ask(client, "Was ist offen?")
    assert answer["answer"] == "chat.answer.pending" and answer["params"]["count"] == 1
    [gate] = answer["gates"]
    # The chat hands over the full gate view - the shell shows the same
    # controls as in Pendenzen; the decision goes through the normal path.
    assert gate["gate_kind"] == "categorization" and gate["options"]
    decided = client.post(
        f"/api/v1/cases/{gate['case_id']}/steps/{gate['step_id']}/decide",
        json={"category": "letter"},
    )
    assert decided.status_code == 200

    cases = ask(client, "Welche Vorgänge gibt es?")
    assert cases["answer"] == "chat.answer.cases" and cases["cases"]
    one = ask(client, f"Vorgang {gate['case_id']}")
    assert one["answer"] == "chat.answer.case"
    assert one["case"]["id"] == gate["case_id"] and one["case"]["audit"]
    assert ask(client, "Vorgang 00000000000000000000000000000000")["answer"] == (
        "chat.answer.caseNotFound"
    )
    assert ask(client, "Wie ist das Wetter?")["answer"] == "chat.answer.unknown"
    assert ask(client, "hilfe")["answer"] == "chat.answer.help"
