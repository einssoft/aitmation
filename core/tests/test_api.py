"""JSON API (/api/v1) for the shell and the MCP channel; multi-addressee delivery."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core.addressees import Addressee, save_addressee
from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
MAILROOM = REPO / "core" / "mailroom"
APPS = REPO / "apps"


@pytest.fixture()
def instance(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        instance_id="firma-x",
        instance_title="Firma X",
        instance_type="company",
    )
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        yield client, runtime


def _drop(runtime, name="doc.txt", text="Sehr geehrte Damen"):
    (runtime.settings.inbox_dir / name).write_text(text, encoding="utf-8")


def test_instance_and_household_single_member(instance):
    client, _runtime = instance
    info = client.get("/api/v1/instance").json()
    assert info["id"] == "firma-x" and info["type"] == "company"
    assert info["languages"] == ["de", "fr", "it", "en"]
    assert "document-basics" in info["apps"]
    hh = client.get("/api/v1/household").json()
    assert hh["self"] == "firma-x"
    assert [m["id"] for m in hh["members"]] == ["firma-x"]


def test_household_from_launcher_file(tmp_path):
    hh = tmp_path / "household.toml"
    hh.write_text(
        'admin = "alex"\n[mailroom]\nport = 8420\n'
        '[[mandants]]\nid = "private"\ntitle = "Privat"\ntype = "private"\nport = 8421\n'
        '[[mandants]]\nid = "firma-x"\ntitle = { de = "Firma X" }\ntype = "company"\nport = 8422\n',
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=tmp_path / "none", household_path=hh
    )
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        hh_direct = client.get("/api/v1/household").json()
        # Behind the launcher (ADR-0032): one origin, relative member URLs.
        hh_proxied = client.get("/api/v1/household", headers={"X-Aitmation-Proxy": "1"}).json()
    members = hh_direct["members"]
    assert [m["id"] for m in members] == ["mailroom", "private", "firma-x"]
    assert members[1]["type"] == "private" and members[1]["url"].endswith(":8421")
    assert members[2]["title"] == "Firma X"
    assert hh_direct["launcher"] is None  # no [launcher] section in this file
    assert [m["url"] for m in hh_proxied["members"]] == ["/m/mailroom", "/m/private", "/m/firma-x"]


def test_household_behind_launcher_is_one_origin(tmp_path):
    hh = tmp_path / "household.toml"
    hh.write_text(
        'admin = "alex"\n[launcher]\nport = 8400\n[mailroom]\nport = 8420\n'
        '[[mandants]]\nid = "private"\ntitle = "Privat"\ntype = "private"\nport = 8421\n',
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=tmp_path / "none", household_path=hh
    )
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        direct = client.get("/api/v1/household").json()
        proxied = client.get("/api/v1/household", headers={"X-Aitmation-Proxy": "1"}).json()
    assert direct["launcher"] == "http://127.0.0.1:8400"
    assert proxied["launcher"] == ""
    assert proxied["members"][1]["url"] == "/m/private"


def test_gates_and_categorization_decision_via_api(instance):
    client, runtime = instance
    _drop(runtime)
    runtime.tick()
    [gate] = client.get("/api/v1/gates").json()
    assert gate["gate_kind"] == "categorization"
    assert {o["id"] for o in gate["options"]} >= {"invoice", "letter"}
    assert gate["template_title"] == "Dokument-Eingang"
    assert gate["document"]["original_name"] == "doc.txt"

    resp = client.post(
        f"/api/v1/cases/{gate['case_id']}/steps/{gate['step_id']}/decide",
        json={"category": "letter", "actor": "alex"},
    )
    assert resp.status_code == 200
    runtime.tick()  # dispatch -> document-read (stub)
    runtime.tick()
    cases = client.get("/api/v1/cases").json()
    assert {c["template_id"] for c in cases} >= {"document-intake", "document-read"}
    detail = client.get(f"/api/v1/cases/{gate['case_id']}").json()
    assert detail["status"] == "completed"
    assert any(a["event"] == "step_completed" for a in detail["audit"])
    assert detail["app"] is None  # intake is core
    # Deciding twice is a conflict, surfaced as 409.
    again = client.post(
        f"/api/v1/cases/{gate['case_id']}/steps/{gate['step_id']}/decide",
        json={"category": "letter"},
    )
    assert again.status_code == 409


def test_generic_gate_decision_and_discard(instance):
    client, runtime = instance
    _drop(runtime)
    runtime.tick()
    [gate] = client.get("/api/v1/gates").json()
    resp = client.post(
        f"/api/v1/cases/{gate['case_id']}/steps/{gate['step_id']}/decide",
        json={"discard": True, "actor": "alex"},
    )
    assert resp.status_code == 200
    assert client.get("/api/v1/gates").json() == []
    assert client.get(f"/api/v1/cases/{gate['case_id']}").json()["status"] == "completed"
    empty = client.post(
        f"/api/v1/cases/{gate['case_id']}/steps/{gate['step_id']}/decide", json={}
    )
    assert empty.status_code == 422


def test_apps_listing_toggle_and_settings(instance):
    client, runtime = instance
    apps = {a["id"]: a for a in client.get("/api/v1/apps").json()}
    # The base solutions ship with the core; further ones may sit next to them.
    assert {"company-profile", "document-basics", "tasks"} <= set(apps)
    app = apps["document-basics"]
    assert app["enabled"] and app["loaded"]
    assert app["name"]["fr"] == "Lire et classer les documents"
    assert [s["name"] for s in app["settings"]] == ["document_type"]
    assert app["requires"]["data_class"] == "internal"

    resp = client.put(
        "/api/v1/apps/document-basics/settings",
        json={"values": {"document_type": "Beleg"}, "actor": "alex"},
    )
    assert resp.status_code == 200
    assert client.get("/api/v1/apps/document-basics").json()["settings_values"] == {
        "document_type": "Beleg"
    }
    bad = client.put(
        "/api/v1/apps/document-basics/settings", json={"values": {"nope": 1}}
    )
    assert bad.status_code == 422

    off = client.post("/api/v1/apps/document-basics/enable", json={"enabled": False})
    assert off.status_code == 200
    assert "document-read" not in runtime.engine.definitions
    listed = {a["id"]: a for a in client.get("/api/v1/apps").json()}
    assert listed["document-basics"]["enabled"] is False
    # what a bundle builds on is listed whether it is loaded or not
    assert listed["document-basics"]["dependencies"] == []
    assert listed["document-basics"]["loaded"] is False
    if "meeting-notes" in listed:  # a solution with a dependency, where one is installed
        assert listed["meeting-notes"]["dependencies"] == ["tasks"]
        client.post("/api/v1/apps/meeting-notes/enable", json={"enabled": False})
        assert client.get("/api/v1/apps/meeting-notes").json()["dependencies"] == ["tasks"]
        client.post("/api/v1/apps/meeting-notes/enable", json={"enabled": True})
    client.post("/api/v1/apps/document-basics/enable", json={"enabled": True})
    assert "document-read" in runtime.engine.definitions


def test_settings_endpoints(instance):
    client, _runtime = instance
    models = client.get("/api/v1/models").json()
    assert {m["role"] for m in models} == {"cheap", "strong"}
    assert client.get("/api/v1/log/llm").json() == []
    assert isinstance(client.get("/api/v1/log/diagnostics").json(), list)
    doctypes = client.get("/api/v1/doctypes").json()
    assert any(d["id"] == "invoice" and d["title"]["it"] == "Fattura" for d in doctypes)
    assert client.get("/api/v1/mandates").json() == []


# -- mailroom over the API: several addressees, rule suggestions ----------------


@pytest.fixture()
def household(tmp_path):
    private = Runtime(Settings(data_dir=tmp_path / "private", templates_dir=tmp_path / "none"))
    company = Runtime(Settings(data_dir=tmp_path / "company", templates_dir=tmp_path / "none"))
    ms = Settings(
        data_dir=tmp_path / "mailroom", templates_dir=MAILROOM, instance_type="mailroom"
    )
    ms.ensure_layout()
    save_addressee(
        ms.addressees_path,
        Addressee(id="private", title="Privat", path=str(private.settings.inbox_dir)),
    )
    save_addressee(
        ms.addressees_path,
        Addressee(id="company", title="Firma", path=str(company.settings.inbox_dir)),
    )
    mailroom = Runtime(ms)
    with TestClient(create_app(mailroom, model_factory=lambda s: None)) as client:
        yield client, mailroom, private, company
    mailroom.close()
    private.close()
    company.close()


def test_document_concerning_both_is_copied_to_both_with_provenance(household):
    client, mailroom, private, company = household
    (mailroom.settings.inbox_dir / "kreditkarte.txt").write_text("Abrechnung", encoding="utf-8")
    mailroom.tick()
    [gate] = client.get("/api/v1/gates").json()
    assert gate["gate_kind"] == "addressing"
    assert {o["id"] for o in gate["options"]} == {"private", "company"}
    resp = client.post(
        f"/api/v1/cases/{gate['case_id']}/steps/{gate['step_id']}/decide",
        json={"addressees": ["private", "company"], "actor": "alex"},
    )
    assert resp.status_code == 200
    mailroom.tick()  # deliver
    for inbox in (private.settings.inbox_dir, company.settings.inbox_dir):
        names = {p.name for p in inbox.iterdir()}
        assert any(n.endswith("kreditkarte.txt") for n in names)
        [note] = [p for p in inbox.iterdir() if p.name.endswith(".provenance.json")]
        data = json.loads(note.read_text(encoding="utf-8"))
        assert data["delivered_to"] == ["private", "company"]
        assert data["source_case"] == gate["case_id"]
    # The receiving intake treats the note as envelope metadata, not as a document.
    company.close()
    company_rt = Runtime(
        Settings(data_dir=company.settings.data_dir, templates_dir=CORE_TEMPLATES)
    )
    try:
        company_rt.tick()
        assert len(company_rt.conn.execute("SELECT id FROM cases").fetchall()) == 1
        [trigger] = company_rt.conn.execute(
            "SELECT result FROM steps WHERE kind = 'trigger'"
        ).fetchall()
        payload = json.loads(trigger["result"])
        assert payload["provenance"]["delivered_to"] == ["private", "company"]
        # The receiving mandant shows the document's own name, not the
        # mailroom's storage prefix.
        assert payload["original_name"] == "kreditkarte.txt"
        assert not any(
            p.name.endswith(".provenance.json") for p in company.settings.inbox_dir.iterdir()
        )
    finally:
        company_rt.close()
    assert not any(mailroom.settings.files_dir.iterdir())  # kept nothing
    detail = client.get(f"/api/v1/cases/{gate['case_id']}").json()
    forward = next(s for s in detail["steps"] if s["step_id"] == "deliver")
    assert forward["result"]["delivered_to"] == ["private", "company"]


def test_rule_suggestion_after_repeated_hand_assignments(household):
    client, mailroom, _private, _company = household
    for i in range(3):
        # The envelope carries the sender via the mail-style trigger payload;
        # folder drops have none, so we inject it the way the mail intake does.
        (mailroom.settings.inbox_dir / f"r{i}.txt").write_text("Rechnung", encoding="utf-8")
        mailroom.tick()
        [gate] = client.get("/api/v1/gates").json()
        mailroom.conn.execute(
            "UPDATE steps SET result = json_set(COALESCE(result, '{}'), '$.from',"
            " 'buchhaltung@lieferant.ch') WHERE case_id = ? AND kind = 'trigger'",
            (gate["case_id"],),
        )
        mailroom.conn.commit()
        client.post(
            f"/api/v1/cases/{gate['case_id']}/steps/{gate['step_id']}/decide",
            json={"addressee": "company", "actor": "alex"},
        )
        mailroom.tick()
    [suggestion] = client.get("/api/v1/mandates/suggestions").json()
    assert suggestion["target"] == "company"
    assert suggestion["condition"] == {"from": "buchhaltung@lieferant.ch"}
    assert suggestion["count"] == 3
    # Granting is a human act; afterwards the suggestion disappears.
    granted = client.post(
        "/api/v1/mandates",
        json={"target": "company", "condition": suggestion["condition"], "actor": "alex"},
    )
    assert granted.status_code == 200
    assert client.get("/api/v1/mandates/suggestions").json() == []
    assert len(client.get("/api/v1/mandates").json()) == 1


def test_document_can_be_opened_and_previewed(instance):
    """A decision needs the document, not just its name."""
    client, runtime = instance
    _drop(runtime, "brief.txt", "Sehr geehrte Damen und Herren, dies ist ein Brief.")
    runtime.tick()
    [gate] = client.get("/api/v1/gates").json()
    raw = client.get(f"/api/v1/cases/{gate['case_id']}/document")
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/plain")
    assert "inline" in raw.headers["content-disposition"]
    assert "brief.txt" in raw.headers["content-disposition"]
    assert raw.text.startswith("Sehr geehrte")
    preview = client.get(f"/api/v1/cases/{gate['case_id']}/document/text").json()
    assert preview["text"].startswith("Sehr geehrte") and preview["truncated"] is False
    assert preview["name"] == "brief.txt"
    assert client.get(f"/api/v1/cases/{gate['case_id']}/document?index=3").status_code == 404
    assert client.get("/api/v1/cases/nope/document").status_code == 404
