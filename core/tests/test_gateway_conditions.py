# ruff: noqa: E501 - test data
"""ADR-0056: a deciding gateway carries its conditions - loaded as the
internal action ``route``, evaluated first-match with one default, and
kept by the card canvas."""

from __future__ import annotations

import pytest

from aitmation_core.bpmn import BpmnError, parse_bpmn
from aitmation_core.bpmn_graph import apply_graph, read_graph
from aitmation_core.runtime import evaluate_routes

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:aimn="https://aitmation.com/schema/aimn/1" id="d" targetNamespace="https://aitmation.com/solutions">
  <bpmn:process id="p" isExecutable="true">
    <bpmn:startEvent id="s"><bpmn:extensionElements><aimn:trigger source="manual"/></bpmn:extensionElements></bpmn:startEvent>
    <bpmn:serviceTask id="read"><bpmn:extensionElements><aimn:toolCall action="archive"/></bpmn:extensionElements></bpmn:serviceTask>
    <bpmn:endEvent id="big"/><bpmn:endEvent id="small"/><bpmn:endEvent id="other"/>
    <bpmn:sequenceFlow id="f0" sourceRef="s" targetRef="read"/>
    <bpmn:sequenceFlow id="f1" name="done" sourceRef="read" targetRef="g"/>
"""
TAIL = "  </bpmn:process>\n</bpmn:definitions>\n"


def process(gateway: str, flows: str) -> str:
    return HEAD + gateway + flows + TAIL


GOOD = process(
    '<bpmn:exclusiveGateway id="g" name="Betrag?" default="f_other"/>',
    """<bpmn:sequenceFlow id="f_big" name="big" sourceRef="g" targetRef="big">
      <bpmn:extensionElements><aimn:condition input="trigger.amount" test="&gt;= 1000"/>
      <aimn:condition input="trigger.currency" test="&quot;CHF&quot;"/></bpmn:extensionElements></bpmn:sequenceFlow>
    <bpmn:sequenceFlow id="f_small" name="small" sourceRef="g" targetRef="small">
      <bpmn:extensionElements><aimn:when match="any"><aimn:condition input="trigger.amount" test="&lt; 10"/>
      <aimn:condition input="steps.read.result.flag" test="true"/></aimn:when></bpmn:extensionElements></bpmn:sequenceFlow>
    <bpmn:sequenceFlow id="f_other" name="otherwise" sourceRef="g" targetRef="other"/>
