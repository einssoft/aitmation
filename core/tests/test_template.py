"""Template loader: parsing, error reporting, the reserved 'end' keyword."""

from pathlib import Path

import pytest

from aitmation_core.definition import END, StepKind
from aitmation_core.template import TemplateError, load_template, load_templates, parse_template

REPO_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
APP_BUNDLE = Path(__file__).resolve().parents[2] / "apps" / "document-basics"

VALID = """
id = "t"
version = 1

[steps.start]
kind = "trigger"
next = { fired = "gate" }

[steps.gate]
kind = "human_gate"
next = { approved = "write", rejected = "end" }

[steps.write]
kind = "system_action"
next = { done = "end" }
"""


def test_shipped_templates_are_valid():
    from aitmation_core.apps import load_app

    definitions = load_templates(REPO_TEMPLATES)
    bundle = load_app(APP_BUNDLE).templates  # format 2: processes/*.bpmn (ADR-0035)
    # The intake is the single folder entry point; everything else chains.
    assert definitions["document-intake"].start_step().config["source"] == "folder"
    assert bundle["document-read"].start_step().config["source"] == "chain"


def test_parse_valid_template():
    definition = parse_template(VALID)
    assert definition.steps["gate"].kind is StepKind.HUMAN_GATE
    assert definition.steps["write"].next["done"] is END


def test_unknown_kind_is_rejected():
    with pytest.raises(TemplateError, match="unknown kind"):
        parse_template(VALID.replace('"human_gate"', '"robot_gate"'))


def test_unknown_step_key_is_rejected():
    with pytest.raises(TemplateError, match="unknown keys"):
        parse_template(VALID + "\n[steps.write.typo]\nx = 1\n")


def test_gate_invariant_applies_to_templates():
    # Remove the gate: trigger goes straight to the SystemAction.
    bad = VALID.replace('next = { fired = "gate" }', 'next = { fired = "write" }')
    with pytest.raises(TemplateError, match="ADR-0007"):
        parse_template(bad)


def test_step_named_end_is_rejected():
    with pytest.raises(TemplateError, match="reserved"):
        parse_template(
            'id = "t"\nversion = 1\n[steps.end]\nkind = "trigger"\nnext = { fired = "end" }\n'
        )


def test_load_template_reports_file_name(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text("id = ", encoding="utf-8")
    with pytest.raises(TemplateError, match=r"broken\.toml"):
        load_template(path)
