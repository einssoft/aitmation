# ruff: noqa: E501 - test data
"""ADR-0057: a process has one start event per way in; the payload's
channel (or source) picks it, the rest of the process is shared."""

from __future__ import annotations

import pytest

from aitmation_core.bpmn import BpmnError, parse_bpmn
from aitmation_core.bpmn_graph import read_graph
from aitmation_core.definition import DefinitionError

TWO = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:aimn="https://aitmation.com/schema/aimn/1" id="d" targetNamespace="https://aitmation.com/solutions">
  <bpmn:process id="p" isExecutable="true">
    <bpmn:startEvent id="chat" name="Chat"><bpmn:extensionElements><aimn:trigger source="manual" channel="chat"/></bpmn:extensionElements></bpmn:startEvent>
    <bpmn:startEvent id="messenger"><bpmn:extensionElements><aimn:trigger source="manual" channel="{ch}"/></bpmn:extensionElements></bpmn:startEvent>
    <bpmn:startEvent id="weekly"><bpmn:extensionElements><aimn:trigger source="schedule" every="week"/></bpmn:extensionElements></bpmn:startEvent>
    <bpmn:serviceTask id="file"><bpmn:extensionElements><aimn:toolCall action="archive"/></bpmn:extensionElements></bpmn:serviceTask>
    <bpmn:endEvent id="e"/>
    <bpmn:sequenceFlow id="a" sourceRef="chat" targetRef="file"/>
    <bpmn:sequenceFlow id="b" sourceRef="messenger" targetRef="file"/>
    <bpmn:sequenceFlow id="c" sourceRef="weekly" targetRef="file"/>
    <bpmn:sequenceFlow id="d" name="done" sourceRef="file" targetRef="e"/>
  </bpmn:process>
</bpmn:definitions>
"""


def test_the_payload_picks_the_entry():
    d = parse_bpmn(TWO.replace("{ch}", "messenger"), "t")
    assert [s.id for s in d.start_steps()] == ["chat", "messenger", "weekly"]
    assert d.start_for({"channel": "messenger"}).id == "messenger"
    assert d.start_for({"channel": "chat", "source": "manual"}).id == "chat"
    assert d.start_for({"source": "schedule"}).id == "weekly"
    assert d.start_for({"channel": "fax"}).id == "weekly"  # no such channel: the first without one
    assert d.sources() == {"manual", "schedule"}
    assert d.start_with_source("schedule").id == "weekly"


def test_two_entries_on_one_channel_or_a_bad_channel_are_load_errors():
    with pytest.raises((BpmnError, DefinitionError), match="share a channel"):
        parse_bpmn(TWO.replace("{ch}", "chat"), "t")
    with pytest.raises(BpmnError, match="no id"):
        parse_bpmn(TWO.replace("{ch}", "Mess enger"), "t")


def test_the_graph_shows_every_entry_with_its_channel():
    graph = read_graph(TWO.replace("{ch}", "messenger"))
    starts = {n["id"]: n for n in graph["nodes"] if n["kind"] == "start"}
    assert starts["chat"]["channel"] == "chat" and starts["messenger"]["channel"] == "messenger"
    assert starts["weekly"]["source"] == "schedule" and "channel" not in starts["weekly"]
