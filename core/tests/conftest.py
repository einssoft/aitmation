"""Shared fixtures: a migrated in-memory database and a dummy definition.

The dummy process is deliberately domain-free (steps named after their
primitive, payloads are arbitrary dicts): the core must never depend on
what a document means.
"""

import pytest

from aitmation_core import db
from aitmation_core.definition import END, ProcessDefinition, StepDefinition, StepKind
from aitmation_core.engine import Engine


@pytest.fixture()
def conn():
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield conn
    conn.close()


def dummy_definition() -> ProcessDefinition:
    """intake -> prepare (agent) -> approve (gate) -> export (action) -> done.

    The gate's rejection leads to a timer that loops back to the agent
    step, which exercises cycles, timers and the gate invariant at once.
    """
    return ProcessDefinition(
        id="dummy",
        version=1,
        steps={
            "intake": StepDefinition(id="intake", kind=StepKind.TRIGGER, next={"fired": "prepare"}),
            "prepare": StepDefinition(
                id="prepare", kind=StepKind.AGENT_STEP, next={"done": "approve"}
            ),
            "approve": StepDefinition(
                id="approve",
                kind=StepKind.HUMAN_GATE,
                next={"approved": "export", "rejected": "cool_off"},
            ),
            "cool_off": StepDefinition(
                id="cool_off",
                kind=StepKind.TIMER,
                next={"elapsed": "prepare"},
                config={"seconds": 0},
            ),
            "export": StepDefinition(id="export", kind=StepKind.SYSTEM_ACTION, next={"done": END}),
        },
    )


@pytest.fixture()
def engine(conn):
    return Engine(conn, {"dummy": dummy_definition()})


class RoleModel:
    """A fake for role-bound briefings (Format 2): answers every task with
    a small structured result. The intake classifier is untouched - the
    runtime's own ``model_client`` stays None, so documents still sort
    out to a person - but ``document-read`` and friends get a model, as
    they must (a role is a requirement, not a hint)."""

    clearance = "sensitive"
    local = True
    entries = ()

    def __init__(self):
        self.calls: list[str] = []

    def complete_structured(self, task, payload, schema_hint=None):
        from aitmation_core.model import ModelResponse

        self.calls.append(task)
        return ModelResponse(
            data={"summary": "read by the fake", "anomalies": [], "confidence": 0.9},
            confidence=0.9, model_ref="fake-house@1", tokens_in=1, tokens_out=1,
        )


@pytest.fixture()
def house_model(tmp_path, monkeypatch):
    """A local registry entry plus a fake client for it, so role-bound
    briefings run while the instance has no router model."""
    from aitmation_core import providers

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "models.toml").write_text(
        '[house]\nmodel = "llama-house"\nprovider = "ollama"\nclearance = "sensitive"\n'
        'role = "cheap"\nregion = "local"\nlocal = true\n',
        encoding="utf-8",
    )
    model = RoleModel()
    monkeypatch.setattr(providers, "client_for", lambda entry, settings: model)
    return model


def needs_solution(*names: str):
    """Skip marker for tests that drive the engine through a solution that
    is not part of this repository. They run wherever ``apps/`` holds it."""
    from pathlib import Path

    import pytest

    apps = Path(__file__).resolve().parents[2] / "apps"
    missing = [n for n in names if not (apps / n).is_dir()]
    return pytest.mark.skipif(bool(missing), reason="needs solution(s): " + ", ".join(missing))
