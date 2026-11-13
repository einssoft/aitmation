"""Vermittler: standing mandates (ADR-0016), forwarding, addressing gate.

Two runtimes on one machine simulate the target picture from
docs/VERMITTLER.md: a Vermittler instance whose only concern is WHO a
document is for, delivering into the inbox of a Mandant instance that then
runs its own intake. The Vermittler keeps no copies - only delivery
metadata in case and audit.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from aitmation_core.addressees import Addressee, AddresseeError, load_addressees, save_addressee
from aitmation_core.config import Settings
from aitmation_core.engine import EngineError
from aitmation_core.runtime import Runtime
from aitmation_core.template import TemplateError, load_templates, parse_template
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[1]
REPO_TEMPLATES = REPO / "templates"
REPO_DOCTYPES = REPO / "doctypes"
REPO_VERMITTLER = REPO / "mailroom"


@pytest.fixture()
def rig(tmp_path):
    """A Vermittler runtime delivering into a Mandant runtime's inbox."""
    mandant = Runtime(
        Settings(
            data_dir=tmp_path / "mandant",
            templates_dir=REPO_TEMPLATES,
            doctypes_dir=REPO_DOCTYPES,
        )
    )
    mailroom_settings = Settings(
        data_dir=tmp_path / "mailroom", templates_dir=REPO_VERMITTLER
    )
    mailroom_settings.ensure_layout()
    save_addressee(
        mailroom_settings.addressees_path,
        Addressee(id="privat", title="Privat", path=str(mandant.settings.inbox_dir)),
    )
    mailroom = Runtime(mailroom_settings)
    yield mailroom, mandant
    mailroom.close()
    mandant.close()


def _drop(runtime, name="brief.txt"):
    (runtime.settings.inbox_dir / name).write_text("Sehr geehrte", encoding="utf-8")


# -- mandates (ADR-0016) -----------------------------------------------------


def test_mandate_grant_match_revoke(rig):
    mailroom, _ = rig
    engine = mailroom.engine
    catch_all = engine.grant_mandate("forward", "privat", {}, actor_id="alex")
    specific = engine.grant_mandate(
        "forward", "privat", {"from": "bank@example.ch"}, actor_id="alex"
    )
    # Most specific active mandate wins; the catch-all is the fallback.
    assert engine.find_mandate("forward", {"from": "bank@example.ch"})["id"] == specific
    assert engine.find_mandate("forward", {"from": "other@example.ch"})["id"] == catch_all
    engine.revoke_mandate(catch_all, actor_id="alex")
    assert engine.find_mandate("forward", {"from": "other@example.ch"}) is None
    with pytest.raises(EngineError, match="already revoked"):
        engine.revoke_mandate(catch_all, actor_id="alex")
    # History stays: mandates are never deleted.
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        mailroom.conn.execute("DELETE FROM mandates WHERE id = ?", (specific,))


FORWARD_TEMPLATE = """
id = "f"
version = 1
[steps.a]
kind = "trigger"
config = { source = "folder" }
next = { fired = "z" }
[steps.z]
kind = "system_action"
config = { action = "forward"MANDATE }
next = { done = "end", failed = "end" }
"""


def test_forward_needs_mandate_declaration_or_gate():
    # Without the mandate declaration the static ADR-0007 check rejects it.
    with pytest.raises(TemplateError, match="ADR-0007"):
        parse_template(FORWARD_TEMPLATE.replace("MANDATE", ""))
    parse_template(FORWARD_TEMPLATE.replace("MANDATE", ", mandate = true")).validate()


def test_declared_mandate_step_still_needs_a_real_mandate(tmp_path):
    """The declaration grants nothing - runtime demands an actual mandate."""
    settings = Settings(data_dir=tmp_path / "d", templates_dir=tmp_path / "unused")
    definition = parse_template(FORWARD_TEMPLATE.replace("MANDATE", ", mandate = true"))
    runtime = Runtime(settings, definitions={"f": definition})
    try:
        case_id = runtime.engine.start_case("f", {"source": "folder"}, actor_id="test")
        with pytest.raises(EngineError, match="standing mandate"):
            runtime.engine.complete_step(case_id, "z", "done", actor_type="system")
        with pytest.raises(EngineError, match="standing mandate"):
            runtime.engine.complete_step(
                case_id, "z", "done", actor_type="system", mandate_id="no-such-mandate"
            )
    finally:
        runtime.close()


# -- delivery paths ----------------------------------------------------------


def test_catch_all_mandate_delivers_without_gate(rig):
    mailroom, mandant = rig
    mandate_id = mailroom.engine.grant_mandate(
        "forward", "privat", {}, actor_id="alex", comment="Catch-all bei Einrichtung"
    )
    _drop(mailroom)
    mailroom.tick()  # rule -> forward, no human involved

    assert mailroom.open_gates() == []
    [case] = list(mailroom.conn.execute("SELECT * FROM cases"))
    assert case["status"] == "completed"
    assert case["gate_passed_at"] is None  # no per-case gate - the mandate legitimized it
    # The file arrived at the Mandant; the Vermittler kept no copy.
    assert any(p.name.endswith("brief.txt") for p in mandant.settings.inbox_dir.iterdir())
    assert not any(mailroom.settings.files_dir.iterdir())
    assert not any(mailroom.settings.archive_dir.iterdir())
    # Every mandated execution references its mandate in the audit trail.
    entries = [
        json.loads(r["details"])
        for r in mailroom.conn.execute("SELECT details FROM audit_log")
        if r["details"]
    ]
    assert any(e.get("mandate_id") == mandate_id for e in entries)

    # The Mandant's own intake picks the delivery up as a fresh document.
    mandant.tick()
    [gate] = mandant.open_gates()
    assert gate["template_id"] == "document-intake"


