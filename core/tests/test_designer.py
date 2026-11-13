"""The process designer (ADR-0062): step settings, the start form, gate
deadlines and 'for each' travel through the card graph; the catalog
lists the building blocks; paths list what a step can bind."""

from __future__ import annotations

import inspect
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core import designer
from aitmation_core import runtime as runtime_module
from aitmation_core.bpmn_graph import GraphError, apply_graph, read_graph
from aitmation_core.config import Settings
from aitmation_core.definition import INTERNAL_ACTIONS
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app
from conftest import needs_solution

pytestmark = needs_solution("appointments")


REPO = Path(__file__).resolve().parents[2]
BOOKING = REPO / "apps" / "appointments" / "processes" / "booking-request.bpmn"


def test_settings_start_form_and_deadline_are_read():
    graph = read_graph(BOOKING.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert nodes["mail_confirmed"]["config"]["inputs"]["to"] == "trigger.email"
    assert nodes["log_request"]["config"]["collection"] == "appointment-requests"
    assert nodes["confirm"]["config"]["approving"] == "confirmed, proposed, declined"
    assert nodes["confirm"]["deadline"] == 4 * 3600
    assert nodes["request"].get("config") is None  # manual start: nothing to set
    names = [c["name"] for c in graph["contract"]]
    assert names[:4] == ["name", "email", "service", "wish"]
    assert {"name": "phone", "description": "phone, optional", "default": ""} in graph["contract"]


def test_unchanged_graph_keeps_the_file_meaning():
    text = BOOKING.read_text(encoding="utf-8")
    graph = read_graph(text)
    again = read_graph(apply_graph(text, graph))
    for before, after in zip(graph["nodes"], again["nodes"], strict=True):
        assert before == after

    def strip(edges):
        return [{k: v for k, v in e.items() if k != "id"} for e in edges]

    assert graph["contract"] == again["contract"] and strip(graph["edges"]) == strip(again["edges"])


def test_settings_contract_deadline_and_loop_are_written():
    text = BOOKING.read_text(encoding="utf-8")
    graph = read_graph(text)
    nodes = {n["id"]: n for n in graph["nodes"]}
    nodes["mail_confirmed"]["config"]["inputs"]["subject"] = "template:Termin {trigger.wish}"
    nodes["confirm"]["config"] = {"approving": "confirmed, declined", "rule": "service"}
    nodes["confirm"]["deadline"] = 3600
    nodes["log_request"]["loop"] = {"over": "trigger.items", "as": "item", "max": 5}
    graph["nodes"].append(
        {
            "id": "notify",
            "kind": "action",
            "ref": "knowledge",
            "label": "Notiz",
            "x": 900,
            "y": 300,
            "config": {"space": "notes", "inputs": {"text": "steps.confirm.comment"}},
        }
    )
    graph["edges"].append({"source": "archive", "target": "notify", "outcome": "failed"})
    graph["contract"] = [
        *graph["contract"][:2],
        {"name": "count", "description": "how many", "default": "1"},
    ]
    out = read_graph(apply_graph(text, graph))
    nodes = {n["id"]: n for n in out["nodes"]}
    assert (
        nodes["mail_confirmed"]["config"]["inputs"]["subject"] == "template:Termin {trigger.wish}"
    )
    assert nodes["confirm"]["config"] == {"approving": "confirmed, declined", "rule": "service"}
    assert nodes["confirm"]["deadline"] == 3600
    assert nodes["log_request"]["loop"] == {"over": "trigger.items", "as": "item", "max": 5}
    assert nodes["notify"]["config"] == {
        "space": "notes",
        "inputs": {"text": "steps.confirm.comment"},
    }
    assert [c["name"] for c in out["contract"]] == ["name", "email", "count"]
    # removing the deadline and the loop
    nodes["confirm"]["deadline"] = None
    nodes["log_request"]["loop"] = None
    out = read_graph(apply_graph(apply_graph(text, graph), out))
    nodes = {n["id"]: n for n in out["nodes"]}
    assert "deadline" not in nodes["confirm"] and "loop" not in nodes["log_request"]


@pytest.mark.parametrize(
    "config",
    [
        {"bad key": 1},
        {"a": {"b": {"c": {"d": 1}}}},
        {"a": [{"x": 1}]},
        {"text": "x" * 5000},
    ],
)
def test_bad_settings_are_refused(config):
    text = BOOKING.read_text(encoding="utf-8")
    graph = read_graph(text)
    next(n for n in graph["nodes"] if n["id"] == "archive")["config"] = config
    with pytest.raises(GraphError):
        apply_graph(text, graph)


def test_cards_without_settings_refuse_them():
    text = BOOKING.read_text(encoding="utf-8")
    graph = read_graph(text)
    next(n for n in graph["nodes"] if n["id"] == "answered")["config"] = {"x": 1}
    with pytest.raises(GraphError, match="no settings"):
        apply_graph(text, graph)


def test_action_schemas_match_the_runtime():
    """Every core action the designer offers is one the runtime runs; the
    internal ones are internal there too (no gate needed)."""
    source = inspect.getsource(runtime_module.Runtime)
    for action, schema in designer.ACTION_SCHEMAS.items():
        assert f'action == "{action}"' in source or action == "export", action
        assert (schema["effect"] == "internal") == (action in INTERNAL_ACTIONS), action


def test_upstream_walks_back_through_branches():
    graph = read_graph(BOOKING.read_text(encoding="utf-8"))
    assert designer.upstream(graph, "log_request")[:1] in (
        ["mail_confirmed"],
        ["mail_proposed"],
        ["mail_declined"],
    )
    assert set(designer.upstream(graph, "archive")) == {
        "log_request",
        "mail_confirmed",
        "mail_proposed",
        "mail_declined",
        "confirm",
        "request",
    }
    assert designer.upstream(graph, "request") == []


@pytest.fixture()
def web(tmp_path):
    apps_dir = tmp_path / "apps"
    for name in ("appointments", "company-profile", "tasks"):
        shutil.copytree(REPO / "apps" / name, apps_dir / name)
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=REPO / "core" / "templates", apps_dir=apps_dir
    )
    runtime = Runtime(settings)
    yield TestClient(create_app(runtime)), runtime
    runtime.close()


