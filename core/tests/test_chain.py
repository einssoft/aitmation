"""Chaining (ADR-0014): process: targets, chain_id, gate reset, policies."""

import json
from pathlib import Path

import pytest

from aitmation_core.config import Settings
from aitmation_core.definition import (
    END,
    DefinitionError,
    ProcessDefinition,
    StepDefinition,
    StepKind,
)
from aitmation_core.engine import EngineError
from aitmation_core.model import ModelResponse
from aitmation_core.runtime import Runtime
from aitmation_core.template import TemplateError, load_templates, parse_template

REPO_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
REPO_APPS = Path(__file__).resolve().parents[2] / "apps"
APP_DOCTYPES = REPO_APPS / "document-basics" / "doctypes"


@pytest.fixture()
def chain_runtime(tmp_path, house_model):
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=REPO_TEMPLATES, apps_dir=REPO_APPS
    )
    runtime = Runtime(settings)
    yield runtime
    runtime.close()


def _cases(runtime):
    return list(
        runtime.conn.execute(
            "SELECT id, template_id, status, chain_id, gate_passed_at FROM cases"
            " ORDER BY created_at"
        )
    )


def test_full_chain_intake_read_review_archive(chain_runtime):
    runtime = chain_runtime
    (runtime.settings.inbox_dir / "brief.txt").write_text("ein Brief", encoding="utf-8")
    runtime.tick()  # ingest -> stub classifier -> sort-out gate

    [gate] = runtime.open_gates()
    assert (gate["template_id"], gate["step_id"]) == ("document-intake", "categorize")
    runtime.categorize(gate["case_id"], "categorize", "letter", actor_id="alex")
    runtime.tick()  # dispatch starts document-read
    runtime.tick()  # stub agent -> handoff to document-review

    cases = _cases(runtime)
    assert [c["template_id"] for c in cases] == [
        "document-intake",
        "document-read",
        "document-review",
    ]
    assert {c["status"] for c in cases[:2]} == {"completed"}
    assert len({c["chain_id"] for c in cases}) == 1
    # Approvals never cross chain boundaries: successors start ungated.
    assert cases[2]["gate_passed_at"] is None

    [gate] = runtime.open_gates()
    assert (gate["template_id"], gate["step_id"]) == ("document-review", "review")
    runtime.decide(gate["case_id"], "review", "approved", actor_id="alex")
    runtime.tick()  # archive action in document-archive

    cases = _cases(runtime)
    assert [c["template_id"] for c in cases] == [
        "document-intake",
        "document-read",
        "document-review",
        "document-archive",
    ]
    assert {c["status"] for c in cases} == {"completed"}
    assert len({c["chain_id"] for c in cases}) == 1

    # The archive holds the original file plus the case summary - the
    # original trigger stayed reachable across three hops.
    archive = runtime.settings.archive_dir / cases[3]["id"]
    names = {p.name for p in archive.iterdir()}
    assert "vorgang.json" in names
    assert any(n.endswith("_brief.txt") for n in names)


def test_rejection_ends_chain_without_archive(chain_runtime):
    runtime = chain_runtime
    (runtime.settings.inbox_dir / "x.txt").write_text("x", encoding="utf-8")
    runtime.tick()
    [gate] = runtime.open_gates()
    runtime.categorize(gate["case_id"], "categorize", "letter", actor_id="alex")
    runtime.tick()
    runtime.tick()
    [gate] = runtime.open_gates()
    runtime.decide(gate["case_id"], "review", "rejected", actor_id="alex")
    runtime.tick()
    assert len(_cases(runtime)) == 3  # no archivieren case started
    assert not any(runtime.settings.archive_dir.iterdir())


def test_shipped_chain_templates_load_and_reference_each_other():
    core = load_templates(REPO_TEMPLATES)
    assert set(core) == {"document-intake"}  # the intake is core mechanics
    definitions = dict(core)
    from aitmation_core.apps import load_app

    definitions.update(load_app(REPO_APPS / "document-basics", core_definitions=core).templates)
    assert set(definitions) == {
        "document-intake",
        "document-read",
        "document-review",
        "document-archive",
        "follow-up",
    }
    assert definitions["document-read"].process_targets() == {"document-review"}
    # Parameter substitution resolved the document type into the task.
    task = definitions["document-read"].steps["read"].config["task"]
    assert "generic" in task and "{param:" not in task


