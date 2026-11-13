"""Workshop API (ADR-0035): explorer, elements, save with validation, proving run."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app
from conftest import needs_solution

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"


@pytest.fixture()
def web(tmp_path):
    # A writable copy of the shipped solution: the API may edit it.
    import shutil

    apps_dir = tmp_path / "apps"
    shutil.copytree(REPO / "apps" / "document-basics", apps_dir / "document-basics")
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=apps_dir)
    runtime = Runtime(settings)
    client = TestClient(create_app(runtime))
    yield client, runtime
    runtime.close()


def test_explorer_lists_the_solution_tree(web):
    client, _ = web
    rows = client.get("/api/v1/solutions").json()
    assert [r["id"] for r in rows] == ["core", "document-basics"]
    sol = rows[1]
    assert sol["format"] == 2 and sol["writable"] is True
    assert "document-read" in sol["tree"]["processes"] and "read" in sol["tree"]["ai"]
    assert set(sol["configurations"]) >= {"standard", "test", "shadow"}
    assert sol["model_roles"]["default"]["model"] is not None
    assert sol["sidecars"] >= 6


def test_element_endpoint_returns_text_summary_and_sidecar(web):
    client, _ = web
    proc = client.get("/api/v1/solutions/document-basics/elements/process/document-read").json()
    assert proc["file"] == "processes/document-read.bpmn" and "<bpmn:process" in proc["text"]
    steps = {s["id"]: s for s in proc["summary"]["steps"]}
    assert steps["read"]["kind"] == "agent_step" and steps["read"]["prompt_hash"]
    assert proc["sidecar"] and "[[case]]" in proc["sidecar"]
    ai = client.get("/api/v1/solutions/document-basics/elements/ai/read").json()
    assert ai["summary"]["model_role"] == "default" and ai["summary"]["task"]
    assert client.get("/api/v1/solutions/document-basics/elements/process/nope").status_code == 404


def test_saving_a_broken_element_is_refused_and_restored(web):
    client, runtime = web
    element = client.get("/api/v1/solutions/document-basics/elements/ai/read").json()
    broken = element["text"].replace('model_role = "default"', 'model_role = "nobody"')
    response = client.put(
        "/api/v1/solutions/document-basics/elements/ai/read", json={"text": broken}
    )
    assert response.status_code == 422
    assert any("model_role 'nobody'" in f for f in response.json()["detail"]["findings"])
    again = client.get("/api/v1/solutions/document-basics/elements/ai/read").json()
    assert again["text"] == element["text"]  # restored
    # A valid change is applied and reloaded (the prompt hash moves).
    before = runtime.engine.definitions["document-read"].steps["read"].config["prompt_hash"]
    changed = element["text"].replace("Summarize the document", "Summarise the document")
    response = client.put(
        "/api/v1/solutions/document-basics/elements/ai/read", json={"text": changed}
    )
    assert response.status_code == 200, response.text
    after = runtime.engine.definitions["document-read"].steps["read"].config["prompt_hash"]
    assert after != before


def test_proving_run_over_the_api(web):
    client, _ = web
    report = client.post(
        "/api/v1/solutions/document-basics/test", json={"configuration": "test"}
    ).json()
    assert report["ok"] is True, report
    assert report["coverage"]["document-read"]["step_ratio"] == 1.0
    only = client.post(
        "/api/v1/solutions/document-basics/test",
        json={"only": "review-waits-for-a-person"},
    ).json()
    assert [r["case_id"] for r in only["results"]] == ["review-waits-for-a-person"]


def test_step_view_carries_the_panel_facts_and_history(web):
    client, runtime = web
    doc = runtime.settings.data_dir / "x.txt"
    doc.write_text("hello", encoding="utf-8")
    case_id = runtime.engine.start_case(
        "document-read", {"source": "manual", "subject": "x", "path": str(doc)}
    )
    runtime.tick()
    el = client.get("/api/v1/solutions/document-basics/elements/process/document-read").json()
    steps = {s["id"]: s for s in el["summary"]["steps"]}
    read = steps["read"]
    assert read["model_role"] == "default" and read["data_class"] in ("internal", "confidential")
    assert read["flexibility"] == "strict" and "summary" in read["output"]
    history = el["summary"]["history"]
    assert history["read"]["runs"] == 1 and history["read"]["last_case"] == case_id
    assert history["read"]["last_outcome"] in ("done", "failed")
    sol = client.get("/api/v1/solutions").json()[0]
    assert sol["active_configuration"] == "standard"


def test_briefing_form_save_writes_canonical_toml(web):
    client, runtime = web
    before = runtime.apps["document-basics"].briefings["read"]
    body = {
        "role": {"en": "You read documents."},
        "instructions": {"en": "Summarize in one sentence."},
        "limits": ["Never invent numbers."],
        "model_role": "default",
        "flexibility": "guided",
        "skills": [],
        "tools": [],
        "output": {"summary": "one sentence"},
    }
    out = client.put("/api/v1/solutions/document-basics/elements/ai/read/fields", json=body)
    assert out.status_code == 200, out.text
    after = runtime.apps["document-basics"].briefings["read"]
    assert after.instructions == {"en": "Summarize in one sentence."}
    assert after.flexibility == "guided" and after.output == {"summary": "one sentence"}
    assert after.context == before.context and after.extra == before.extra  # kept
    text = client.get("/api/v1/solutions/document-basics/elements/ai/read").json()["text"]
    assert 'flexibility = "guided"' in text and "[context]" in text
    # An unknown model role is refused and the file restored.
    bad = client.put(
        "/api/v1/solutions/document-basics/elements/ai/read/fields",
        json={**body, "model_role": "nonexistent"},
    )
    assert bad.status_code == 422
    assert runtime.apps["document-basics"].briefings["read"].flexibility == "guided"


def test_core_templates_appear_as_a_read_only_solution(web):
    """The workshop shows what the instance runs, including the core's
    mechanics templates - but nothing of the core is ever written."""
    client, _ = web
    core = client.get("/api/v1/solutions").json()[0]
    assert core["writable"] is False and "document-intake" in core["tree"]["processes"]
    element = client.get("/api/v1/solutions/core/elements/process/document-intake").json()
    assert element["writable"] is False and "<bpmn:" in element["text"]
    assert any(step["id"] == "classify" for step in element["summary"]["steps"])
    refused = client.put(
        "/api/v1/solutions/core/elements/process/document-intake", json={"text": element["text"]}
    )
    assert refused.status_code == 409
    assert client.post("/api/v1/solutions/core/test", json={}).status_code == 409
    assert client.get("/api/v1/solutions/core/elements/ai/read").status_code == 404


def test_new_solution_process_and_case_from_the_workshop(web, tmp_path):
    """Create: a solution, a process (its briefing comes along), a decision,
    then a sidecar case from a sample - every step validated and loaded."""
    client, runtime = web
    created = client.post(
        "/api/v1/solutions",
        json={"id": "my-flow", "name": {"de": "Mein Ablauf", "en": "My flow"}},
    )
    assert created.status_code == 201, created.text
    assert created.json()["writable"] is True and "my-flow" in runtime.apps
    dup = client.post("/api/v1/solutions", json={"id": "my-flow", "name": "x"})
    assert dup.status_code == 409

    process = client.post(
        "/api/v1/solutions/my-flow/elements/process",
        json={"id": "check", "title": "Prüfen", "ai": "look"},
    )
    assert process.status_code == 201, process.text
    assert set(process.json()["files"]) == {"ai/look.aimn.toml", "processes/check.bpmn"}
    assert "check" in runtime.engine.definitions
    element = client.get("/api/v1/solutions/my-flow/elements/process/check").json()
    ids = {step["id"] for step in element["summary"]["steps"]}
    assert ids == {"start", "prepare", "review"}
    assert client.post(
        "/api/v1/solutions/my-flow/elements/process", json={"id": "check"}
    ).status_code == 409

    decision = client.post(
        "/api/v1/solutions/my-flow/elements/decision", json={"id": "size", "title": "Grösse"}
    )
    assert decision.status_code == 201, decision.text
    assert "size" in runtime.decisions

    sample = runtime.apps["my-flow"].path / "samples" / "note.txt"
    sample.write_text("Rechnung 12, Total CHF 10", encoding="utf-8")
    case = client.post(
        "/api/v1/solutions/my-flow/elements/process/check/cases",
        json={
            "id": "note-waits-for-review",
            "sample": "samples/note.txt",
            "status": "running",
            "outcomes": {"prepare": "done"},
        },
    )
    assert case.status_code == 201, case.text
    body = case.json()
    assert "[[case]]" in body["sidecar"]
    # The case ran at once. Under 'test' the new briefing has no recording
    # yet, so the AI step fails (live calls are refused) - the run says so
    # instead of silently passing; recording it is the next step.
    (result,) = body["run"]["results"]
    assert result["passed"] is False and result["outcomes"]["prepare"] == "failed"
    assert any("outcomes.prepare" in f for f in result["failures"])
    assert client.get("/api/v1/solutions/my-flow/elements/process/check").json()["sidecar"]
    bad = client.post(
        "/api/v1/solutions/my-flow/elements/process/check/cases",
        json={"id": "x", "sample": "samples/missing.txt"},
    )
    assert bad.status_code == 422


def test_graph_endpoint_reads_and_applies_the_canvas(web):
    """The card canvas (ADR-0040): the graph comes from the file and goes
    back onto it through the validated save."""
    client, runtime = web
    url = "/api/v1/solutions/document-basics/elements/process/document-read/graph"
    graph = client.get(url).json()
    assert graph["process"] == "document-read" and graph["writable"] is True
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    assert kinds["read"] == "ai" and kinds["intake"] == "start"
    # Insert a gate after the AI step; the read step's extensions stay.
    graph["nodes"].append({"id": "check", "kind": "gate", "label": "Prüfen", "x": 460, "y": 120})
    graph["edges"] = [
        e for e in graph["edges"] if not (e["source"] == "read" and e["outcome"] == "done")
    ] + [
        {"source": "read", "target": "check", "outcome": "done"},
        {"source": "check", "target": "to_document_review", "outcome": "approved"},
        {"source": "check", "target": "finished", "outcome": "rejected"},
    ]
    saved = client.put(url, json={"nodes": graph["nodes"], "edges": graph["edges"]})
    assert saved.status_code == 200, saved.text
    definition = runtime.engine.definitions["document-read"]
    assert definition.steps["check"].kind.value == "human_gate"
    assert definition.steps["read"].next["done"] == "check"
    assert definition.steps["read"].config.get("briefing") or definition.steps["read"].config
    # A graph without a start is refused before anything is written.
    bad = client.put(
        url, json={"nodes": [n for n in graph["nodes"] if n["kind"] != "start"], "edges": []}
    )
    assert bad.status_code == 422 and "start" in bad.text
    assert "check" in runtime.engine.definitions["document-read"].steps
    core = client.put("/api/v1/solutions/core/elements/process/document-intake/graph", json={})
    assert core.status_code == 409


@needs_solution("website-chat")
def test_a_process_serves_as_template_and_a_bpmn_file_can_be_imported(tmp_path):
    """Workflows as templates (copy into one's own solution with what they
    name) and import of a BPMN file from another installation - both wait
    as drafts until an admin releases them (ADR-0039 stage 1)."""
    import shutil

    apps_dir = tmp_path / "apps"
    shutil.copytree(REPO / "apps" / "website-chat", apps_dir / "website-chat")
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=apps_dir)
    runtime = Runtime(settings)
    client = TestClient(create_app(runtime))
    base = "/api/v1/solutions/my-chat/elements/process"
    template = {"source_solution": "website-chat", "source_process": "website-chat"}
    try:
        created = client.post("/api/v1/solutions", json={"id": "my-chat", "name": "Mein Chat"})
        assert created.status_code == 201
        copy = client.post(f"{base}/copy", json={**template, "id": "shop-chat"})
        assert copy.status_code == 201, copy.text
        draft = copy.json()["draft"]
        assert copy.json()["findings"] == [] and draft["origin"] == "template"
        assert {"processes/shop-chat.bpmn", "ai/website-answer.aimn.toml",
                "ai/website-deep-terms.aimn.toml"} <= set(draft["files"])
        assert draft["changed"] == ["solution.toml"]
        # a draft runs nowhere
        assert "shop-chat" not in runtime.engine.definitions
        assert not (apps_dir / "my-chat" / "processes" / "shop-chat.bpmn").exists()
        listed = client.get("/api/v1/solutions/my-chat/drafts").json()
        assert [d["id"] for d in listed] == [draft["id"]]
        view = client.get(f"/api/v1/drafts/{draft['id']}").json()
        assert "shop-chat" in view["graphs"] and view["graphs"]["shop-chat"]["nodes"]
        # release: the admin gate - then it is a process like any other
        released = client.post(f"/api/v1/drafts/{draft['id']}/release")
        assert released.status_code == 200, released.text
        assert {"shop-chat", "website-chat"} <= set(runtime.engine.definitions)
        manifest = (apps_dir / "my-chat" / "solution.toml").read_text(encoding="utf-8")
        assert "[model_roles.answer]" in manifest and "[model_roles.check]" in manifest
        assert client.get("/api/v1/solutions/my-chat/drafts").json() == []
        # the same id twice is refused
        again = client.post(f"{base}/copy", json={**template, "id": "shop-chat"})
        assert again.status_code == 409
        # import: an exported BPMN under a new id, as a draft
        path = apps_dir / "my-chat" / "processes" / "shop-chat.bpmn"
        text = path.read_text(encoding="utf-8")
        imported = client.post(f"{base}/import", json={"name": "faq-chat.bpmn", "text": text})
        assert imported.status_code == 201, imported.text
        assert "faq-chat" not in runtime.engine.definitions
        client.post(f"/api/v1/drafts/{imported.json()['draft']['id']}/release")
        assert "faq-chat" in runtime.engine.definitions
        # a file naming what the solution lacks is kept as a draft with its
        # findings - and cannot be released
        broken = text.replace('ref="website-answer"', 'ref="nowhere"')
        held_body = {"name": "x.bpmn", "text": broken, "id": "broken-chat"}
        held = client.post(f"{base}/import", json=held_body)
        assert held.status_code == 201 and any("nowhere" in f for f in held.json()["findings"])
        bad_id = held.json()["draft"]["id"]
        assert client.post(f"/api/v1/drafts/{bad_id}/release").status_code == 422
        assert not (apps_dir / "my-chat" / "processes" / "broken-chat.bpmn").exists()
        assert client.delete(f"/api/v1/drafts/{bad_id}").status_code == 200
        assert client.get(f"/api/v1/drafts/{bad_id}").status_code == 404
        # straight in, when asked for (draft = false)
        direct_body = {"name": "y.bpmn", "text": text, "id": "direct-chat", "draft": False}
        direct = client.post(f"{base}/import", json=direct_body)
        assert direct.status_code == 201 and "direct-chat" in runtime.engine.definitions
        doctype = {"name": "x.bpmn", "text": "<!DOCTYPE x><x/>"}
        assert client.post(f"{base}/import", json=doctype).status_code == 422
    finally:
        runtime.close()


@needs_solution("website-chat")
def test_a_draft_does_not_overwrite_what_changed_since(tmp_path):
    import shutil

    apps_dir = tmp_path / "apps"
    shutil.copytree(REPO / "apps" / "website-chat", apps_dir / "website-chat")
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=apps_dir)
    runtime = Runtime(settings)
    client = TestClient(create_app(runtime))
    try:
        client.post("/api/v1/solutions", json={"id": "my-chat", "name": "Mein Chat"})
        body = {"source_solution": "website-chat", "source_process": "website-chat", "id": "a-chat"}
        copied = client.post("/api/v1/solutions/my-chat/elements/process/copy", json=body)
        draft = copied.json()["draft"]
        manifest = apps_dir / "my-chat" / "solution.toml"
        edited = manifest.read_text(encoding="utf-8") + "\n# edited meanwhile\n"
        manifest.write_text(edited, encoding="utf-8")
        refused = client.post(f"/api/v1/drafts/{draft['id']}/release")
        assert refused.status_code == 409 and "changed" in refused.json()["detail"]
        assert client.post("/api/v1/drafts/d-nothere/release").status_code == 404
        assert client.get("/api/v1/drafts/..%2Fetc").status_code == 404
    finally:
        runtime.close()


def test_a_foreign_workflow_becomes_a_draft_with_a_report(tmp_path):
    """ADR-0060: an n8n export lands as a draft - report, secrets, the
    model role added to the manifest - and goes live on release."""
    import json as _json
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_workflow_import import N8N

    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=apps_dir)
    runtime = Runtime(settings)
    client = TestClient(create_app(runtime))
    try:
        client.post("/api/v1/solutions", json={"id": "imports", "name": "Importe"})
        r = client.post("/api/v1/solutions/imports/import/foreign", json={"text": _json.dumps(N8N)})
        assert r.status_code == 201, r.text
        out = r.json()
        assert out["id"] == "lead-triage" and out["report"]["source"] == "n8n"
        assert out["findings"] == [] and "solution.toml" in out["files"]
        assert "AITMATION_SECRET_CRM" in out["report"]["secrets"]
        view = client.get(f"/api/v1/drafts/{out['draft']['id']}").json()
        assert view["report"]["counts"]["placeholder"] == 2 and "lead-triage" in view["graphs"]
        assert "lead-triage" not in runtime.engine.definitions  # a draft is not live
        released = client.post(f"/api/v1/drafts/{out['draft']['id']}/release")
        assert released.status_code == 200, released.text
        assert "lead-triage" in runtime.engine.definitions
        url = "/api/v1/solutions/imports/import/foreign"
        again = client.post(url, json={"text": _json.dumps(N8N)})
        assert again.status_code == 409
        bad = client.post("/api/v1/solutions/imports/import/foreign", json={"text": "{}"})
        assert bad.status_code == 422
    finally:
        runtime.close()
