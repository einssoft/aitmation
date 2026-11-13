"""The card canvas's graph (ADR-0040): read from BPMN, applied back onto
it without touching what the canvas does not edit."""

from pathlib import Path

import pytest

from aitmation_core import bpmn_graph
from aitmation_core.aimn import load_briefings
from aitmation_core.bpmn import parse_bpmn
from aitmation_core.solution import parse_constants

REPO = Path(__file__).resolve().parents[2]
READ = REPO / "apps" / "document-basics" / "processes" / "document-read.bpmn"
REVIEW = REPO / "apps" / "document-basics" / "processes" / "document-review.bpmn"
PARALLEL = REPO / "docs" / "examples" / "demo-triage" / "processes" / "triage-parallel.bpmn"
BRIEFINGS = load_briefings(REPO / "apps" / "document-basics" / "ai")


def _definition(text: str, path: Path):
    return parse_bpmn(text, str(path), BRIEFINGS)


def test_read_graph_lists_nodes_with_kind_position_and_binding():
    graph = bpmn_graph.read_graph(READ.read_text(encoding="utf-8"))
    assert graph["process"] == "document-read"
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert by_id["intake"]["kind"] == "start" and by_id["intake"]["source"] == "chain"
    assert by_id["read"]["kind"] == "ai" and by_id["read"]["ref"] == "read"
    assert by_id["to_document_review"] == {
        **by_id["to_document_review"], "kind": "end", "ref": "document-review"
    }
    assert by_id["read"]["x"] == 360 and by_id["read"]["w"] == 100
    edges = {(e["source"], e["target"]): e["outcome"] for e in graph["edges"]}
    assert edges[("read", "to_document_review")] == "done"
    review = bpmn_graph.read_graph(REVIEW.read_text(encoding="utf-8"))
    gate = next(n for n in review["nodes"] if n["id"] == "review")
    assert gate["kind"] == "gate" and gate["deadline"] == 2 * 86400


def test_round_trip_keeps_the_definition_and_the_extensions():
    text = READ.read_text(encoding="utf-8")
    graph = bpmn_graph.read_graph(text)
    out = bpmn_graph.apply_graph(text, graph)
    before, after = _definition(text, READ), _definition(out, READ)
    assert after.steps.keys() == before.steps.keys()
    for sid, step in before.steps.items():
        assert after.steps[sid].config == step.config
        assert after.steps[sid].next == step.next
    assert after.policies == before.policies and after.parameters == before.parameters
    assert "aimn:policy" in out and "aimn:contract" in out


def test_moving_a_card_only_changes_the_diagram():
    text = READ.read_text(encoding="utf-8")
    graph = bpmn_graph.read_graph(text)
    for node in graph["nodes"]:
        if node["id"] == "read":
            node["x"], node["y"] = 500, 300
    out = bpmn_graph.apply_graph(text, graph)
    assert 'x="500" y="300"' in out
    unchanged = _definition(text, READ).steps["read"].config
    assert _definition(out, READ).steps["read"].config == unchanged


def test_adding_a_gate_between_ai_step_and_end():
    text = READ.read_text(encoding="utf-8")
    graph = bpmn_graph.read_graph(text)
    graph["nodes"].append({"id": "check", "kind": "gate", "label": "Prüfen", "x": 460, "y": 120})
    graph["edges"] = [
        e for e in graph["edges"] if not (e["source"] == "read" and e["outcome"] == "done")
    ] + [
        {"source": "read", "target": "check", "outcome": "done"},
        {"source": "check", "target": "to_document_review", "outcome": "approved"},
        {"source": "check", "target": "finished", "outcome": "rejected"},
    ]
    out = bpmn_graph.apply_graph(text, graph)
    definition = _definition(out, READ)
    assert definition.steps["check"].kind.value == "human_gate"
    assert definition.steps["read"].next["done"] == "check"
    assert definition.steps["check"].next["approved"] == "process:document-review"
    assert definition.steps["check"].next["rejected"] is None  # the end
    assert "<bpmn:userTask id=\"check\" name=\"Prüfen\">" in out
    assert 'bpmnElement="check"' in out


def test_removing_a_node_takes_its_boundary_event_along():
    text = REVIEW.read_text(encoding="utf-8")
    graph = bpmn_graph.read_graph(text)
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "review"]
    graph["edges"] = [{"source": "intake", "target": "finished", "outcome": "fired"}]
    out = bpmn_graph.apply_graph(text, graph)
    assert "review_deadline" not in out and "userTask" not in out
    definition = _definition(out, REVIEW)
    assert list(definition.steps) == ["intake"]


def test_changing_a_kind_is_a_remove_and_add():
    text = READ.read_text(encoding="utf-8")
    graph = bpmn_graph.read_graph(text)
    for node in graph["nodes"]:
        if node["id"] == "read":
            node.update(kind="gate", ref="")
    for edge in graph["edges"]:
        if edge["source"] == "read":
            edge["outcome"] = {"done": "approved", "failed": "rejected"}[edge["outcome"]]
    out = bpmn_graph.apply_graph(text, graph)
    assert "aimn:aiTask" not in out
    assert _definition(out, READ).steps["read"].kind.value == "human_gate"


def test_new_nodes_need_their_binding():
    text = READ.read_text(encoding="utf-8")
    graph = bpmn_graph.read_graph(text)
    graph["nodes"].append({"id": "extra", "kind": "ai"})
    with pytest.raises(bpmn_graph.GraphError, match="briefing"):
        bpmn_graph.apply_graph(text, graph)
    graph["nodes"][-1] = {"id": "Extra", "kind": "ai", "ref": "read"}
    with pytest.raises(bpmn_graph.GraphError, match="lowercase"):
        bpmn_graph.apply_graph(text, graph)
    graph["nodes"][-1] = {"id": "extra", "kind": "ai", "ref": "read"}
    graph["edges"].append({"source": "finished", "target": "extra"})
    with pytest.raises(bpmn_graph.GraphError, match="end has no outgoing"):
        bpmn_graph.apply_graph(text, graph)


def test_parallel_gateways_survive_the_round_trip():
    text = PARALLEL.read_text(encoding="utf-8")
    graph = bpmn_graph.read_graph(text)
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    assert "parallel" in kinds.values()
    out = bpmn_graph.apply_graph(text, graph)
    demo = REPO / "docs" / "examples" / "demo-triage"
    constants = parse_constants((demo / "constants" / "company.toml").read_text(), "c")
    briefings = load_briefings(demo / "ai")
    before = parse_bpmn(text, str(PARALLEL), briefings, constants=constants)
    after = parse_bpmn(out, str(PARALLEL), briefings, constants=constants)
    assert after.steps.keys() == before.steps.keys()
    for sid, step in before.steps.items():
        assert after.steps[sid].forks == step.forks and after.steps[sid].next == step.next
