"""Web form channel (docs/VERMITTLER.md) and content language (ADR-0022)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings
from aitmation_core.language import detect
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"
TOKEN = "form-secret-123"

FR = (
    "Madame, Monsieur, veuillez trouver ci-joint la facture du mois d'août."
    " Nous vous prions de payer le montant dans les 30 jours. Salutations distinguées."
)
DE = (
    "Sehr geehrte Damen und Herren, anbei erhalten Sie die Rechnung für den Monat"
    " August. Bitte überweisen Sie den Betrag bis Ende September. Freundliche Grüsse."
)


def test_language_heuristic():
    assert detect(DE) == "de"
    assert detect(FR) == "fr"
    assert (
        detect("Gentile signora, in allegato la fattura del mese. Cordiali saluti a voi.") == "it"
    )
    assert (
        detect("Dear Sir, please find the invoice attached and pay the amount. Kind regards.")
        == "en"
    )
    assert detect("12 34 56") is None  # too short / no signal
    assert detect("") is None


@pytest.fixture()
def instance(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    runtime = Runtime(settings)
    app = create_app(runtime, model_factory=lambda s: None, intake_token=TOKEN)
    with TestClient(app) as client:
        yield client, runtime


def test_channel_off_without_token(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        resp = client.post("/api/v1/intake", json={"text": "hallo"})
        assert resp.status_code == 404


def test_form_with_file_and_envelope_reaches_a_gate(instance):
    client, runtime = instance
    body = {
        "from": "kunde@example.ch",
        "sender_name": "K. Kunde",
        "subject": "Facture août",
        "text": "voir pièce jointe",
        "files": [
            {
                "name": "../../facture août.txt",
                "content_base64": base64.b64encode(FR.encode()).decode(),
            }
        ],
    }
    assert client.post("/api/v1/intake", json=body).status_code == 401
    resp = client.post("/api/v1/intake", json=body, headers={"X-Intake-Token": TOKEN})
    assert resp.status_code == 200 and resp.json()["accepted"] == 1
    [name] = resp.json()["files"]
    assert "/" not in name and ".." not in name
    # Sidecar sits next to the file until the intake consumes both.
    inbox = list(runtime.settings.inbox_dir.iterdir())
    assert {p.name for p in inbox} == {name, name + ".envelope.json"}
    runtime.tick()
    assert list(runtime.settings.inbox_dir.iterdir()) == []
    [gate] = client.get("/api/v1/gates").json()
    doc = gate["document"]
    assert doc["source"] == "web" and doc["from"] == "kunde@example.ch"
    assert doc["subject"] == "Facture août" and doc["language"] == "fr"
    assert doc["original_name"].endswith("facture-ao-t.txt") or doc["original_name"].endswith(
        ".txt"
    )


def test_text_only_form_becomes_a_document(instance):
    client, runtime = instance
    resp = client.post(
        "/api/v1/intake",
        json={"from": "a@b.ch", "subject": "Frage zur Rechnung", "text": DE},
        headers={"X-Intake-Token": TOKEN},
    )
    assert resp.status_code == 200
    runtime.tick()
    [gate] = client.get("/api/v1/gates").json()
    assert gate["document"]["language"] == "de"
    assert gate["document"]["original_name"].endswith(".txt")
    empty = client.post(
        "/api/v1/intake", json={"from": "a@b.ch"}, headers={"X-Intake-Token": TOKEN}
    )
    assert empty.status_code == 422


def test_folder_intake_detects_language(instance):
    client, runtime = instance
    (runtime.settings.inbox_dir / "brief.txt").write_text(DE, encoding="utf-8")
    runtime.tick()
    [gate] = client.get("/api/v1/gates").json()
    assert gate["document"]["language"] == "de"


def test_signed_in_upload_lands_in_own_inbox(instance):
    client, runtime = instance
    resp = client.post(
        "/api/v1/upload",
        json={
            "subject": "Notiz",
            "text": DE,
            "files": [
                {"name": "foto.txt", "content_base64": base64.b64encode(DE.encode()).decode()}
            ],
        },
    )
    assert resp.status_code == 200 and resp.json()["accepted"] == 1
    runtime.tick()
    [gate] = client.get("/api/v1/gates").json()
    assert gate["document"]["source"] == "upload" and gate["document"]["channel"] == "ui"
    assert gate["document"]["original_name"] == "foto.txt"
    assert client.post("/api/v1/upload", json={}).status_code == 422