def test_unknown_successor_is_rejected(tmp_path):
    lonely = parse_template(
        """
id = "lonely"
version = 1
[steps.a]
kind = "trigger"
next = { fired = "process:ghost" }
"""
    )
    settings = Settings(data_dir=tmp_path / "d", templates_dir=tmp_path / "unused")
    with pytest.raises(EngineError, match="ghost"):
        Runtime(settings, definitions={"lonely": lonely})


def test_unknown_parameter_placeholder_is_rejected():
    with pytest.raises(TemplateError, match="unknown parameter"):
        parse_template(
            """
id = "p"
version = 1
[steps.a]
kind = "trigger"
next = { fired = "end" }
config = { task = "use {param:missing}" }
"""
        )


def test_internal_action_needs_no_gate_but_external_does():
    def definition(action_config):
        return ProcessDefinition(
            id="p",
            version=1,
            steps={
                "a": StepDefinition(id="a", kind=StepKind.TRIGGER, next={"fired": "w"}),
                "w": StepDefinition(
                    id="w", kind=StepKind.SYSTEM_ACTION, next={"done": END}, config=action_config
                ),
            },
        )

    definition({"action": "archive"}).validate()  # internal: allowed ungated
    with pytest.raises(DefinitionError, match="ADR-0007"):
        definition({"action": "bexio_booking"}).validate()  # external: gated
    with pytest.raises(DefinitionError, match="ADR-0007"):
        definition({}).validate()  # undeclared counts as external


PRIVACY_TEMPLATE = """
id = "sensitive"
version = 1
[policy.privacy]
category = "sensitive"
[steps.a]
kind = "trigger"
config = { source = "folder" }
next = { fired = "work" }
[steps.work]
kind = "agent_step"
config = { task = "analyze" }
next = { done = "end", failed = "end" }
"""


class RemoteModel:
    local = False

    def __init__(self, confidence=0.9):
        self.confidence = confidence
        self.calls = 0

    def complete_structured(self, task, payload, schema_hint=None):
        self.calls += 1
        return ModelResponse(
            data={"ok": True, "confidence": self.confidence},
            model_ref="remote@1",
            confidence=self.confidence,
        )


def test_privacy_category_blocks_remote_model(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", templates_dir=tmp_path / "unused")
    model = RemoteModel()
    runtime = Runtime(
        settings,
        definitions={"sensitive": parse_template(PRIVACY_TEMPLATE)},
        model_client=model,
    )
    try:
        (runtime.settings.inbox_dir / "f.txt").write_text("secret", encoding="utf-8")
        runtime.tick()
        assert model.calls == 0  # the data never reached the remote model
        row = runtime.conn.execute(
            "SELECT outcome, result FROM steps WHERE step_id = 'work'"
        ).fetchone()
        assert row["outcome"] == "failed"
        assert "sensitive" in json.loads(row["result"])["error"]
    finally:
        runtime.close()


LOW_CONFIDENCE_TEMPLATE = PRIVACY_TEMPLATE.replace(
    'category = "sensitive"', 'category = "internal"'
).replace("[policy.privacy]", "[policy.model]\nconfidence_threshold = 0.8\n[policy.privacy]")


def test_confidence_below_policy_threshold_fails_step(tmp_path):
    settings = Settings(data_dir=tmp_path / "d", templates_dir=tmp_path / "unused")
    runtime = Runtime(
        settings,
        definitions={"sensitive": parse_template(LOW_CONFIDENCE_TEMPLATE)},
        model_client=RemoteModel(confidence=0.4),
    )
    try:
        (runtime.settings.inbox_dir / "f.txt").write_text("x", encoding="utf-8")
        runtime.tick()
        row = runtime.conn.execute(
            "SELECT outcome, result FROM steps WHERE step_id = 'work'"
        ).fetchone()
        assert row["outcome"] == "failed"
        assert "below policy threshold" in json.loads(row["result"])["error"]
    finally:
        runtime.close()
