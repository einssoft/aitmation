"""Notifications (Stufe 1.2): new gate, reminder, escalation; SMTP settings
through the API; a failed send stays visible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core import notify
from aitmation_core.config import Settings, load_settings
from aitmation_core.runtime import Runtime
from aitmation_core.secrets import load_file_secrets
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"


class FakeSender:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, settings, password, to, subject, body):
        if self.fail:
            raise notify.NotifyError("smtp down")
        self.sent.append((to, subject, body))


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.delenv("AITMATION_SMTP_PASSWORD", raising=False)
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        instance_title="Firma X",
        notify_to="alex@example.ch, buchhaltung@example.ch",
        escalate_to="chef@example.ch",
        remind_after_hours=24,
        escalate_after_hours=72,
        smtp_host="smtp.example.ch",
        smtp_from="aitmation@example.ch",
        public_url="https://haushalt.example.ch",
    )
    runtime = Runtime(settings)
    sender = FakeSender()
    runtime.mail_sender = sender
    runtime.notifier = runtime._build_notifier()
    yield runtime, sender
    runtime.close()


def _open_gate(runtime: Runtime) -> dict:
    (runtime.settings.inbox_dir / "brief.txt").write_text("Sehr geehrte Damen", encoding="utf-8")
    runtime.tick()
    [gate] = runtime.open_gates()
    return gate


def test_new_gate_is_announced_once_to_every_recipient(rig):
    runtime, sender = rig
    gate = _open_gate(runtime)
    # The tick that opened the gate already queued and sent the "new" mails.
    assert sorted(to for to, _s, _b in sender.sent) == ["alex@example.ch", "buchhaltung@example.ch"]
    _to, subject, body = sender.sent[0]
    assert subject.startswith("Neue Pendenz:")
    assert "Firma X" in body and "https://haushalt.example.ch/app/" in body
    assert "Sehr geehrte" not in body  # never document content
    runtime.tick()
    runtime.tick()
    assert len(sender.sent) == 2  # no repeat
    rows = notify.recent(runtime.conn)
    assert {r["kind"] for r in rows} == {"new"} and all(r["status"] == "sent" for r in rows)
    assert notify.state_of(runtime.conn, gate["case_id"], gate["step_id"]).keys() == {"new"}


def test_reminder_and_escalation_follow_the_thresholds(rig):
    runtime, sender = rig
    gate = _open_gate(runtime)
    sender.sent.clear()
    started = datetime.fromisoformat(gate["started_at"])
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)

    def sla_of(_template_id):
        return {}

    gates = [{**gate, "title": "Kategorisieren"}]
    # 23 hours later: nothing new.
    assert runtime.notifier.observe(gates, sla_of, now=started + timedelta(hours=23)) == 0
    # 25 hours: a reminder to the recipients.
    assert runtime.notifier.observe(gates, sla_of, now=started + timedelta(hours=25)) == 2
    runtime.notifier.deliver()
    assert all("Erinnerung" in s for _t, s, _b in sender.sent)
    sender.sent.clear()
    # 73 hours: escalation to the deputy only, once.
    assert runtime.notifier.observe(gates, sla_of, now=started + timedelta(hours=73)) == 1
    runtime.notifier.deliver()
    assert sender.sent[0][0] == "chef@example.ch" and "Eskalation" in sender.sent[0][1]
    assert runtime.notifier.observe(gates, sla_of, now=started + timedelta(hours=99)) == 0
    state = notify.state_of(runtime.conn, gate["case_id"], gate["step_id"])
    assert set(state) == {"new", "reminder", "escalation"}


def test_template_sla_can_only_tighten(rig):
    runtime, _sender = rig
    gate = _open_gate(runtime)
    started = datetime.fromisoformat(gate["started_at"]).replace(tzinfo=UTC)
    gates = [{**gate, "title": "x"}]
    # A template asking for a 2-hour reminder gets it; one asking for 200
    # hours is held to the instance's 24.
    assert (
        runtime.notifier.observe(
            gates, lambda _t: {"remind_after_hours": 2}, now=started + timedelta(hours=3)
        )
        == 2
    )
    runtime.conn.execute("DELETE FROM notifications WHERE kind = 'reminder'")
    runtime.conn.commit()
    assert (
        runtime.notifier.observe(
            gates, lambda _t: {"remind_after_hours": 200}, now=started + timedelta(hours=30)
        )
        == 2
    )


def test_gate_deadline_escalates_immediately(rig):
    runtime, sender = rig
    gate = _open_gate(runtime)
    sender.sent.clear()
    past = (datetime.now(UTC) - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    runtime.conn.execute(
        "UPDATE steps SET deadline_at = ? WHERE case_id = ? AND step_id = ?",
        (past, gate["case_id"], gate["step_id"]),
    )
    runtime.conn.commit()
    runtime.tick()
    assert [to for to, _s, _b in sender.sent] == ["chef@example.ch"]


def test_failed_sends_are_retried_then_left_visible(rig):
    runtime, sender = rig
    sender.fail = True
    _open_gate(runtime)
    for _ in range(4):
        runtime.tick()
    rows = notify.recent(runtime.conn)
    assert rows and all(r["status"] == "failed" and r["attempts"] == 3 for r in rows)
    assert "smtp down" in rows[0]["error"]


def test_without_mail_server_notifications_are_recorded_as_unsent(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        notify_to="alex@example.ch",
    )
    runtime = Runtime(settings)
    assert runtime.notifier.active is False
    _open_gate(runtime)
    rows = notify.recent(runtime.conn)
    assert rows and all(r["status"] == "unsent" for r in rows)
    runtime.close()


def test_notification_settings_through_the_api(tmp_path, monkeypatch):
    monkeypatch.delenv("AITMATION_SMTP_PASSWORD", raising=False)
    settings = Settings(data_dir=tmp_path / "data", templates_dir=CORE_TEMPLATES, apps_dir=APPS)
    runtime = Runtime(settings)
    sender = FakeSender()
    runtime.mail_sender = sender
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        info = client.get("/api/v1/channels").json()["notifications"]
        assert info["active"] is False and info["password"] == "none"
        resp = client.put(
            "/api/v1/channels/notifications",
            json={
                "notify_to": "alex@example.ch",
                "escalate_to": "",
                "remind_after_hours": 8,
                "escalate_after_hours": 48,
                "smtp_host": "smtp.example.ch",
                "smtp_port": 465,
                "smtp_user": "aitmation",
                "smtp_from": "aitmation@example.ch",
                "smtp_security": "ssl",
                "public_url": "https://h.example.ch/",
                "password": "geheim",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["active"] is True and body["password"] == "file"
        assert body["public_url"] == "https://h.example.ch" and "geheim" not in resp.text
        assert load_file_secrets(settings.data_dir)["smtp_password"] == "geheim"
        config_text = (settings.data_dir / "config.toml").read_text(encoding="utf-8")
        assert "smtp.example.ch" in config_text and "geheim" not in config_text
        reloaded = load_settings(env={"AITMATION_DATA_DIR": str(settings.data_dir)})
        assert reloaded.smtp_port == 465 and reloaded.remind_after_hours == 8.0
        # Test mail goes through the (fake) sender; the notifier was rebuilt.
        assert client.post(
            "/api/v1/channels/notifications/test", json={"to": "alex@example.ch"}
        ).json() == {"ok": True}
        assert sender.sent[-1][0] == "alex@example.ch"
        bad = client.put(
            "/api/v1/channels/notifications",
            json={"smtp_security": "carrier-pigeon"},
        )
        assert bad.status_code == 422
        # Gates carry their notification state; the list endpoint exists.
        _open_gate(runtime)
        [gate] = client.get("/api/v1/gates").json()
        assert "new" in gate["notified"]
        assert client.get("/api/v1/notifications").json()[0]["kind"] == "new"
