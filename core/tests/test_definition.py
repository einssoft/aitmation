# ruff: noqa: E501
"""Definition validation, above all the static ADR-0007 gate invariant."""

import pytest

from aitmation_core.definition import (
    END,
    DefinitionError,
    ProcessDefinition,
    StepDefinition,
    StepKind,
)
from conftest import dummy_definition


def test_dummy_definition_is_valid():
    dummy_definition().validate()


def _definition(steps: dict[str, StepDefinition]) -> ProcessDefinition:
    return ProcessDefinition(id="p", version=1, steps=steps)


def test_system_action_without_gate_is_rejected():
    bad = _definition(
        {
            "start": StepDefinition(id="start", kind=StepKind.TRIGGER, next={"fired": "write"}),
            "write": StepDefinition(id="write", kind=StepKind.SYSTEM_ACTION, next={"done": END}),
        }
    )
    with pytest.raises(DefinitionError, match="ADR-0007"):
        bad.validate()


def test_system_action_via_rejected_gate_path_is_rejected():
    # The gate exists, but the rejection outcome also leads to the write -
    # a path without approval. Must be caught.
    bad = _definition(
        {
            "start": StepDefinition(id="start", kind=StepKind.TRIGGER, next={"fired": "gate"}),
            "gate": StepDefinition(
                id="gate",
                kind=StepKind.HUMAN_GATE,
                next={"approved": "write", "rejected": "write"},
            ),
            "write": StepDefinition(id="write", kind=StepKind.SYSTEM_ACTION, next={"done": END}),
        }
    )
    with pytest.raises(DefinitionError, match="ADR-0007"):
        bad.validate()


def test_entries_need_their_own_channel():
    # ADR-0057: several triggers are entries of one process - but two on
    # the same channel could not be told apart, and none is no process.
    two = _definition(
        {
            "a": StepDefinition(id="a", kind=StepKind.TRIGGER, next={"fired": END}, config={"channel": "x"}),
            "b": StepDefinition(id="b", kind=StepKind.TRIGGER, next={"fired": END}, config={"channel": "y"}),
        }
    )
    two.validate()
    two.steps["b"].config["channel"] = "x"
    with pytest.raises(DefinitionError, match="share a channel"):
        two.validate()
    none = _definition({"a": StepDefinition(id="a", kind=StepKind.SYSTEM_ACTION, next={"done": END}, config={"action": "archive"})})
    with pytest.raises(DefinitionError, match="at least one Trigger"):
        none.validate()


def test_unknown_transition_target_is_rejected():
    bad = _definition({"a": StepDefinition(id="a", kind=StepKind.TRIGGER, next={"fired": "ghost"})})
    with pytest.raises(DefinitionError, match="unknown step"):
        bad.validate()


def test_timer_without_duration_is_rejected():
    bad = _definition(
        {
            "a": StepDefinition(id="a", kind=StepKind.TRIGGER, next={"fired": "t"}),
            "t": StepDefinition(id="t", kind=StepKind.TIMER, next={"elapsed": END}),
        }
    )
    with pytest.raises(DefinitionError, match="seconds"):
        bad.validate()
