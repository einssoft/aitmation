"""Stammdaten-Pflege: document types in the instance's local bundle."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"


@pytest.fixture()
def instance(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        yield client, runtime


def test_bundle_types_are_listed_with_their_source(instance):
    client, _ = instance
    rows = {d["id"]: d for d in client.get("/api/v1/doctypes").json()}
    assert rows["invoice"]["source"] == "document-basics"
    assert rows["invoice"]["overrides"] is None
    assert rows["invoice"]["title"]["fr"] == "Facture"
    assert client.get("/api/v1/templates").json()[0]["id"]


def test_household_adds_a_type_and_overlays_a_bundle_type(instance):
    client, runtime = instance
    body = {
        "title": {"de": "Quittung", "fr": "Reçu"},
        "description": {"de": "Beleg über eine bezahlte Leistung."},
        "features": ["Betrag", "Datum", "Verkäufer"],
        "chain": "document-read",
        "fields": {"amount": "Bezahlter Betrag"},
    }
    resp = client.put("/api/v1/doctypes/receipt", json=body)
    assert resp.status_code == 200, resp.text
    saved = resp.json()
    assert saved["source"] == "local" and saved["overrides"] is None
    assert saved["title"] == {"de": "Quittung", "fr": "Reçu"}
    assert (runtime.settings.doctypes_dir / "receipt.toml").is_file()
    assert "receipt" in runtime.doctypes  # live after reload

    # Overlay: the bundle's invoice gets a new German title and a feature.
    resp = client.put(
        "/api/v1/doctypes/invoice",
        json={"title": {"de": "Lieferantenrechnung"}, "chain": "document-read", "features": ["QR"]},
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "local" and resp.json()["overrides"] == "document-basics"
    assert runtime.doctypes["invoice"].title == "Lieferantenrechnung"

    # Bad input never degrades the running set.
    assert (
        client.put(
            "/api/v1/doctypes/x", json={"title": {"fr": "Sans"}, "chain": "document-read"}
        ).status_code
        == 422
    )
    assert (
        client.put("/api/v1/doctypes/y", json={"title": {"de": "Y"}, "chain": "nope"}).status_code
        == 422
    )
    assert (
        client.put(
            "/api/v1/doctypes/Bad Id", json={"title": {"de": "Y"}, "chain": "document-read"}
        ).status_code
        == 422
    )
    intake = client.put(
        "/api/v1/doctypes/y", json={"title": {"de": "Y"}, "chain": "document-intake"}
    )
    assert intake.status_code == 422 and "intake" in intake.json()["detail"]
    entries = [t["id"] for t in client.get("/api/v1/templates").json() if t["intake"]]
    assert entries == ["document-intake"]
    assert "invoice" in runtime.doctypes and "receipt" in runtime.doctypes

    # Removing the overlay falls back to the bundle; removing the own type ends it.
    back = client.delete("/api/v1/doctypes/invoice")
    assert back.status_code == 200 and back.json()["fallback"] is True
    assert runtime.doctypes["invoice"].title == "Rechnung"
    gone = client.delete("/api/v1/doctypes/receipt")
    assert gone.status_code == 200 and gone.json()["fallback"] is False
    assert "receipt" not in runtime.doctypes
    assert client.delete("/api/v1/doctypes/letter").status_code == 404  # bundle type, not local


def test_template_graph_for_the_workshop(instance):
    client, _ = instance
    graph = client.get("/api/v1/templates/document-intake").json()
    assert graph["intake"] is True and graph["start"] == "intake"
    steps = {s["id"]: s for s in graph["steps"]}
    assert steps["intake"]["kind"] == "trigger"
    assert {e["target"] for e in steps["classify"]["next"]} == {"dispatch", "categorize"}
    assert steps["dispatch"]["config"]["routes"]["invoice"] == "document-read"
    read = client.get("/api/v1/templates/document-read").json()
    door = [e for s in read["steps"] for e in s["next"] if e["kind"] == "process"]
    assert door and door[0]["target"] == "document-review"
    assert client.get("/api/v1/templates/nope").status_code == 404
