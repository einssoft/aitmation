"""App bundles (ADR-0023): validator, loader, runtime merge, enable/disable."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core import i18n
from aitmation_core.apps import AppError, load_app, load_apps, validate_app
from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.template import load_templates
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
REPO_TEMPLATES = REPO / "core" / "templates"
REPO_APPS = REPO / "apps"

MANIFEST = """
format = 1
id = "demo"
version = "1.0.0"
name = { de = "Demo", fr = "Démo", it = "Demo", en = "Demo" }
description = "Ein Demo-App."
[requires]
core = "0.0.1"
data_class = "internal"
[settings.approver]
type = "principal"
label = { de = "Wer gibt frei?", fr = "Qui valide ?", it = "Chi approva?", en = "Who approves?" }
required = true
binds = ["demo-flow:approver"]
"""

TEMPLATE = """
id = "demo-flow"
version = 1
[meta]
title = { de = "Demo-Ablauf", fr = "Flux démo", it = "Flusso demo", en = "Demo flow" }
[parameters]
approver = { default = "owner", description = "wer freigibt" }
[policy.privacy]
category = "internal"
[steps.intake]
kind = "trigger"
config = { source = "chain" }
next = { fired = "prepare" }
[steps.prepare]
kind = "agent_step"
config = { task = "prepare for {param:approver}" }
next = { done = "approve", failed = "end" }
[steps.approve]
kind = "human_gate"
next = { approved = "export", rejected = "end" }
[steps.export]
kind = "system_action"
config = { action = "export" }
next = { done = "end" }
"""

DOCTYPE = """
id = "demo-doc"
title = { de = "Demo-Dokument", fr = "Document démo", it = "Documento demo", en = "Demo document" }
chain = "demo-flow"
"""

TEST_CASE = """
id = "demo-doc-recognized"
[given]
text = "irgendein Text"
[expect]
doctype = "demo-doc"
"""


def _bundle(root: Path, name="demo", manifest=MANIFEST, template=TEMPLATE, doctype=DOCTYPE):
    app = root / name
    (app / "templates").mkdir(parents=True)
    (app / "doctypes").mkdir()
    (app / "tests").mkdir()
    (app / "app.toml").write_text(manifest, encoding="utf-8")
    (app / "templates" / "demo-flow.toml").write_text(template, encoding="utf-8")
    (app / "doctypes" / "demo-doc.toml").write_text(doctype, encoding="utf-8")
    (app / "tests" / "case.toml").write_text(TEST_CASE, encoding="utf-8")
    return app


# -- i18n ---------------------------------------------------------------------


def test_i18n_normalize_and_pick():
    assert i18n.normalize("Hallo") == {"de": "Hallo"}
    table = i18n.normalize({"de": "Hallo", "en": "Hello"})
    assert i18n.pick(table, "en") == "Hello"
    assert i18n.pick(table, "it") == "Hallo"  # fallback to the lead language
    assert i18n.missing_languages(table) == ("fr", "it")
    with pytest.raises(i18n.TextError, match="lead language"):
        i18n.normalize({"en": "Hello"})
    with pytest.raises(i18n.TextError, match="unknown language"):
        i18n.normalize({"de": "x", "xx": "y"})


# -- validator ----------------------------------------------------------------


def test_shipped_bundle_is_valid_and_fully_translated():
    report = validate_app(
        REPO_APPS / "document-basics", core_definitions=load_templates(REPO_TEMPLATES)
    )
    assert report.ok, [str(f) for f in report.errors]
    assert report.warnings == []
    assert set(report.app.templates) == {
        "document-read", "document-review", "document-archive", "follow-up",
    }
    assert set(report.app.doctypes) == {"invoice", "bank-statement", "letter", "email"}
    assert [c.id for c in report.app.tests] == ["invoice-is-recognized", "letter-is-recognized"]
    assert report.app.doctypes["invoice"].title_in("fr") == "Facture"
    assert "document_type" in report.app.manifest.settings


def test_valid_bundle_loads(tmp_path):
    app = load_app(_bundle(tmp_path))
    assert app.id == "demo" and app.version == "1.0.0"
    assert app.manifest.settings["approver"].binds == ("demo-flow:approver",)
    assert app.tests[0].expect == {"doctype": "demo-doc"}


def test_executable_files_are_rejected(tmp_path):
    app = _bundle(tmp_path)
    (app / "hook.py").write_text("print('no')", encoding="utf-8")
    (app / "templates" / "x.sh").write_text("#!/bin/sh", encoding="utf-8")
    report = validate_app(app)
    messages = [f.message for f in report.errors]
    assert any("file not allowed at bundle root" in m for m in messages)
    assert any("only *.toml" in m for m in messages)
    assert report.app is None


def test_unsupported_format_and_bad_version(tmp_path):
    app = _bundle(tmp_path, manifest=MANIFEST.replace("format = 1", "format = 7"))
    report = validate_app(app)
    assert any("unsupported format" in f.message for f in report.errors)
    app2 = _bundle(
        tmp_path, name="demo2", manifest=MANIFEST.replace('version = "1.0.0"', 'version = "1"')
    )
    report2 = validate_app(app2)
    assert any("semantic" in f.message for f in report2.errors)


def test_core_version_requirement(tmp_path):
    app = _bundle(tmp_path, manifest=MANIFEST.replace('core = "0.0.1"', 'core = "99.0.0"'))
    report = validate_app(app)
    assert any("needs core >= 99.0.0" in f.message for f in report.errors)


def test_step_may_not_exceed_declared_data_class(tmp_path):
    hot = TEMPLATE.replace(
        'config = { task = "prepare for {param:approver}" }',
        'config = { task = "prepare", data_class = "sensitive" }',
    )
    report = validate_app(_bundle(tmp_path, template=hot))
    assert any("requires.data_class" in f.message for f in report.errors)


def test_bind_must_point_to_a_declared_parameter(tmp_path):
    bad = MANIFEST.replace('binds = ["demo-flow:approver"]', 'binds = ["demo-flow:nobody", "x"]')
    report = validate_app(_bundle(tmp_path, manifest=bad))
    messages = [f.message for f in report.errors]
    assert any("declares no parameter 'nobody'" in m for m in messages)
    assert any("must look like" in m for m in messages)


def test_doctype_chain_must_resolve_and_no_core_shadowing(tmp_path):
    lonely = DOCTYPE.replace('chain = "demo-flow"', 'chain = "ghost"')
    report = validate_app(_bundle(tmp_path, doctype=lonely))
    assert any("neither in this bundle nor in the core" in f.message for f in report.errors)
    shadow = TEMPLATE.replace('id = "demo-flow"', 'id = "document-intake"')
    report2 = validate_app(
        _bundle(tmp_path, name="shadow", template=shadow),
        core_definitions=load_templates(REPO_TEMPLATES),
    )
    assert any("would shadow a core template" in f.message for f in report2.errors)


def test_connectors_are_not_supported_in_format_1(tmp_path):
    m = MANIFEST.replace(
        'data_class = "internal"', 'data_class = "internal"\nconnectors = ["bank"]'
    )
    report = validate_app(_bundle(tmp_path, manifest=m))
    assert any("connectors are not supported" in f.message for f in report.errors)


def test_missing_languages_are_warnings_not_errors(tmp_path):
    mono = MANIFEST.replace(
        'name = { de = "Demo", fr = "Démo", it = "Demo", en = "Demo" }', 'name = "Demo"'
    )
    report = validate_app(_bundle(tmp_path, manifest=mono))
    assert report.ok
    assert {f.message for f in report.warnings} >= {"name has no fr text", "name has no en text"}


def test_acceptance_case_references_are_checked(tmp_path):
    app = _bundle(tmp_path)
    (app / "tests" / "bad.toml").write_text(
        'id = "bad"\n[given]\ntext = "x"\n[expect]\ndoctype = "nope"\n', encoding="utf-8"
    )
    (app / "tests" / "bad2.toml").write_text(
        'id = "bad2"\n[given]\nfile = "samples/missing.pdf"\n[expect]\n'
        'template = "demo-flow"\noutcome = "exploded"\n',
        encoding="utf-8",
    )
    messages = [f.message for f in validate_app(app).errors]
    assert any("not declared in this bundle" in m for m in messages)
    assert any("existing samples/ file" in m for m in messages)
    assert any("no outcome of 'demo-flow'" in m for m in messages)


def test_load_apps_rejects_cross_bundle_collisions(tmp_path):
    _bundle(tmp_path, name="one")
    _bundle(tmp_path, name="two", manifest=MANIFEST.replace('id = "demo"', 'id = "two"'))
    with pytest.raises(AppError, match="also provided by app"):
        load_apps(tmp_path)


# -- runtime ------------------------------------------------------------------


def test_runtime_merges_bundle_templates_and_records_installation(tmp_path):
    apps_dir = tmp_path / "apps"
    _bundle(apps_dir)
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=tmp_path / "none", apps_dir=apps_dir
    )
    runtime = Runtime(settings)
    try:
        assert "demo-flow" in runtime.engine.definitions
        assert runtime.template_app["demo-flow"] == "demo"
        assert "demo-doc" in runtime.doctypes
        [row] = runtime.installed_apps()
        assert row["id"] == "demo" and row["version"] == "1.0.0" and row["enabled"] == 1

        # Switching the app off unloads its templates but keeps the row.
        runtime.set_app_enabled("demo", False)
        assert "demo-flow" not in runtime.engine.definitions
        assert "demo-doc" not in runtime.doctypes
        assert runtime.installed_apps()[0]["enabled"] == 0
        runtime.set_app_enabled("demo", True)
        assert "demo-flow" in runtime.engine.definitions
    finally:
        runtime.close()


def test_broken_bundle_keeps_previous_state_on_reload(tmp_path):
    apps_dir = tmp_path / "apps"
    app = _bundle(apps_dir)
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=tmp_path / "none", apps_dir=apps_dir
    )
    runtime = Runtime(settings)
    try:
        (app / "evil.exe").write_bytes(b"MZ")
        with pytest.raises(AppError, match="not allowed"):
            runtime.reload_templates()
        assert "demo-flow" in runtime.engine.definitions  # old state survives
    finally:
        runtime.close()


def test_admin_lists_apps_and_toggles(tmp_path):
    apps_dir = tmp_path / "apps"
    _bundle(apps_dir)
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=tmp_path / "none", apps_dir=apps_dir
    )
    runtime = Runtime(settings)
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        [app] = client.get("/api/v1/apps").json()
        assert app["id"] == "demo" and app["enabled"] and app["loaded"]
        assert len(app["templates"]) == 1 and len(app["doctypes"]) == 1
        resp = client.post("/api/v1/apps/demo/enable", json={"enabled": False})
        assert resp.status_code == 200
        assert "demo-flow" not in runtime.engine.definitions
        [app] = client.get("/api/v1/apps").json()
        assert app["enabled"] is False
