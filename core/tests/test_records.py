"""Declared records (ADR-0026): schemas with country variants, versioned
values, the company-profile bundle and the agent context hook."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings
from aitmation_core.records import RecordError, parse_schema
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"

SCHEMA = """
id = "demo"
title = { de = "Demo", en = "Demo" }
variant_field = "country"

[fields.name]
type = "text"
required = true
label = { de = "Name" }

[fields.country]
type = "choice"
required = true
label = { de = "Land" }
choices = ["CH", "DE"]

[fields.postal_code]
type = "text"
label = { de = "PLZ" }

[fields.vat]
type = "bool"
label = { de = "MWST" }

[variants.CH.postal_code]
type = "text"
pattern = "\\\\d{4}"
example = "8001"
label = { de = "PLZ" }

[variants.CH.uid]
type = "text"
pattern = "CHE-\\\\d{3}\\\\.\\\\d{3}\\\\.\\\\d{3}"
label = { de = "UID" }

[variants.DE.postal_code]
type = "text"
pattern = "\\\\d{5}"
label = { de = "PLZ" }
"""


def test_schema_resolves_fields_per_variant():
    schema = parse_schema(SCHEMA)
    names = [f.name for f in schema.resolve({"country": "CH"})]
    assert names == ["name", "country", "postal_code", "vat", "uid"]
    assert [f.name for f in schema.resolve({"country": "DE"})] == [
        "name",
        "country",
        "postal_code",
        "vat",
    ]
    assert [f.name for f in schema.resolve(None)] == ["name", "country", "postal_code", "vat"]


def test_values_are_validated_against_the_variant():
    schema = parse_schema(SCHEMA)
    clean = schema.validate_values(
        {
            "name": " Einssoft ",
            "country": "CH",
            "postal_code": "8001",
            "uid": "CHE-123.456.789",
            "vat": "true",
            "ignored": 1,
        }
    )
    assert clean == {
        "name": "Einssoft",
        "country": "CH",
        "postal_code": "8001",
        "uid": "CHE-123.456.789",
        "vat": True,
    }
    with pytest.raises(RecordError, match="postal_code"):
        schema.validate_values({"name": "x", "country": "CH", "postal_code": "80331"})
    with pytest.raises(RecordError, match="name: required"):
        schema.validate_values({"country": "DE"})
    with pytest.raises(RecordError, match="country"):
        schema.validate_values({"name": "x", "country": "FR"})
    # The German variant accepts five digits and knows no UID.
    clean = schema.validate_values(
        {"name": "x", "country": "DE", "postal_code": "80331", "uid": "CHE-1"}
    )
    assert "uid" not in clean


def test_schema_errors_are_precise():
    with pytest.raises(RecordError, match="variant_field 'x' is not a declared field"):
        parse_schema('id = "a"\ntitle = "A"\nvariant_field = "x"\n')
    with pytest.raises(RecordError, match="not a choice of"):
        parse_schema(
            'id = "a"\ntitle = "A"\nvariant_field = "c"\n'
            '[fields.c]\ntype = "choice"\nlabel = "C"\nchoices = ["x"]\n'
            '[variants.y.f]\ntype = "text"\nlabel = "F"\n'
        )
    with pytest.raises(RecordError, match="unknown keys"):
        parse_schema('id = "a"\ntitle = "A"\n[fields.f]\ntype = "text"\nlabel = "F"\nfoo = 1\n')


@pytest.fixture()
def company(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        instance_type="company",
    )
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        yield client, runtime


def test_company_profile_bundle_is_offered_to_company_instances(company):
    client, _ = company
    rows = {r["id"]: r for r in client.get("/api/v1/records").json()}
    assert "company-profile" in rows and "household-profile" not in rows
    assert rows["contacts"]["kind"] == "collection" and rows["contacts"]["rows"] == 0
    row = rows["company-profile"]
    assert row["source"] == "company-profile"
    assert row["completeness"]["complete"] is False and row["completeness"]["filled"] == 0
    detail = client.get("/api/v1/records/company-profile").json()
    assert detail["variant_field"] == "country"
    assert detail["variants"] == ["AT", "CH", "DE"]
    assert detail["values"] == {}
    assert client.get("/api/v1/instance").json()["country"] is None


def test_private_instances_see_the_household_profile(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        instance_type="private",
    )
    runtime = Runtime(settings)
    # the household profile instead of the company profile; every other
    # solution's collections come along as they are
    assert {"household-profile", "contacts", "accounts", "tasks"} <= set(runtime.records)
    assert "company-profile" not in runtime.records
    runtime.close()


def test_saving_a_swiss_profile_switches_the_variant_and_versions_values(company):
    client, runtime = company
    values = {
        "name": "Einssoft AG",
        "country": "CH",
        "street": "Bahnhofstrasse 1",
        "postal_code": "8001",
        "city": "Zürich",
        "uid": "CHE-123.456.789",
        "legal_form": "ag",
        "canton": "ZH",
        "vat_registered": True,
        "vat_number": "CHE-123.456.789 MWST",
        "email": "info@example.ch",
    }
    resp = client.put("/api/v1/records/company-profile", json={"values": values, "actor": "alex"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["values"]["uid"] == "CHE-123.456.789"
    names = [f["name"] for f in body["fields"]]
    assert "uid" in names and "canton" in names and "register_number" not in names
    assert body["completeness"]["complete"] is True
    assert body["meta"]["version"] == 1 and body["meta"]["saved_by"] == "alex"
    assert client.get("/api/v1/instance").json()["country"] == "CH"

    # A German company has other identifiers; the Swiss ones are dropped.
    values.update(
        {
            "country": "DE",
            "postal_code": "80331",
            "vat_number": "DE123456789",
            "register_number": "HRB 12345",
            "legal_form": "gmbh",
            "state": "DE-BY",
        }
    )
    values.pop("canton")
    resp = client.put("/api/v1/records/company-profile", json={"values": values})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "uid" not in body["values"] and body["values"]["register_number"] == "HRB 12345"
    assert body["meta"]["version"] == 2
    history = client.get("/api/v1/records/company-profile/history").json()
    assert [h["version"] for h in history] == [2, 1]
    assert runtime.country() == "DE"


def test_invalid_values_are_rejected_with_the_field_named(company):
    client, _ = company
    resp = client.put(
        "/api/v1/records/company-profile",
        json={
            "values": {
                "name": "X",
                "country": "CH",
                "street": "a",
                "postal_code": "80331",
                "city": "b",
                "uid": "123",
            }
        },
    )
    assert resp.status_code == 422
    assert "postal_code" in resp.json()["detail"] and "uid" in resp.json()["detail"]
    assert client.get("/api/v1/records/nope").status_code == 404
    assert client.put("/api/v1/records/nope", json={"values": {}}).status_code == 404


def test_local_schema_overlays_the_bundle(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        instance_type="company",
    )
    settings.ensure_layout()
    (settings.records_dir / "company-profile.toml").write_text(
        'id = "company-profile"\ntitle = "Eigenes Profil"\nscope = "company"\n'
        '[fields.name]\ntype = "text"\nrequired = true\nlabel = "Name"\n',
        encoding="utf-8",
    )
    runtime = Runtime(settings)
    assert runtime.record_source["company-profile"] == "local"
    assert [f.name for f in runtime.records["company-profile"].resolve(None)] == ["name"]
    runtime.close()


def test_agent_context_carries_requested_records_only(company):
    client, runtime = company
    client.put(
        "/api/v1/records/company-profile",
        json={
            "values": {
                "name": "Einssoft AG",
                "country": "CH",
                "street": "s",
                "postal_code": "8001",
                "city": "Zürich",
            }
        },
    )
    # A minimal case so _agent_payload has something to read.
    definition = runtime.engine.definitions["document-intake"]
    case_id = runtime.engine.start_case(definition.id, {"source": "test", "files": []})
    payload = runtime._agent_payload(case_id, {"context": {"records": ["company-profile"]}})
    assert payload["records"]["company-profile"]["name"] == "Einssoft AG"
    payload = runtime._agent_payload(case_id, {})
    assert "records" not in payload


def test_collections_keep_versioned_rows_and_feed_the_agent_context(company):
    client, runtime = company
    resp = client.post(
        "/api/v1/records/contacts/rows",
        json={
            "values": {
                "name": "Swisscom AG",
                "kind": "supplier",
                "city": "Bern",
                "email": "billing@swisscom.ch",
            },
            "actor": "alex",
        },
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["label"] == "Swisscom AG" and row["version"] == 1 and row["saved_by"] == "alex"
    row_id = row["row_id"]
    client.post(
        "/api/v1/records/contacts/rows",
        json={"values": {"name": "Kanton Zürich", "kind": "authority"}},
    )
    assert client.get("/api/v1/records").json()
    rows = {r["id"]: r for r in client.get("/api/v1/records").json()}
    assert rows["contacts"]["rows"] == 2
    # Search over the declared fields.
    found = client.get("/api/v1/records/contacts/rows", params={"q": "swiss"}).json()
    assert [r["label"] for r in found] == ["Swisscom AG"]
    # Changing a row appends a version; the history keeps the old values.
    resp = client.put(
        f"/api/v1/records/contacts/rows/{row_id}",
        json={"values": {"name": "Swisscom (Schweiz) AG", "kind": "supplier"}, "actor": "erika"},
    )
    assert resp.status_code == 200 and resp.json()["version"] == 2
    detail = client.get(f"/api/v1/records/contacts/rows/{row_id}").json()
    assert [h["version"] for h in detail["history"]] == [2, 1]
    assert detail["history"][1]["values"]["city"] == "Bern"
    # Validation names the field; unknown collections are 404.
    bad = client.post("/api/v1/records/contacts/rows", json={"values": {"kind": "supplier"}})
    assert bad.status_code == 422 and "name" in bad.json()["detail"]
    assert client.get("/api/v1/records/company-profile/rows").status_code == 404
    # Agent context: collections arrive as rows, singles as values.
    definition = runtime.engine.definitions["document-intake"]
    case_id = runtime.engine.start_case(definition.id, {"source": "test", "files": []})
    payload = runtime._agent_payload(case_id, {"context": {"records": ["contacts"]}})
    names = sorted(r["name"] for r in payload["records"]["contacts"]["rows"])
    assert names == ["Kanton Zürich", "Swisscom (Schweiz) AG"]
    assert payload["records"]["contacts"]["truncated"] is False
    # Soft delete: gone from the list, kept in the history.
    assert client.delete(f"/api/v1/records/contacts/rows/{row_id}").json() == {"ok": True}
    assert [r["label"] for r in client.get("/api/v1/records/contacts/rows").json()] == [
        "Kanton Zürich"
    ]
    assert client.delete(f"/api/v1/records/contacts/rows/{row_id}").status_code == 404
    assert client.get(f"/api/v1/records/contacts/rows/{row_id}").json()["deleted"] is True