def test_catalog_and_paths_over_the_api(web):
    client, _ = web
    cat = client.get("/api/v1/solutions/appointments/designer/catalog").json()
    assert {a["id"] for a in cat["actions"]} >= {"mail", "record", "rows", "knowledge", "archive"}
    assert [t["id"] for t in cat["triggers"]][:2] == ["manual", "schedule"]
    assert "appointment-requests" in cat["collections"]
    fields = {f["name"] for f in cat["collections"]["appointment-requests"]["fields"]}
    assert {"name", "email", "status"} <= fields
    out = client.get(
        "/api/v1/solutions/appointments/elements/process/booking-request/paths",
        params={"node": "log_request"},
    ).json()
    paths = {p["path"] for p in out["paths"]}
    assert {
        "trigger.email",
        "trigger.wish",
        "steps.confirm.comment",
        "steps.confirm.outcome",
        "steps.mail_confirmed.subject",
        "case.id",
    } <= paths
    assert any(p.startswith("const:appointments.") for p in paths)
    assert (
        client.get(
            "/api/v1/solutions/appointments/elements/process/booking-request/paths",
            params={"node": "nope"},
        ).status_code
        == 404
    )


def test_designer_save_goes_through_validation(web):
    client, runtime = web
    url = "/api/v1/solutions/appointments/elements/process/booking-request/graph"
    graph = client.get(url).json()
    nodes = {n["id"]: n for n in graph["nodes"]}
    nodes["mail_declined"]["config"]["inputs"]["cc"] = "literal:office@example.ch"
    graph["contract"].append(
        {"name": "channel", "description": "where it came from", "default": "web"}
    )
    saved = client.put(
        url, json={"nodes": graph["nodes"], "edges": graph["edges"], "contract": graph["contract"]}
    )
    assert saved.status_code == 200, saved.text
    definition = runtime.engine.definitions["booking-request"]
    assert definition.steps["mail_declined"].config["inputs"]["cc"] == "literal:office@example.ch"
    assert "channel" in definition.contract["input"]
    # a binding the validator refuses (an unknown path) comes back as 422, nothing written
    nodes["mail_declined"]["config"]["inputs"]["cc"] = "nowhere.at.all"
    bad = client.put(url, json={"nodes": graph["nodes"], "edges": graph["edges"]})
    assert bad.status_code == 422
    assert (
        runtime.engine.definitions["booking-request"].steps["mail_declined"].config["inputs"]["cc"]
        == "literal:office@example.ch"
    )
