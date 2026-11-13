"""Setup/admin UI: secrets store, connection tests, template reload."""

import os
import sys

import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings, load_settings
from aitmation_core.model import ModelError, ModelResponse
from aitmation_core.runtime import Runtime
from aitmation_core.secrets import load_file_secrets, resolve_secret, save_secret
from aitmation_core.template import parse_template
from aitmation_core.web import create_app

TEMPLATE = """
id = "demo"
version = 1

[steps.intake]
kind = "trigger"
config = { source = "folder" }
next = { fired = "approve" }

[steps.approve]
kind = "human_gate"
next = { approved = "end", rejected = "end" }
"""


# -- secrets store ---------------------------------------------------------


def test_save_and_resolve_secret(tmp_path):
    save_secret(tmp_path, "anthropic_api_key", 'sk-with"quote')
    assert resolve_secret(tmp_path, "anthropic_api_key", env={}) == 'sk-with"quote'
    # Environment wins over the file.
    assert (
        resolve_secret(
            tmp_path, "anthropic_api_key", env={"AITMATION_ANTHROPIC_API_KEY": "from-env"}
        )
        == "from-env"
    )
    # Empty value removes the secret.
    save_secret(tmp_path, "anthropic_api_key", "")
    assert resolve_secret(tmp_path, "anthropic_api_key", env={}) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_secrets_file_is_owner_only(tmp_path):
    save_secret(tmp_path, "imap_password", "geheim")
    mode = os.stat(tmp_path / "secrets.toml").st_mode & 0o777
    assert mode == 0o600


def test_invalid_secret_names_are_rejected(tmp_path):
    """Known secrets and tool secrets (ADR-0041, lowercase identifiers) are
    fine; anything else - paths, uppercase, spaces - is refused."""
    for bad in ("Root Password", "../x", "A", "x" * 70):
        with pytest.raises(ValueError, match="invalid secret name"):
            save_secret(tmp_path, bad, "x")
    save_secret(tmp_path, "scheduler_token", "x")  # a tool secret is allowed


# -- web fixtures ----------------------------------------------------------


class FlakyModel:
    def __init__(self, fail=False):
        self.fail = fail

    def complete_structured(self, task, payload, schema_hint=None):
        if self.fail:
            raise ModelError("nope")
        return ModelResponse(data={"ok": True, "confidence": 1.0}, model_ref="t@1", confidence=1.0)


class CheckedMail:
    def __init__(self, fail=False):
        self.fail = fail

    def check(self):
        if self.fail:
            raise RuntimeError("login refused")

    def fetch_new(self, attachment_dir):
        return []


@pytest.fixture()
def rig(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=tmp_path / "templates")
    settings.templates_dir.mkdir(parents=True)
    (settings.templates_dir / "demo.toml").write_text(TEMPLATE, encoding="utf-8")
    state = {"model": None, "mail": None}
    runtime = Runtime(settings, definitions={"demo": parse_template(TEMPLATE)})
    app = create_app(
        runtime,
        model_factory=lambda s: state["model"],
        mail_factory=lambda s, cfg=None: state["mail"],
    )
    with TestClient(app) as client:
        yield client, runtime, settings, state
    # runtime closed by lifespan


# -- setup flow ------------------------------------------------------------


def test_root_redirects_to_the_shell_or_explains(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=tmp_path / "none")
    runtime = Runtime(settings, definitions={})
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        resp = client.get("/", follow_redirects=False)
        # Without a built shell the root explains itself instead of 404ing.
        assert resp.status_code in (200, 307)
        if resp.status_code == 200:
            assert resp.json()["api"] == "/api/v1"


def test_api_key_goes_to_secrets_file_not_config_or_response(rig):
    client, _runtime, settings, _state = rig
    resp = client.put("/api/v1/secrets/anthropic_api_key", json={"value": "sk-test-123"})
    assert resp.status_code == 200
    assert "sk-test-123" not in resp.text  # never echoed
    assert load_file_secrets(settings.data_dir)["anthropic_api_key"] == "sk-test-123"
    config = settings.data_dir / "config.toml"
    assert not config.exists() or "sk-test-123" not in config.read_text(encoding="utf-8")
    assert client.get("/api/v1/secrets").json()["anthropic_api_key"] == "file"


def test_mail_settings_persist_non_secrets_to_config(rig):
    client, runtime, settings, _state = rig
    resp = client.put(
        "/api/v1/channels/mail",
        json={"imap_host": "imap.example.ch", "imap_user": "x@firma.ch",
              "imap_folder": "Rechnungen", "password": "pw123"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["password"] == "file" and resp.json()["imap_host"] == "imap.example.ch"
    config_text = (settings.data_dir / "config.toml").read_text(encoding="utf-8")
    assert "imap.example.ch" in config_text and "Rechnungen" in config_text
    assert "pw123" not in config_text  # secret stays out of config.toml
    assert load_file_secrets(settings.data_dir)["imap_password"] == "pw123"
    assert runtime.mail_config["imap_folder"] == "Rechnungen"
    # Reloaded settings pick the values up (round-trip through load_settings).
    reloaded = load_settings(env={"AITMATION_DATA_DIR": str(settings.data_dir)})
    assert reloaded.imap_host == "imap.example.ch"
    # Overview never carries the password, only where it comes from.
    overview = client.get("/api/v1/channels").json()
    assert overview["mail"]["password"] == "file" and "pw123" not in resp.text
    assert overview["web"]["token"] == "none"


def test_mail_connection_test_reports_result(rig):
    client, _runtime, _settings, state = rig
    assert client.post("/api/v1/channels/mail/test").json()["reason"] == "incomplete"
    state["mail"] = CheckedMail()
    assert client.post("/api/v1/channels/mail/test").json() == {"ok": True}
    state["mail"] = CheckedMail(fail=True)
    result = client.post("/api/v1/channels/mail/test").json()
    assert result["ok"] is False and "login refused" in result["detail"]


def test_web_form_token_is_issued_once_and_then_works(rig):
    client, _runtime, settings, _state = rig
    assert client.post("/api/v1/intake", json={"text": "x"}).status_code == 404
    issued = client.post("/api/v1/channels/web/token").json()
    token = issued["token"]
    assert len(token) > 20 and issued["header"] == "X-Intake-Token"
    assert client.get("/api/v1/channels").json()["web"]["token"] == "file"
    assert token not in client.get("/api/v1/channels").text
    resp = client.post(
        "/api/v1/intake", json={"text": "Hallo"}, headers={"X-Intake-Token": token}
    )
    assert resp.status_code == 200, resp.text
    assert client.delete("/api/v1/channels/web/token").json() == {"ok": True}
    assert client.post("/api/v1/intake", json={"text": "x"}).status_code == 404
    assert load_file_secrets(settings.data_dir).get("intake_token") is None


# -- reload ----------------------------------------------------------------


def test_admin_reload_picks_up_new_template(rig):
    client, runtime, settings, _state = rig
    (settings.templates_dir / "second.toml").write_text(
        TEMPLATE.replace('id = "demo"', 'id = "second"'), encoding="utf-8"
    )
    resp = client.post("/admin/reload")
    assert resp.status_code == 200 and resp.json()["templates"] == ["demo", "second"]
    assert set(runtime.engine.definitions) == {"demo", "second"}


def test_admin_reload_keeps_old_set_on_broken_template(rig):
    client, runtime, settings, _state = rig
    (settings.templates_dir / "broken.toml").write_text("id = ", encoding="utf-8")
    resp = client.post("/admin/reload")
    assert resp.status_code == 422
    assert set(runtime.engine.definitions) == {"demo"}  # old set stays active
