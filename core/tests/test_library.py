"""The workshop's library (library.py): agents and tools as building
blocks across solutions, made from short forms into the instance's own
solution, with "used in" and "adapt a copy"."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core import library
from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.solution import parse_tool
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
DEMO = REPO / "docs" / "examples" / "demo-triage"


@pytest.fixture()
def world(tmp_path):
    apps = tmp_path / "apps"
    apps.mkdir()
    shutil.copytree(DEMO, apps / "demo-triage")
    runtime = Runtime(
        Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=apps)
    )
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        yield client, runtime, apps


def test_the_library_lists_blocks_with_origin_and_use(world):
    client, _runtime, _apps = world
    view = client.get("/api/v1/library").json()
    assert view["has_own"] is False and view["form_kinds"] == ["page", "search", "json", "api"]
    agents = {a["id"]: a for a in view["agents"]}
    used = [a for a in agents.values() if a["used_in"]]
    assert used and all(a["solution"] == "demo-triage" and not a["own"] for a in used)
    first = used[0]["used_in"][0]
    assert first["solution"] == "demo-triage" and first["process"] and first["step"]
    tool = next(t for t in view["tools"] if t["id"] == "notify-sender")
    assert tool["kind"] == "action" and tool["effect"] == "external" and tool["used_in"]


def test_an_agent_is_made_in_the_own_solution_and_edited_as_a_form(world):
    client, runtime, apps = world
    made = client.post(
        "/api/v1/library/agents",
        json={
            "name": "Offert-Prüfer",
            "purpose": "Liest Offertanfragen\nund schlägt eine Antwort vor.",
        },
    )
    assert made.status_code == 201, made.text
    assert made.json() == {"ok": True, "solution": "own", "id": "offert-pruefer", "warnings": []}
    assert (apps / "own" / "solution.toml").is_file() and "own" in runtime.apps
    again = client.post("/api/v1/library/agents", json={"name": "Offert Prüfer"}).json()
    assert again["id"] == "offert-pruefer-2"  # a free id, never an overwrite
    page = client.post("/api/v1/library/tools", json={"kind": "page", "name": "Seite lesen"}).json()
    # the form of the agent's page: name, purpose, knowledge, tools
    runtime.knowledge.save_space("handbook", title="Handbuch", data_class="internal")
    saved = client.put(
        "/api/v1/solutions/own/elements/ai/offert-pruefer/fields",
        json={
            "name": {"de": "Offertprüfer"},
            "description": {"de": "Prüft Offerten."},
            "instructions": {"de": "Lies die Anfrage und schlage eine Antwort vor."},
            "limits": ["Nenne nie Preise."],
            "model_role": "default",
            "tools": [page["id"]],
            "knowledge": ["handbook"],
            "output": {"answer": "the proposed answer"},
        },
    )
    assert saved.status_code == 200, saved.text
    element = client.get("/api/v1/solutions/own/elements/ai/offert-pruefer").json()["summary"]
    assert element["name"] == {"de": "Offertprüfer"} and element["knowledge"] == ["handbook"]
    assert (
        element["tools"] == ["seite-lesen"] and "Offertprüfer" not in element["task"]
    )  # a label, not prompt
    assert (
        client.put(
            "/api/v1/solutions/own/elements/ai/offert-pruefer/fields",
            json={"instructions": {"de": "x"}, "model_role": "default", "knowledge": ["nope"]},
        ).status_code
        == 422
    )
    # the step panel's save (no labels in the body) keeps them
    client.put(
        "/api/v1/solutions/own/elements/ai/offert-pruefer/fields",
        json={"instructions": {"de": "Neu."}, "model_role": "default", "output": {"answer": "a"}},
    )
    element = client.get("/api/v1/solutions/own/elements/ai/offert-pruefer").json()["summary"]
    assert element["name"] == {"de": "Offertprüfer"} and element["knowledge"] == ["handbook"]
    view = client.get("/api/v1/library").json()
    assert view["has_own"] and view["agents"][0]["own"] and view["agents"][0]["solution"] == "own"
    assert any(k["id"] == "handbook" for k in view["knowledge"])


def test_tools_from_the_four_forms(world):
    client, runtime, _apps = world

    def make(**body):
        return client.post("/api/v1/library/tools", json=body)

    assert make(kind="search", name="Websuche").json()["id"] == "websuche"
    assert (
        make(kind="search", name="Brave", form={"provider": "brave"}).status_code == 422
    )  # needs a secret
    assert (
        make(
            kind="search", name="Brave", form={"provider": "brave", "secret": "brave_key"}
        ).status_code
        == 201
    )
    made = make(
        kind="json",
        name="Wechselkurs",
        description="Liest den Tageskurs.",
        form={
            "url": "https://api.example.org/rates",
            "params": ["base"],
            "response": {"rate": "rates.CHF"},
        },
    )
    assert made.status_code == 201, made.text
    assert (
        make(kind="json", name="Ohne", form={"url": "https://api.example.org/x"}).status_code == 422
    )
    assert (
        make(
            kind="json", name="Lokal", form={"url": "http://intern/x", "response": {"a": "a"}}
        ).status_code
        == 422
    )
    api = make(
        kind="api",
        name="Ticket anlegen",
        form={
            "method": "post",
            "url": "https://desk.example.org/v1/projects/{input:project}/tickets",
            "params": ["project", "title", "text"],
            "secret": "desk_token",
            "response": {"ticket": "data.id"},
        },
    )
    assert api.status_code == 201, api.text
    assert (
        make(
            kind="api",
            name="Kaputt",
            form={"url": "https://desk.example.org/{input:nope}", "params": []},
        ).status_code
        == 422
    )
    assert make(kind="shell", name="Nein").status_code == 422
    tools = {t["id"]: t for t in client.get("/api/v1/library").json()["tools"] if t["own"]}
    assert {k: tools[k]["kind"] for k in tools} == {
        "websuche": "search",
        "brave": "search",
        "wechselkurs": "json",
        "ticket-anlegen": "api",
    }
    assert (
        tools["wechselkurs"]["effect"] == "read" and tools["ticket-anlegen"]["effect"] == "external"
    )
    ticket = runtime.apps["own"].tools["ticket-anlegen"]
    assert ticket.http.headers == {"Authorization": "Bearer {secret:desk_token}"}
    assert ticket.http.body == {
        "title": "{input:title}",
        "text": "{input:text}",
    }  # the address carries project


def test_a_copy_of_a_shipped_block_is_ones_own(world):
    client, runtime, _apps = world
    agent = next(a for a in client.get("/api/v1/library").json()["agents"] if not a["tools"])
    copied = client.post(
        "/api/v1/library/copy", json={"solution": "demo-triage", "kind": "ai", "id": agent["id"]}
    )
    assert copied.status_code == 201, copied.text
    assert copied.json()["solution"] == "own" and copied.json()["id"] == agent["id"]
    assert agent["id"] in runtime.apps["own"].briefings
    assert (
        runtime.apps["own"].briefings[agent["id"]].model_role
        in runtime.apps["own"].manifest.model_roles
    )
    tool = client.post(
        "/api/v1/library/copy",
        json={"solution": "demo-triage", "kind": "tool", "id": "notify-sender"},
    ).json()
    assert tool["id"] == "notify-sender" and "notify-sender" in runtime.apps["own"].tools
    assert (
        client.post(
            "/api/v1/library/copy", json={"solution": "own", "kind": "tool", "id": "notify-sender"}
        ).status_code
        == 409
    )


def test_declarations_from_forms_parse(tmp_path):
    text = library.tool_toml("page", "read-page", "Seite lesen", "", "de", {})
    tool = parse_tool(text, "x")
    assert (
        tool.effect == "read"
        and tool.fetch.url == "{input:url}"
        and library.tool_kind(tool) == "page"
    )
    assert library.slug("  Ärger & Söhne 2 ") == "aerger-soehne-2" and library.slug("42") == "x-42"
    assert library.retitle('id = "a"\nname = "A"\n', "a", "b").startswith('id = "b"')
    with pytest.raises(library.LibraryError):
        library.tool_toml("api", "x", "X", "", "de", {"url": "ftp://x"})