def test_without_mandate_the_gate_decides(rig):
    mailroom, mandant = rig
    _drop(mailroom)
    mailroom.tick()  # no rule, no model -> addressing gate

    [gate] = mailroom.open_gates()
    assert (gate["template_id"], gate["step_id"]) == ("mailroom-assign", "choose_addressee")
    mailroom.address(gate["case_id"], "choose_addressee", "privat", actor_id="alex")
    mailroom.tick()  # forward runs, legitimized by the passed gate

    assert any(p.name.endswith("brief.txt") for p in mandant.settings.inbox_dir.iterdir())
    [case] = list(mailroom.conn.execute("SELECT * FROM cases"))
    assert case["status"] == "completed"
    assert case["gate_passed_at"] is not None  # zugeteilt is the approving outcome


def test_discard_at_the_gate_delivers_nothing(rig):
    mailroom, mandant = rig
    _drop(mailroom)
    mailroom.tick()
    [gate] = mailroom.open_gates()
    mailroom.address(gate["case_id"], "choose_addressee", None, actor_id="alex")
    mailroom.tick()
    assert not any(mandant.settings.inbox_dir.iterdir())
    with pytest.raises(EngineError, match="unknown addressee"):
        mailroom.address(gate["case_id"], "choose_addressee", "niemand", actor_id="alex")


def test_revoked_mandate_falls_back_to_the_gate(rig):
    mailroom, _ = rig
    mandate_id = mailroom.engine.grant_mandate("forward", "privat", {}, actor_id="alex")
    mailroom.engine.revoke_mandate(mandate_id, actor_id="alex")
    _drop(mailroom)
    mailroom.tick()
    [gate] = mailroom.open_gates()  # the rule no longer fires
    assert gate["step_id"] == "choose_addressee"


# -- configuration -----------------------------------------------------------


def test_addressee_roundtrip_and_validation(tmp_path):
    path = tmp_path / "addressees.toml"
    save_addressee(path, Addressee(id="firma-a", title="Firma A", path="/x/inbox"))
    save_addressee(path, Addressee(id="privat", title="Privat", path="/y/inbox"))
    loaded = load_addressees(path)
    assert set(loaded) == {"firma-a", "privat"}
    assert loaded["firma-a"].channel == "folder"
    with pytest.raises(AddresseeError, match="unknown channel"):
        Addressee(id="x", title="X", path="/p", channel="brieftaube").validate()


def test_mailroom_template_set_loads():
    definitions = load_templates(REPO_VERMITTLER)
    assert set(definitions) == {"mailroom-assign"}
    gate = definitions["mailroom-assign"].steps["choose_addressee"]
    assert gate.approving_outcomes() == {"assigned"}


# -- web ---------------------------------------------------------------------


@pytest.fixture()
def web(rig):
    mailroom, mandant = rig
    from fastapi.testclient import TestClient

    with TestClient(create_app(mailroom, model_factory=lambda s: None)) as client:
        yield client, mailroom, mandant


def test_addressing_gate_renders_and_delivers(web):
    client, mailroom, mandant = web
    _drop(mailroom)
    mailroom.tick()
    [gate] = client.get("/api/v1/gates").json()
    assert gate["gate_kind"] == "addressing"
    assert [o["id"] for o in gate["options"]] == ["privat"]

    resp = client.post(
        f"/api/v1/cases/{gate['case_id']}/steps/choose_addressee/decide",
        json={"addressee": "privat", "actor": "alex"},
    )
    assert resp.status_code == 200, resp.text
    mailroom.tick()
    assert any(mandant.settings.inbox_dir.iterdir())


def test_admin_creates_addressee_with_catch_all_mandate(web):
    client, mailroom, _ = web
    resp = client.put(
        "/api/v1/addressees/firma-a",
        json={"title": "Firma A", "path": "/tmp/firma-a-inbox", "catch_all": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["catch_all"] is True
    assert "firma-a" in mailroom.addressees
    mandates = mailroom.engine.active_mandates("forward")
    assert any(m["target"] == "firma-a" and m["condition"] == "{}" for m in mandates)
    # Channels overview lists it; deleting is refused while the mandate stands.
    overview = client.get("/api/v1/channels").json()
    assert [a["id"] for a in overview["addressees"] if a["catch_all"]] == ["firma-a"]
    assert client.delete("/api/v1/addressees/firma-a").status_code == 409

    [mandate] = [m for m in mandates if m["target"] == "firma-a"]
    assert client.post(f"/api/v1/mandates/{mandate['id']}/revoke").json() == {"ok": True}
    assert mailroom.engine.active_mandates("forward") == []
    assert client.delete("/api/v1/addressees/firma-a").json() == {"ok": True}
    assert "firma-a" not in mailroom.addressees
