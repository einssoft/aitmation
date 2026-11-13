"""Shop v0 (ADR-0043 §4): a signed catalog on a plain host, bundles
installed after digest, validator and trust check; revocations shown;
export of an installed solution as a bundle."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aitmation_core import catalog, trust
from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "docs" / "examples"


@pytest.fixture()
def shop(tmp_path):
    """A platform key, two signed bundles (demo-triage verified, a copy as
    community) and a catalog served by a mock host."""
    pem, public = trust.generate_keypair()
    keys = {"aitmation-test": public}
    src = tmp_path / "src"
    shutil.copytree(EXAMPLES / "demo-triage", src / "demo-triage")
    trust.sign_solution(src / "demo-triage", pem, "aitmation-test", "verified")
    bundle = catalog.pack_solution(src / "demo-triage", tmp_path / "shop")

    community = tmp_path / "src" / "demo-community"
    shutil.copytree(EXAMPLES / "demo-triage", community)
    manifest = community / "solution.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('id = "demo-triage"', 'id = "demo-community"'),
        encoding="utf-8",
    )
    trust.sign_solution(community, pem, "aitmation-test", "community")
    bundle2 = catalog.pack_solution(community, tmp_path / "shop")

    out = catalog.build_catalog(
        [bundle, bundle2], tmp_path / "shop", pem, "aitmation-test",
        base_url="https://shop.example.test/",
        revoked=[{"id": "old-thing", "version": "0.0.1", "reason": "superseded"}],
    )
    files = {p.name: p.read_bytes() for p in (tmp_path / "shop").iterdir()}

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if request.url.host != "shop.example.test" or name not in files:
            return httpx.Response(404)
        return httpx.Response(200, content=files[name])

    return {
        "pem": pem, "keys": keys, "catalog_file": out, "bundle": bundle,
        "transport": httpx.MockTransport(handler), "files": files,
    }


def _runtime(tmp_path, shop, **overrides) -> Runtime:
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=REPO / "core" / "templates",
        apps_dir=tmp_path / "data" / "apps", trust_keys=shop["keys"],
        catalog_url="https://shop.example.test/catalog.json", **overrides,
    )
    runtime = Runtime(settings)
    runtime._http_transport = shop["transport"]
    return runtime


def test_catalog_is_only_used_with_a_valid_platform_signature(shop):
    text = shop["catalog_file"].read_text(encoding="utf-8")
    parsed = catalog.parse_catalog(text, shop["keys"])
    assert [e.id for e in parsed.entries] == ["demo-triage", "demo-community"]
    assert parsed.entries[0].trust_level == "verified" and parsed.revoked[0]["id"] == "old-thing"
    with pytest.raises(catalog.CatalogError, match="unknown key"):
        catalog.parse_catalog(text, {})
    tampered = json.loads(text)
    tampered["entries"][1]["trust_level"] = "official"
    with pytest.raises(catalog.CatalogError, match="does not verify"):
        catalog.parse_catalog(json.dumps(tampered), shop["keys"])
    # Unsigned bundles never make it into a catalog.
    plain = catalog.pack_solution(EXAMPLES / "demo-triage", shop["catalog_file"].parent / "plain")
    with pytest.raises(catalog.CatalogError, match="not signed"):
        catalog.build_catalog([plain], shop["catalog_file"].parent / "plain", shop["pem"], "k")


def test_install_from_catalog_checks_digest_trust_and_validator(tmp_path, shop):
    runtime = _runtime(tmp_path, shop)
    client = TestClient(create_app(runtime))
    try:
        view = client.get("/api/v1/catalog").json()
        assert view["error"] is None and view["signed_by"] == "aitmation-test"
        by_id = {e["id"]: e for e in view["entries"]}
        assert by_id["demo-triage"]["state"] == "available" and by_id["demo-triage"]["installable"]
        assert not by_id["demo-community"]["installable"]  # community is off by default

        done = client.post(
            "/api/v1/catalog/install", json={"id": "demo-triage", "version": "0.1.0"}
        )
        assert done.status_code == 200, done.text
        assert done.json()["trust"] == {
            "level": "verified", "verified": True, "reason": "platform signature verified",
            "signed_by": "aitmation-test",
        }
        assert "demo-triage" in runtime.apps and runtime.app_trust["demo-triage"].verified
        assert (tmp_path / "data" / "apps" / "demo-triage" / "solution.toml").is_file()
        assert client.get("/api/v1/catalog").json()["entries"][0]["state"] == "installed"

        refused = client.post(
            "/api/v1/catalog/install", json={"id": "demo-community", "version": "0.1.0"}
        )
        assert refused.status_code == 422 and "community" in refused.text

        # A bundle whose bytes differ from the catalog's digest is refused.
        shop["files"]["demo-triage-0.1.0.tgz"] += b"\\n"
        again = client.post(
            "/api/v1/catalog/install", json={"id": "demo-triage", "version": "0.1.0"}
        )
        assert again.status_code == 502 and "digest" in again.text
    finally:
        runtime.close()


def test_install_from_file_replaces_and_retires_the_old_version(tmp_path, shop):
    runtime = _runtime(
        tmp_path, shop, trusted_levels=("local", "official", "verified", "community")
    )
    client = TestClient(create_app(runtime))
    try:
        raw = shop["files"]["demo-community-0.1.0.tgz"]
        first = client.post(
            "/api/v1/catalog/install-file",
            json={"name": "x.tgz", "content_base64": base64.b64encode(raw).decode()},
        )
        assert first.status_code == 200, first.text
        assert first.json()["replaced_version"] is None
        second = client.post(
            "/api/v1/catalog/install-file",
            json={"name": "x.tgz", "content_base64": base64.b64encode(raw).decode()},
        )
        assert second.status_code == 200 and second.json()["replaced_version"] == "0.1.0"
        retired = list((tmp_path / "data" / "retired").glob("demo-community-0.1.0-*"))
        assert len(retired) == 1 and (retired[0] / "solution.toml").is_file()
        assert runtime.app_trust["demo-community"].level == "community"

        # Garbage and unsafe archives never touch the apps directory.
        bad = client.post(
            "/api/v1/catalog/install-file",
            json={"name": "x.tgz", "content_base64": base64.b64encode(b"not a tar").decode()},
        )
        assert bad.status_code == 422
    finally:
        runtime.close()


def test_unsafe_bundle_paths_are_refused(tmp_path):
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape.toml")
        payload = b"x = 1\\n"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(catalog.CatalogError, match="unsafe path"):
        catalog.install_bundle(buffer.getvalue(), tmp_path / "apps")
    assert not (tmp_path / "escape.toml").exists()


def test_export_packs_an_installed_solution(tmp_path, shop):
    runtime = _runtime(tmp_path, shop)
    client = TestClient(create_app(runtime))
    try:
        client.post("/api/v1/catalog/install", json={"id": "demo-triage", "version": "0.1.0"})
        exported = client.get("/api/v1/solutions/demo-triage/export")
        assert exported.status_code == 200
        assert exported.headers["content-disposition"].endswith('demo-triage-0.1.0.tgz"')
        # Round trip: the exported bundle is the same solution and installs again.
        digest_before = hashlib.sha256(shop["files"]["demo-triage-0.1.0.tgz"]).hexdigest()
        result = catalog.install_bundle(
            exported.content, tmp_path / "elsewhere", keys=shop["keys"]
        )
        assert result.id == "demo-triage" and result.trust.verified
        assert digest_before  # the catalog digest is over the platform's own packing
    finally:
        runtime.close()
