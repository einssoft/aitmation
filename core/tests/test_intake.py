"""Intake, deterministic dispatch and the sort-out gate (docs/AGENTIK.md).

The agentic part is exactly one judgment call: which document type is
present. Routing is a lookup in the declared document types; unknown or
uncertain documents land at a human gate, and a new category created there
grows the system without a single line of code.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings
from aitmation_core.doctypes import Doctype, DoctypeError, load_doctypes, save_doctype
from aitmation_core.engine import EngineError
from aitmation_core.model import ModelResponse
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

REPO_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
REPO_APPS = Path(__file__).resolve().parents[2] / "apps"
APP_DOCTYPES = REPO_APPS / "document-basics" / "doctypes"


class FakeClassifier:
    """Answers every model call with a fixed category and confidence."""

    def __init__(self, category="invoice", confidence=0.9):
        self.category = category
        self.confidence = confidence
        self.tasks: list[str] = []

    def complete_structured(self, task, payload, schema_hint=None):
        self.tasks.append(task)
        return ModelResponse(
            data={"category": self.category, "confidence": self.confidence},
            model_ref="fake@1",
            confidence=self.confidence,
        )


@pytest.fixture()
def make_runtime(tmp_path):
    runtimes = []

    def factory(model_client=None, doctypes_dir=None):
        settings = Settings(
            data_dir=tmp_path / "data",
            templates_dir=REPO_TEMPLATES,
            apps_dir=REPO_APPS,
            doctypes_dir=doctypes_dir,
        )
        runtime = Runtime(settings, model_client=model_client)
        runtimes.append(runtime)
        return runtime

    yield factory
    for runtime in runtimes:
        runtime.close()


def _drop(runtime, name="doc.txt"):
    (runtime.settings.inbox_dir / name).write_text("Sehr geehrte Damen", encoding="utf-8")


def _templates_of(runtime):
    return [
        r["template_id"]
        for r in runtime.conn.execute("SELECT template_id FROM cases ORDER BY created_at")
    ]


# -- doctype declarations ----------------------------------------------------


def test_shipped_doctypes_load_and_route_into_loaded_chains():
    doctypes = load_doctypes(APP_DOCTYPES)
    assert set(doctypes) == {"invoice", "bank-statement", "letter", "email"}
    assert all(d.chain == "document-read" for d in doctypes.values())
    assert doctypes["invoice"].features  # the classifier needs signs to look for


def test_doctype_save_load_roundtrip(tmp_path):
    doctype = Doctype(
        id="quittung",
        title="Quittung",
        chain="document-read",
        description='Beleg mit "Anführungszeichen"',
        features=("Betrag", "Datum"),
        fields={"amount": "Bezahlter Betrag"},
    )
    save_doctype(tmp_path, doctype)
    assert load_doctypes(tmp_path) == {"quittung": doctype}


def test_invalid_doctype_is_rejected(tmp_path):
    with pytest.raises(DoctypeError, match="reserved"):
        Doctype(id="unknown", title="X", chain="k").validate()
    with pytest.raises(DoctypeError, match="title"):
        Doctype(id="x", title=" ", chain="k").validate()
    (tmp_path / "bad.toml").write_text('id = "x"\ntitle = "X"\n', encoding="utf-8")
    with pytest.raises(DoctypeError, match="chain"):
        load_doctypes(tmp_path)


def test_unknown_chain_is_rejected_at_startup(tmp_path, make_runtime):
    doctypes_dir = tmp_path / "doctypes"
    save_doctype(doctypes_dir, Doctype(id="geist", title="Geist", chain="ghost"))
    with pytest.raises(DoctypeError, match="ghost"):
        make_runtime(doctypes_dir=doctypes_dir)


# -- classification and dispatch ---------------------------------------------


def test_recognized_document_dispatches_without_a_human(make_runtime):
    runtime = make_runtime(model_client=FakeClassifier("invoice", 0.9))
    _drop(runtime)
    runtime.tick()  # classify (recognized) -> dispatch starts document-read

    assert runtime.open_gates() == []  # no sort-out gate on the happy path
    assert _templates_of(runtime) == ["document-intake", "document-read"]
    row = runtime.conn.execute(
        "SELECT outcome FROM steps WHERE step_id = 'classify'"
    ).fetchone()
    assert row["outcome"] == "recognized"
    # One chain across the dispatch boundary.
    chains = {r["chain_id"] for r in runtime.conn.execute("SELECT chain_id FROM cases")}
    assert len(chains) == 1
    # The routing table reached the model as part of the task.
    assert "invoice" in runtime.model_client.tasks[0]
    assert "bank-statement" in runtime.model_client.tasks[0]


def test_low_confidence_is_sorted_out(make_runtime):
    runtime = make_runtime(model_client=FakeClassifier("invoice", 0.4))
    _drop(runtime)
    runtime.tick()
    [gate] = runtime.open_gates()
    assert gate["step_id"] == "categorize"
    # The model's suggestion stays visible for the human.
    row = runtime.conn.execute(
        "SELECT outcome, result FROM steps WHERE step_id = 'classify'"
    ).fetchone()
    assert row["outcome"] == "unknown"
    assert "invoice" in row["result"]


def test_undeclared_category_is_sorted_out(make_runtime):
    runtime = make_runtime(model_client=FakeClassifier("mahnung", 0.95))
    _drop(runtime)
    runtime.tick()
    [gate] = runtime.open_gates()
    assert gate["step_id"] == "categorize"


def test_offline_everything_is_sorted_out_and_categorized(make_runtime):
    runtime = make_runtime()  # no model: the stub sorts everything out
    _drop(runtime)
    runtime.tick()
    [gate] = runtime.open_gates()
    assert (gate["template_id"], gate["step_id"]) == ("document-intake", "categorize")
    runtime.categorize(gate["case_id"], "categorize", "email", actor_id="alex")
    runtime.tick()
    assert _templates_of(runtime) == ["document-intake", "document-read"]


def test_discarding_ends_the_case_without_dispatch(make_runtime):
    runtime = make_runtime()
    _drop(runtime)
    runtime.tick()
    [gate] = runtime.open_gates()
    runtime.categorize(gate["case_id"], "categorize", None, actor_id="alex")
    runtime.tick()
    assert _templates_of(runtime) == ["document-intake"]
    [status] = [r["status"] for r in runtime.conn.execute("SELECT status FROM cases")]
    assert status == "completed"


def test_unknown_category_at_the_gate_is_rejected(make_runtime):
    runtime = make_runtime()
    _drop(runtime)
    runtime.tick()
    [gate] = runtime.open_gates()
    with pytest.raises(EngineError, match="frei_erfunden"):
        runtime.categorize(gate["case_id"], "categorize", "frei_erfunden", actor_id="alex")
    assert len(runtime.open_gates()) == 1  # gate still open


# -- web: sort-out gate and new categories -----------------------------------


@pytest.fixture()
def web(tmp_path):
    """App over the shipped bundle plus a writable instance doctypes dir."""
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=REPO_TEMPLATES, apps_dir=REPO_APPS
    )
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        yield client, runtime


def test_categorization_endpoint_routes_and_rejects_unknown(web):
    client, runtime = web
    _drop(runtime)
    runtime.tick()
    [gate] = runtime.open_gates()
    url = f"/api/v1/cases/{gate['case_id']}/steps/categorize/decide"
    resp = client.post(url, json={"category": "frei_erfunden"})
    assert resp.status_code in (409, 422)
    resp = client.post(url, json={"category": "letter", "actor": "alex"})
    assert resp.status_code == 200, resp.text
    runtime.tick()
    assert _templates_of(runtime) == ["document-intake", "document-read"]
    # Deciding the same gate twice is a conflict.
    resp = client.post(url, json={"discard": True})
    assert resp.status_code == 409


def test_admin_creates_new_category_and_intake_uses_it(web):
    client, runtime = web
    resp = client.put(
        "/api/v1/doctypes/quittung",
        json={"title": {"de": "Quittung"}, "features": ["Betrag", "Datum"],
              "chain": "document-read"},
    )
    assert resp.status_code == 200, resp.text
    assert "quittung" in runtime.doctypes
    assert (runtime.settings.doctypes_dir / "quittung.toml").is_file()
    # The new category is immediately assignable at the sort-out gate.
    _drop(runtime)
    runtime.tick()
    [gate] = runtime.open_gates()
    runtime.categorize(gate["case_id"], "categorize", "quittung", actor_id="alex")
    runtime.tick()
    assert _templates_of(runtime) == ["document-intake", "document-read"]


def test_admin_rejects_category_with_unknown_chain(web):
    client, runtime = web
    resp = client.put(
        "/api/v1/doctypes/geist", json={"title": {"de": "Geist"}, "chain": "ghost"}
    )
    assert resp.status_code == 422
    assert "geist" not in runtime.doctypes