""",
)


def test_the_gateway_is_a_route_step():
    definition = parse_bpmn(GOOD, "t")
    step = definition.steps["g"]
    assert step.config["action"] == "route" and step.config["default"] == "otherwise"
    assert [r["outcome"] for r in step.config["routes"]] == ["big", "small"]
    assert step.config["routes"][1]["match"] == "any"
    assert definition.steps["read"].next == {"done": "g"}
    assert set(step.next) == {"big", "small", "otherwise"}


def test_first_match_wins_and_every_branch_is_explained():
    config = parse_bpmn(GOOD, "t").steps["g"].config
    ctx = {"trigger": {"amount": "1500", "currency": "CHF"}, "steps": {"read": {"result": {"flag": True}}}}
    evaluated, taken = evaluate_routes(config, ctx)
    assert taken == "big"
    assert [e["taken"] for e in evaluated] == [True, False, False]
    assert evaluated[1]["result"] is True  # small held too, but came second
    assert evaluated[0]["conditions"][0] == {"input": "trigger.amount", "test": ">= 1000", "value": "1500", "result": True}
    _e, taken = evaluate_routes(config, {"trigger": {"amount": 500, "currency": "CHF"}, "steps": {}})
    assert taken == "otherwise"
    _e, taken = evaluate_routes(config, {"trigger": {"amount": 5}, "steps": {}})
    assert taken == "small"


@pytest.mark.parametrize(
    ("gateway", "flows", "message"),
    [
        ('<bpmn:exclusiveGateway id="g"/>',
         '<bpmn:sequenceFlow id="a" name="a" sourceRef="g" targetRef="big"><bpmn:extensionElements><aimn:condition input="trigger.x" test="1"/></bpmn:extensionElements></bpmn:sequenceFlow>'
         '<bpmn:sequenceFlow id="b" name="b" sourceRef="g" targetRef="small"><bpmn:extensionElements><aimn:condition input="trigger.x" test="2"/></bpmn:extensionElements></bpmn:sequenceFlow>',
         "otherwise"),
        ('<bpmn:exclusiveGateway id="g" default="b"/>',
         '<bpmn:sequenceFlow id="a" name="a" sourceRef="g" targetRef="big"><bpmn:extensionElements><aimn:condition input="steps.nope.x" test="1"/></bpmn:extensionElements></bpmn:sequenceFlow>'
         '<bpmn:sequenceFlow id="b" name="b" sourceRef="g" targetRef="small"/>',
         "does not have"),
        ('<bpmn:exclusiveGateway id="g" default="b"/>',
         '<bpmn:sequenceFlow id="a" name="a" sourceRef="g" targetRef="big"><bpmn:extensionElements><aimn:condition input="answer" test="1"/></bpmn:extensionElements></bpmn:sequenceFlow>'
         '<bpmn:sequenceFlow id="b" name="b" sourceRef="g" targetRef="small"/>',
         "must start with"),
        ('<bpmn:exclusiveGateway id="g"/>',
         '<bpmn:sequenceFlow id="a" name="a" sourceRef="g" targetRef="big"><bpmn:extensionElements><aimn:condition input="trigger.x" test="1"/></bpmn:extensionElements></bpmn:sequenceFlow>'
         '<bpmn:sequenceFlow id="b" name="b" sourceRef="g" targetRef="small"/><bpmn:sequenceFlow id="c" name="c" sourceRef="g" targetRef="other"/>',
         "more than one default"),
    ],
)
def test_load_errors(gateway, flows, message):
    with pytest.raises(BpmnError, match=message):
        parse_bpmn(process(gateway, flows), "t")


def test_gateways_may_follow_each_other():
    text = process(
        '<bpmn:exclusiveGateway id="g" default="f_on"/><bpmn:exclusiveGateway id="h" default="f_o"/>',
        """<bpmn:sequenceFlow id="f_big" name="big" sourceRef="g" targetRef="big"><bpmn:extensionElements><aimn:condition input="trigger.x" test="1"/></bpmn:extensionElements></bpmn:sequenceFlow>
        <bpmn:sequenceFlow id="f_on" name="otherwise" sourceRef="g" targetRef="h"/>
        <bpmn:sequenceFlow id="f_s" name="small" sourceRef="h" targetRef="small"><bpmn:extensionElements><aimn:condition input="trigger.x" test="2"/></bpmn:extensionElements></bpmn:sequenceFlow>
        <bpmn:sequenceFlow id="f_o" name="otherwise" sourceRef="h" targetRef="other"/>""",
    )
    definition = parse_bpmn(text, "t")
    assert definition.steps["g"].next["otherwise"] == "h"


def test_the_canvas_keeps_conditions_and_default():
    graph = read_graph(GOOD)
    edges = {e["id"]: e for e in graph["edges"]}
    assert edges["f_big"]["when"]["conditions"][0] == {"input": "trigger.amount", "test": ">= 1000"}
    assert edges["f_small"]["when"]["match"] == "any"
    assert edges["f_other"].get("default") is True
    # move a node, edges without "when": the conditions stay where they were
    for node in graph["nodes"]:
        node["x"] += 10
    plain = [{k: v for k, v in e.items() if k not in ("when", "default")} for e in graph["edges"]]
    again = apply_graph(GOOD, {**graph, "edges": plain})
    definition = parse_bpmn(again, "t")
    assert definition.steps["g"].config["routes"][0]["conditions"][1]["test"] == '"CHF"'
    assert definition.steps["g"].config["default"] == "otherwise"
    # the graph can change a condition
    changed = [dict(e) for e in graph["edges"]]
    for e in changed:
        if e["outcome"] == "big":
            e["when"] = {"match": "all", "conditions": [{"input": "trigger.amount", "test": "> 5000"}]}
    definition = parse_bpmn(apply_graph(GOOD, {**graph, "edges": changed}), "t")
    assert definition.steps["g"].config["routes"][0]["conditions"] == [{"input": "trigger.amount", "test": "> 5000"}]
