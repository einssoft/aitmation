"""Knowledge spaces (ADR-0031): spaces with data class and owner, versioned
documents as files, local lexical + optional embedding retrieval with
provenance, fail-closed binding from agent steps, bundle seeding, API."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core.apps import validate_app
from aitmation_core.config import Settings
from aitmation_core.knowledge import (
    FastembedEmbedder,
    KnowledgeError,
    bm25_scores,
    chunk_text,
    cosine,
    tokens,
)
from aitmation_core.model import ModelResponse
from aitmation_core.runtime import Runtime
from aitmation_core.template import parse_template
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"

POLICY = (
    "Spesenreglement der Einssoft AG.\n\n"
    "Reisen mit dem Zug erfolgen in der zweiten Klasse. Das Halbtax wird von der "
    "Firma bezahlt.\n\n"
    "Übernachtungen im Hotel bis 180 Franken pro Nacht werden ohne Rückfrage "
    "vergütet. Teurere Hotels braucht die Freigabe der Geschäftsleitung.\n\n"
    "Kundenessen: bis 60 Franken pro Person. Alkohol wird nicht vergütet."
)


class FakeEmbedder:
    """A 'model' that puts a text on an axis per keyword - enough to see
    the hybrid ranking work without a real model."""

    name = "fake"
    axes = ("hotel", "zug", "essen", "alkohol")

    def embed(self, texts):
        out = []
        for text in texts:
            words = tokens(text)
            out.append([float(sum(1 for w in words if a in w)) for a in self.axes])
        return out


def test_chunking_tokens_and_scores():
    long = "\n\n".join(f"Absatz {i} " + "x" * 400 for i in range(6))
    chunks = chunk_text(long, size=1000, overlap=100)
    assert len(chunks) >= 3 and all(len(c) <= 1000 for c in chunks)
    assert chunks[1].startswith(chunks[0][-100:].strip()[:20])  # overlap carries over
    assert chunk_text("") == []
    assert tokens("Die Übernachtung im Hotel, 180 Franken!") == [
        "die",
        "übernachtung",
        "im",
        "hotel",
        "180",
        "franken",
    ]
    docs = [tokens("hotel hotel nacht"), tokens("zug klasse"), tokens("hotel zug")]
    scores = bm25_scores(tokens("hotel"), docs)
    assert scores[0] > scores[2] > 0 and scores[1] == 0
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0) and cosine([1, 0], [0, 1]) == 0.0


@pytest.fixture()
def store(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=tmp_path / "unused", embeddings="off"
    )
    runtime = Runtime(settings, definitions={}, model_client=None)
    yield runtime.knowledge, runtime
    runtime.close()


def test_spaces_documents_versions_and_deletion(store, tmp_path):
    ks, runtime = store
    with pytest.raises(KnowledgeError, match="space id"):
        ks.save_space("Bad Id", title="x")
    with pytest.raises(KnowledgeError, match="unknown data class"):
        ks.save_space("rules", title="x", data_class="secret")
    space = ks.save_space(
        "rules",
        title={"de": "Reglemente", "en": "Policies"},
        data_class="confidential",
        owner="alex",
    )
    assert space.source == "local" and space.title["en"] == "Policies"
    doc = ks.add_document("rules", "spesen.txt", POLICY.encode(), actor="alex")
    assert doc["status"] == "indexed" and doc["version"] == 1 and doc["chunks"] >= 1
    assert doc["method"] == "plain_text" and doc["embedded"] is False
    stored = ks.document_path(doc["id"])
    assert stored.is_file() and stored.parent == runtime.settings.knowledge_dir / "rules"
    # A new version keeps the old original on disk and replaces the chunks.
    doc2 = ks.add_document(
        "rules",
        "spesen-v2.txt",
        (POLICY + "\n\nNeu: Taxi bis 50 Franken.").encode(),
        replace=doc["id"],
        actor="erika",
    )
    assert doc2["id"] == doc["id"] and doc2["version"] == 2 and doc2["uploaded_by"] == "erika"
    assert stored.is_file() and ks.document_path(doc["id"]) != stored
    assert ks.search(["rules"], "Taxi")[0].text.endswith("Taxi bis 50 Franken.")
    # Unreadable content is a failed document, visible as such.
    bad = ks.add_document("rules", "blob.bin", b"\xff\xfe\x00")
    assert bad["status"] == "failed" and "neither" in bad["error"]
    overview = {s["id"]: s for s in ks.overview()}
    assert overview["rules"]["documents"] == 2
    # Deletion removes the chunks at once; the row and the file stay marked.
    ks.delete_document("rules", doc["id"])
    assert ks.search(["rules"], "Taxi") == []
    assert ks.document(doc["id"])["deleted"] is True and ks.document_path(doc["id"]).is_file()
    with pytest.raises(KnowledgeError):
        ks.delete_document("rules", doc["id"])
    # Archiving makes the space unusable for steps.
    ks.archive_space("rules")
    with pytest.raises(KnowledgeError, match="unknown or archived"):
        ks.require(["rules"])
    with pytest.raises(KnowledgeError):
        ks.add_document("rules", "x.txt", b"x")


def test_retrieval_is_lexical_by_default_and_hybrid_with_embeddings(store):
    ks, _ = store
    ks.save_space("rules", title="Reglemente")
    ks.add_document("rules", "spesen.txt", POLICY.encode())
    hits = ks.search(["rules"], "Wie teuer darf das Hotel sein?")
    assert hits and hits[0].document_title == "spesen" and "Hotel" in hits[0].text
    assert hits[0].as_dict()["source"] == "spesen §1"
    assert ks.search(["rules"], "") == [] and ks.search([], "hotel") == []
    # With an embedder the chunks carry vectors and the query is embedded too.
    ks.embedder = FakeEmbedder()
    doc = ks.add_document("rules", "essen.txt", b"Kundenessen bis 60 Franken.\n\nAlkohol nie.")
    assert doc["embedded"] is True
    row = ks.conn.execute(
        "SELECT embedding FROM knowledge_chunks WHERE document_id = ?", (doc["id"],)
    ).fetchone()
    assert json.loads(row["embedding"]) == [0.0, 0.0, 1.0, 1.0]
    hits = ks.search(["rules"], "Alkohol beim Essen")
    assert hits[0].document_title == "essen"

    # Fastembed wrapper: injected loader, no download.
    class Model:
        def embed(self, texts):
            return [[0.5, 0.5] for _ in texts]

    fe = FastembedEmbedder("x/y", loader=lambda name: Model())
    assert fe.available() and fe.name == "fastembed:y" and fe.embed(["a"]) == [[0.5, 0.5]]


TEMPLATE = """
id = "answer"
version = 1
[policy.privacy]
category = "internal"
[steps.a]
kind = "trigger"
config = { source = "folder" }
next = { fired = "work" }
[steps.work]
kind = "agent_step"
next = { done = "end", failed = "end" }
[steps.work.config]
task = "answer"
context = { document = true, knowledge = ["rules"] }
require_grounding = true
"""


class FakeModel:
    clearance = "confidential"

    def __init__(self):
        self.payloads = []

    def complete_structured(self, task, payload, schema_hint=None):
        self.payloads.append(payload)
        return ModelResponse(
            data={"ok": True, "confidence": 0.9},
            model_ref="claude-haiku-4-5",
            confidence=0.9,
            tokens_in=10,
            tokens_out=5,
        )


def _ask(runtime, name):
    # Explicit UTF-8: the core reads documents as UTF-8, Windows would write cp1252.
    (runtime.settings.inbox_dir / name).write_text("Hotel Zürich 240 Franken?", encoding="utf-8")


def test_agent_step_binds_spaces_fail_closed_and_raises_the_demand(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=tmp_path / "unused", embeddings="off"
    )
    model = FakeModel()
    runtime = Runtime(
        settings, definitions={"answer": parse_template(TEMPLATE)}, model_client=model
    )
    try:
        # Space missing: the step fails, nothing is called.
        _ask(runtime, "frage1.txt")
        runtime.tick()
        assert model.payloads == []
        rows = list(runtime.conn.execute("SELECT detail FROM diagnostic_events"))
        assert any("unknown or archived" in r["detail"] for r in rows)
        # Space present but empty and require_grounding: fails too.
        runtime.knowledge.save_space("rules", title="Reglemente", data_class="confidential")
        _ask(runtime, "frage2.txt")
        runtime.tick()
        assert model.payloads == []
        assert any(
            "no grounding" in r["detail"]
            for r in runtime.conn.execute("SELECT detail FROM diagnostic_events")
        )
        # With content the model sees the passages with provenance...
        runtime.knowledge.add_document("rules", "spesen.txt", POLICY.encode())
        _ask(runtime, "frage3.txt")
        runtime.tick()
        [payload] = model.payloads
        block = payload["knowledge"]
        assert block["grounded"] is True and "Hotel" in block["query"]
        [hit] = block["spaces"]["rules"]["hits"][:1]
        assert hit["source"] == "spesen §1" and "180 Franken" in hit["text"]
        # ...and the call was logged with the space's (higher) data class.
        row = runtime.conn.execute("SELECT datenklasse FROM llm_interactions").fetchone()
        assert row["datenklasse"] == "confidential"
        definition = runtime.engine.definitions["answer"]
        assert runtime._need(definition, definition.steps["work"].config) == "confidential"
        # A sensitive space pushes the demand beyond a remote model's clearance.
        runtime.knowledge.save_space("rules", title="Reglemente", data_class="sensitive")
        assert runtime._dominanz_violation(definition, definition.steps["work"].config)
    finally:
        runtime.close()


def test_bundle_declares_spaces_and_the_runtime_seeds_them(tmp_path):
    bundle = tmp_path / "apps" / "demo"
    (bundle / "knowledge").mkdir(parents=True)
    (bundle / "templates").mkdir()
    (bundle / "app.toml").write_text(
        'format = 1\nid = "demo"\nversion = "0.1.0"\nname = "Demo"\n', encoding="utf-8"
    )
    (bundle / "knowledge" / "policies.toml").write_text(
        'id = "policies"\ntitle = { de = "Reglemente", fr = "Règlements", it = "Regolamenti",'
        ' en = "Policies" }\ndescription = "Interne Reglemente"\ndata_class = "confidential"\n',
        encoding="utf-8",
    )
    (bundle / "templates" / "answer.toml").write_text(
        TEMPLATE.replace('knowledge = ["rules"]', 'knowledge = ["policies", "other"]'),
        encoding="utf-8",
    )
    report = validate_app(bundle)
    assert report.app is not None, [f.message for f in report.findings]
    assert report.app.knowledge["policies"].data_class == "confidential"
    assert any("other" in f.message for f in report.findings if f.severity == "warning")
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=tmp_path / "unused",
        apps_dir=tmp_path / "apps",
        embeddings="off",
    )
    runtime = Runtime(settings)
    try:
        space = runtime.knowledge.space("policies")
        assert space is not None and space.source == "app:demo" and space.title["fr"]
        # The owner edits title and class; a reload does not overwrite them.
        runtime.knowledge.save_space("policies", title="Unsere Reglemente", data_class="internal")
        runtime.reload_templates()
        again = runtime.knowledge.space("policies")
        assert again.title["de"] == "Unsere Reglemente" and again.data_class == "internal"
        assert again.source == "app:demo"
    finally:
        runtime.close()


def test_knowledge_api(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=tmp_path / "unused", embeddings="off"
    )
    runtime = Runtime(settings, definitions={}, model_client=None)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        assert client.get("/api/v1/knowledge").json() == {"spaces": [], "embeddings": None}
        resp = client.put(
            "/api/v1/knowledge/rules",
            json={"title": {"de": "Reglemente"}, "data_class": "confidential", "owner": "alex"},
        )
        assert resp.status_code == 200, resp.text
        assert (
            client.put(
                "/api/v1/knowledge/rules", json={"title": "x", "data_class": "nope"}
            ).status_code
            == 422
        )
        resp = client.post(
            "/api/v1/knowledge/rules/documents",
            json={
                "name": "spesen.txt",
                "content_base64": base64.b64encode(POLICY.encode()).decode(),
                "title": "Spesenreglement",
            },
        )
        assert resp.status_code == 200, resp.text
        doc = resp.json()
        assert doc["title"] == "Spesenreglement" and doc["status"] == "indexed"
        space = client.get("/api/v1/knowledge/rules").json()
        assert [d["id"] for d in space["documents"]] == [doc["id"]]
        hits = client.get("/api/v1/knowledge/rules/search", params={"q": "Hotel"}).json()
        assert hits and hits[0]["source"] == "Spesenreglement §1"
        file = client.get(f"/api/v1/knowledge/rules/documents/{doc['id']}/file")
        assert file.status_code == 200 and b"Spesenreglement" in file.content
        assert client.delete(f"/api/v1/knowledge/rules/documents/{doc['id']}").json() == {
            "ok": True
        }
        assert client.get("/api/v1/knowledge/rules/search", params={"q": "Hotel"}).json() == []
        assert client.delete("/api/v1/knowledge/rules").json() == {"ok": True}
        assert client.get("/api/v1/knowledge").json()["spaces"] == []
        assert client.get("/api/v1/knowledge/nope").status_code == 404
        assert client.get("/api/v1/knowledge/nope/search", params={"q": "x"}).status_code == 404


def test_search_index_is_cached_and_follows_changes(store):
    """The tokenised index is kept between searches (speed) and rebuilt
    the moment a document is added or removed - never a stale hit."""
    import time as _time

    knowledge, _rt = store
    knowledge.save_space("big", title="Gross", data_class="public")
    text = "\n\n".join(f"Abschnitt {i}: Gebühren und Abfall im Ortsteil {i % 17}." for i in range(400))  # noqa: E501
    knowledge.add_document("big", "lang.txt", text.encode())
    first = knowledge.search(["big"], "Rathaus Öffnungszeiten")
    assert first == []
    doc = knowledge.add_document("big", "rathaus.txt", "Das Rathaus hat Montag 8 bis 12 Uhr geöffnet.".encode())  # noqa: E501
    hits = knowledge.search(["big"], "Rathaus Öffnungszeiten")
    assert hits and hits[0].document_title.startswith("rathaus")
    t0 = _time.perf_counter()
    for _ in range(50):
        knowledge.search(["big"], "Gebühren Abfall Ortsteil")
    assert (_time.perf_counter() - t0) / 50 < 0.05  # cached: well under 50 ms a search
    knowledge.delete_document("big", doc["id"])
    assert knowledge.search(["big"], "Rathaus Öffnungszeiten") == []


def test_cached_ranking_equals_plain_bm25(store):
    from aitmation_core.knowledge import bm25_scores, tokens

    knowledge, _rt = store
    knowledge.save_space("sx", title="S", data_class="public")
    for i, body in enumerate(["Hund Steuer Hund", "Katze Steuer", "Abfall Kalender Steuer", "Hund Abfall"]):  # noqa: E501
        knowledge.add_document("sx", f"d{i}.txt", body.encode())
    index = knowledge._index(["sx"])
    query = tokens("Hund Steuer")
    plain = bm25_scores(query, [tokens(r[4]) for r in index.rows])
    assert [round(x, 9) for x in index.bm25(query)] == [round(x, 9) for x in plain]


def test_rebuild_after_a_change_prepares_only_new_chunks(store):
    knowledge, rt = store
    knowledge.save_space("inc", title="Inc", data_class="public")
    knowledge.add_document("inc", "a.txt", b"Hund Steuer Gemeinde")
    assert knowledge.search(["inc"], "Hund")
    prepared = set(knowledge._chunk_cache)
    knowledge.add_document("inc", "b.txt", b"Katze Steuer Gemeinde")
    assert [h.document_title for h in knowledge.search(["inc"], "Katze")] == ["b"]
    assert prepared < set(knowledge._chunk_cache)  # the old chunks were reused, the new one added
    assert rt.warm_knowledge() >= 1


def test_the_same_passage_twice_counts_once(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=tmp_path / "unused", embeddings="off"
    )
    runtime = Runtime(settings)
    store = runtime.knowledge
    store.save_space("sp", title="S", data_class="public")
    text = b"Oeffnungszeiten\nSonntag 10 bis 19 Uhr geoeffnet."
    store.add_document("sp", "a.txt", text)
    store.add_document("sp", "b.txt", text)  # the same page under a second address
    store.add_document("sp", "c.txt", b"Gutschein\nSonntag Gutschein kaufen.")
    hits = store.search(["sp"], "Sonntag geoeffnet", top_k=3)
    assert len([h for h in hits if "10 bis 19" in h.text]) == 1 and len(hits) == 2
    runtime.close()
