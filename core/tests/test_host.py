"""Shared host: many instances in one process, each on its own directory.

What must hold when tenants share a process: data and secrets stay apart,
a session counts only where a role was granted, the operator's
environment is not the tenants', and a slug cannot become another path.
"""

from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core import auth, providers
from aitmation_core import secrets as core_secrets
from aitmation_core.host import Host, HostError, needs_clock
from aitmation_core.model_registry import ModelEntry

SECRET = "ab" * 32
KEY = "operator-key"
OPERATOR = {"Authorization": f"Bearer {KEY}"}
APPS = Path(__file__).resolve().parents[2] / "apps"


@pytest.fixture()
def host(tmp_path):
    h = Host(
        tmp_path / "root",
        auth_secret=SECRET,
        host_key=KEY,
        login_url="https://platform.example/account",
        solutions_dir=APPS,
        base_solutions=("tasks",),
        env={},
        model_factory=lambda settings: None,
        mail_factory=lambda settings, config=None: None,
    )
    try:
        yield h
    finally:
        h.close()
        core_secrets.ignore_environment(False)


def _token(roles: dict[str, str], sub: str = "anna@example.com", ttl: int = 60) -> str:
    principal = auth.Principal(sub, "member", tuple(roles), int(time.time()) + ttl, roles)
    return auth.sign(bytes.fromhex(SECRET), principal)


def _bearer(roles: dict[str, str], **kw) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(roles, **kw)}"}


def test_two_tenants_share_a_process_and_nothing_else(host):
    with TestClient(host) as client:
        for slug in ("alpha", "beta"):
            r = client.put(f"/host/tenants/{slug}", headers=OPERATOR, json={"title": slug.title()})
            assert r.status_code == 200 and r.json()["solutions"] == ["tasks"]
        a, b = _bearer({"alpha": "admin"}), _bearer({"beta": "admin"})
        assert client.get("/m/alpha/api/v1/instance", headers=a).json()["id"] == "alpha"
        assert client.get("/m/beta/api/v1/instance", headers=b).json()["id"] == "beta"
        assert host.loaded() == ["alpha", "beta"]

        # a secret set in one instance lies in that instance's file and nowhere else
        r = client.put("/m/alpha/api/v1/secrets/crm_token", headers=a, json={"value": "s3cret"})
        assert r.status_code == 200
        assert "s3cret" in (host.directory("alpha") / "data" / "secrets.toml").read_text()
        assert not (host.directory("beta") / "data" / "secrets.toml").exists()
        assert "crm_token" not in client.get("/m/beta/api/v1/secrets", headers=b).json()

        # each has its own database, and its booked solution in its own apps
        for slug in ("alpha", "beta"):
            data = host.directory(slug) / "data"
            assert (data / "aitmation.db").is_file()
            assert (data / "apps" / "tasks" / "solution.toml").is_file()


def test_a_session_counts_only_where_a_role_was_granted(host):
    with TestClient(host) as client:
        for slug in ("alpha", "beta"):
            client.put(f"/host/tenants/{slug}", headers=OPERATOR, json={})
        only_alpha = _bearer({"alpha": "admin"})
        assert client.get("/m/alpha/api/v1/gates", headers=only_alpha).status_code == 200
        assert client.get("/m/beta/api/v1/gates", headers=only_alpha).status_code == 403
        # admin in one, member in the other: the role is the instance's
        mixed = _bearer({"alpha": "admin", "beta": "member"})
        put = {"value": "x"}
        assert (
            client.put("/m/alpha/api/v1/secrets/a_token", headers=mixed, json=put).status_code
            == 200
        )
        assert (
            client.put("/m/beta/api/v1/secrets/a_token", headers=mixed, json=put).status_code == 403
        )
        # a household token (one role everywhere, wildcard) opens nothing here
        everywhere = auth.sign(
            bytes.fromhex(SECRET), auth.Principal("root", "admin", ("*",), int(time.time()) + 60)
        )
        r = client.get("/m/alpha/api/v1/gates", headers={"Authorization": f"Bearer {everywhere}"})
        assert r.status_code == 403
        assert client.get("/m/alpha/api/v1/gates").status_code == 401
        # an unknown role is no role
        assert (
            client.get("/m/alpha/api/v1/gates", headers=_bearer({"alpha": "owner"})).status_code
            == 403
        )


