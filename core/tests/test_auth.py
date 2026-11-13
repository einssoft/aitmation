"""Household login (ADR-0025): the core verifies launcher tokens."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core import auth
from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"
SECRET = "ab" * 32  # hex, as the launcher writes it


def _token(sub: str, role: str = "member", instances=("privat",), ttl: int = 60) -> str:
    return auth.sign(
        bytes.fromhex(SECRET),
        auth.Principal(sub=sub, role=role, instances=tuple(instances), exp=int(time.time()) + ttl),
    )


def test_verify_round_trip_and_failures():
    secret = bytes.fromhex(SECRET)
    token = _token("alex", "admin", ("*",))
    principal = auth.verify(secret, token)
    assert principal.sub == "alex" and principal.is_admin
    assert principal.may_access("firma-x", "company")
    member = auth.verify(secret, _token("anna"))
    assert member.may_access("privat", "private")
    assert not member.may_access("firma-x", "company")
    assert member.may_access("mailroom", "mailroom")  # the shared inbox
    with pytest.raises(auth.AuthError, match="signature"):
        auth.verify(b"other" * 8, token)
    with pytest.raises(auth.AuthError, match="signature"):
        auth.verify(secret, token[:-3] + "abc")
    with pytest.raises(auth.AuthError, match="expired"):
        auth.verify(secret, _token("alex", ttl=-5))
    with pytest.raises(auth.AuthError, match="malformed"):
        auth.verify(secret, "nonsense")


@pytest.fixture()
def instance(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        instance_id="firma-x",
        instance_title="Firma X",
        instance_type="company",
    )
    runtime = Runtime(settings)
    app = create_app(runtime, model_factory=lambda s: None, auth_secret=SECRET)
    with TestClient(app) as client:
        yield client, runtime


def test_protected_instance(instance):
    client, runtime = instance
    # Public: liveness, the household map (with the flag), the shell.
    assert client.get("/healthz").status_code == 200
    hh = client.get("/api/v1/household").json()
    assert hh["auth_required"] is True
    # Everything else: 401 without a token, as JSON (the shell shows the login).
    resp = client.get("/api/v1/instance")
    assert resp.status_code == 401 and resp.json()["login"] == "launcher"
    assert client.get("/client-tasks").status_code == 401
    assert client.get("/").status_code in (200, 307)  # root only redirects to the shell
    # A member of another mandant is refused; an admin gets in.
    denied = client.get(
        "/api/v1/instance", headers={"Authorization": f"Bearer {_token('anna')}"}
    )
    assert denied.status_code == 403
    ok = client.get(
        "/api/v1/instance", headers={"Authorization": f"Bearer {_token('alex', 'admin', ('*',))}"}
    )
    assert ok.status_code == 200
    # The cookie works too, and the principal becomes the audited actor.
    client.cookies.set(auth.COOKIE_NAME, _token("anna", instances=("firma-x",)))
    (runtime.settings.inbox_dir / "brief.txt").write_text("Sehr geehrte Damen", encoding="utf-8")
    runtime.tick()
    [gate] = client.get("/api/v1/gates").json()
    resp = client.post(
        f"/api/v1/cases/{gate['case_id']}/steps/{gate['step_id']}/decide",
        json={"category": "letter", "actor": "spoofed"},
    )
    assert resp.status_code == 200
    detail = client.get(f"/api/v1/cases/{gate['case_id']}").json()
    actors = {a["actor_id"] for a in detail["audit"] if a["actor_type"] == "human"}
    assert actors == {"anna"}
    # An upload carries the principal as sender, whatever the body claims.
    up = client.post("/api/v1/upload", json={"from": "spoofed", "text": "Sehr geehrte Damen"})
    assert up.status_code == 200
    runtime.tick()
    [gate] = client.get("/api/v1/gates").json()
    assert gate["document"]["from"] == "anna" and gate["document"]["source"] == "upload"