def test_sign_on_and_the_organisation_as_the_shell_sees_it(host):
    with TestClient(host, base_url="https://host.example") as client:
        for slug, title in (("alpha", "Alpha AG"), ("beta", "Beta GmbH"), ("gamma", "Gamma")):
            client.put(f"/host/tenants/{slug}", headers=OPERATOR, json={"title": title})
        nobody = client.get("/api/v1/household").json()
        assert nobody["members"] == [] and nobody["principal"] is None
        assert nobody["auth_required"] and nobody["login_url"] == "https://platform.example/account"

        link = _token({"alpha": "admin", "beta": "member"})
        r = client.get("/sso", params={"token": link}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app/"
        assert auth.COOKIE_NAME in r.cookies
        seen = client.get("/api/v1/household").json()
        assert [m["id"] for m in seen["members"]] == ["alpha", "beta"]
        assert seen["members"][0] == {
            "id": "alpha",
            "title": "Alpha AG",
            "type": "company",
            "url": "/m/alpha",
        }
        assert seen["principal"]["roles"] == {"alpha": "admin", "beta": "member"}
        # the cookie opens exactly those two
        assert client.get("/m/alpha/api/v1/instance").status_code == 200
        assert client.get("/m/beta/api/v1/instance").status_code == 200
        assert client.get("/m/gamma/api/v1/instance").status_code == 403
        # a long-lived token is not a sign-on link; one without roles opens nothing
        assert (
            client.get(
                "/sso",
                params={"token": _token({"alpha": "admin"}, ttl=3600)},
                follow_redirects=False,
            ).status_code
            == 401
        )
        assert (
            client.get("/sso", params={"token": _token({})}, follow_redirects=False).status_code
            == 403
        )


def test_the_operators_environment_is_not_the_tenants(host, monkeypatch):
    monkeypatch.setenv("AITMATION_SECRET_CRM_TOKEN", "from-the-operator")
    monkeypatch.setenv("AITMATION_ANTHROPIC_API_KEY", "sk-operator")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-test")
    monkeypatch.setenv("LLM_API_KEY", "sk-lent")
    with TestClient(host) as client:
        client.put("/host/tenants/alpha", headers=OPERATOR, json={})
        a = _bearer({"alpha": "admin"})
        status = client.get("/m/alpha/api/v1/secrets", headers=a).json()
        assert status["anthropic_api_key"] == "none" and "crm_token" not in status
        settings = host.load("alpha").runtime.settings
        assert core_secrets.resolve_secret(settings.data_dir, "crm_token") is None
        # the one thing lent: the model the operator declared, at its address
        monkeypatch.delenv("AITMATION_ANTHROPIC_API_KEY")
        lent = host.load("alpha").runtime.registry["llm"]
        assert providers._key_for(lent, settings) == "sk-lent"
        # an entry of the tenant's own - same provider, another address - gets no key
        own = ModelEntry(
            id="mine",
            model="claude-test",
            clearance="internal",
            role="cheap",
            provider="anthropic",
            base_url="https://elsewhere.example",
        )
        assert providers._key_for(own, settings) is None
        # ... nor does a copy of the lent entry pointed somewhere else
        from dataclasses import replace

        assert (
            providers._key_for(replace(lent, base_url="https://elsewhere.example"), settings)
            is None
        )


def test_what_sharing_forbids_is_forced(host):
    with TestClient(host) as client:
        client.put("/host/tenants/alpha", headers=OPERATOR, json={"budget_chf": 20})
        client.put("/host/tenants/beta", headers=OPERATOR, json={})
        a = _bearer({"alpha": "admin"})
        settings = host.load("alpha").runtime.settings
        assert settings.confine_paths and not settings.mcp_allow_stdio
        assert not settings.tools_allow_private_hosts
        assert settings.monthly_budget_chf == 20 and settings.budget_stop
        assert settings.embedding_cache_dir == host.models_dir
        # the instance's own file cannot switch them back on
        (settings.data_dir / "config.toml").write_text(
            "mcp_allow_stdio = true\ntools_allow_private_hosts = true\nmonthly_budget_chf = 9999\n"
        )
        host.unload("alpha")
        again = host.load("alpha").runtime.settings
        assert not again.mcp_allow_stdio and not again.tools_allow_private_hosts
        assert again.monthly_budget_chf == 20
        # a delivery folder may not point into a neighbour
        other = str(host.directory("beta") / "data" / "inbox")
        r = client.put(
            "/m/alpha/api/v1/addressees/neighbour",
            headers=a,
            json={"title": "x", "path": other, "channel": "folder"},
        )
        assert r.status_code == 422
        inside = str(settings.data_dir / "out")
        r = client.put(
            "/m/alpha/api/v1/addressees/own",
            headers=a,
            json={"title": "x", "path": inside, "channel": "folder"},
        )
        assert r.status_code == 200


@pytest.mark.parametrize("slug", ["..", "a/b", "A", "-a", "a_b", "x" * 41, "%2e%2e"])
def test_a_slug_cannot_become_another_path(host, slug):
    for bad in (slug, f"alpha/../{slug}", "alpha/../beta"):
        with pytest.raises(HostError):
            host.directory(bad)
    with TestClient(host) as client:
        assert client.get(f"/m/{slug}/api/v1/instance").status_code in (401, 404)
        assert client.put(f"/host/tenants/{slug}", headers=OPERATOR, json={}).status_code in (
            404,
            405,
        )
    assert sorted(p.name for p in host.tenants_dir.iterdir()) == []


def test_the_operators_side_needs_the_key_and_keeps_data(host):
    with TestClient(host) as client:
        assert client.get("/host/tenants").status_code == 401
        assert (
            client.get("/host/tenants", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        # a tenant's session is not the operator's key
        assert client.get("/host/tenants", headers=_bearer({"alpha": "admin"})).status_code == 401
        client.put(
            "/host/tenants/alpha", headers=OPERATOR, json={"title": "Alpha", "type": "private"}
        )
        assert (
            client.put(
                "/host/tenants/alpha", headers=OPERATOR, json={"type": "mailroom"}
            ).status_code
            == 422
        )
        assert (
            client.put("/host/tenants/alpha", headers=OPERATOR, json={"colour": 1}).status_code
            == 422
        )
        listed = client.get("/host/tenants", headers=OPERATOR).json()
        assert [(t["slug"], t["type"], t["in_memory"]) for t in listed] == [
            ("alpha", "private", False)
        ]
        a = _bearer({"alpha": "admin"})
        client.put("/m/alpha/api/v1/secrets/a_token", headers=a, json={"value": "kept"})
        # switched off: nobody gets in, nothing is lost
        client.put("/host/tenants/alpha", headers=OPERATOR, json={"enabled": False})
        assert client.get("/m/alpha/api/v1/instance", headers=a).status_code == 404
        client.put("/host/tenants/alpha", headers=OPERATOR, json={"enabled": True})
        # the export is the instance's directory
        r = client.get("/host/tenants/alpha/export", headers=OPERATOR)
        with tarfile.open(fileobj=io.BytesIO(r.content)) as archive:
            names = archive.getnames()
        assert "data/aitmation.db" in names and "data/secrets.toml" in names
        # retiring moves the directory aside
        kept = client.delete("/host/tenants/alpha", headers=OPERATOR).json()["kept_as"]
        assert (host.removed_dir / kept / "data" / "secrets.toml").is_file()
        assert client.get("/m/alpha/api/v1/instance", headers=a).status_code == 404
        assert client.get("/host/tenants", headers=OPERATOR).json() == []


def test_solutions_come_from_the_library_with_their_dependencies(host, tmp_path):
    library = tmp_path / "library"
    for name, deps in (("base", ""), ("upper", 'base = "*"')):
        (library / name).mkdir(parents=True)
        (library / name / "solution.toml").write_text(
            f'id = "{name}"\ntitle = "{name}"\nversion = "1"\n[dependencies]\n{deps}\n'
        )
    host.solutions_dir, host.base_solutions = library, ()
    assert [s["id"] for s in host.library()] == ["base", "upper"]
    tenant = host.save("alpha", {"solutions": ["upper"]})
    assert tenant.solutions == ("base", "upper")
    with pytest.raises(HostError):
        host.save("alpha", {"solutions": ["nowhere"]})
    settings = host.settings_for(tenant)
    settings.ensure_layout()
    (settings.apps_dir / "own").mkdir()  # what the tenant built itself
    host._mirror_solutions(tenant, settings)
    assert sorted(p.name for p in settings.apps_dir.iterdir() if p.is_dir()) == [
        "base",
        "own",
        "upper",
    ]
    # no longer booked: the solution goes, the tenant's own project stays
    tenant = host.save("alpha", {"solutions": ["base"]})
    host._mirror_solutions(tenant, settings)
    assert sorted(p.name for p in settings.apps_dir.iterdir() if p.is_dir()) == ["base", "own"]


def test_quiet_instances_sleep_and_wake_on_demand(host):
    host.idle_seconds = 0
    with TestClient(host) as client:
        client.put("/host/tenants/alpha", headers=OPERATOR, json={})
        a = _bearer({"alpha": "admin"})
        assert client.get("/m/alpha/api/v1/instance", headers=a).status_code == 200
        assert host.loaded() == ["alpha"]
        assert host.load("alpha").runtime.quiescent()
        assert host.sleep_idle() == 1 and host.loaded() == []
        assert not needs_clock(host.directory("alpha") / "data")
        assert host.wake_due() == 0
        # the next request starts it again, with its data
        assert client.get("/m/alpha/api/v1/instance", headers=a).status_code == 200
        # something for the clock keeps it awake - and wakes it after a restart
        runtime = host.load("alpha").runtime
        with runtime.lock:
            runtime.conn.execute(
                "INSERT INTO cases"
                " (id, template_id, template_version, status, created_at, updated_at)"
                " VALUES ('c1', 't', 1, 'running', '2026-01-01', '2026-01-01')"
            )
            runtime.conn.commit()
        assert not runtime.quiescent() and host.sleep_idle() == 0
        host.unload("alpha")
        assert needs_clock(host.directory("alpha") / "data")
        assert host.wake_due() == 1 and host.loaded() == ["alpha"]


def test_the_host_refuses_to_start_without_its_secrets(tmp_path):
    with pytest.raises(HostError):
        Host(tmp_path, auth_secret="", host_key=KEY, env={})
    with pytest.raises(HostError):
        Host(tmp_path, auth_secret=SECRET, host_key="", env={})
    core_secrets.ignore_environment(False)


def test_one_clock_ticks_every_instance_in_memory(host):
    host.poll_seconds = 0.02
    with TestClient(host) as client:
        seen: dict[str, int] = {}
        for slug in ("alpha", "beta"):
            client.put(f"/host/tenants/{slug}", headers=OPERATOR, json={})
            runtime = host.load(slug).runtime

            def tick(slug=slug, **kwargs):
                seen[slug] = seen.get(slug, 0) + 1
                if slug == "alpha":
                    raise RuntimeError("one tenant's bad tick")  # must not stop the others
                return 0

            runtime.tick = tick
        deadline = time.time() + 5
        while time.time() < deadline and min(seen.get("alpha", 0), seen.get("beta", 0)) < 3:
            time.sleep(0.02)
        assert seen.get("alpha", 0) >= 3 and seen.get("beta", 0) >= 3
