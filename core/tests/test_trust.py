"""ADR-0043: trust levels and platform signatures on solutions. A bundle
built here is ``local``; one from the shop carries a level only as far as
the platform's signature verifies it; what the instance loads is a setting."""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest

from aitmation_core import trust
from aitmation_core.config import Settings
from aitmation_core.runtime import Runtime

REPO = Path(__file__).resolve().parents[2]
SOLUTION = REPO / "docs" / "examples" / "demo-triage"


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "apps" / "demo-triage"
    shutil.copytree(SOLUTION, target)
    return target


def test_sign_and_verify_roundtrip_and_tamper_detection(tmp_path):
    root = _copy(tmp_path)
    pem, public = trust.generate_keypair()
    keys = {"aitmation-2026": public}

    before = trust.solution_digest(root)
    assert trust.solution_digest(root) == before  # stable
    digest = trust.sign_solution(root, pem, "aitmation-2026", "verified")
    manifest = tomllib.loads((root / "solution.toml").read_text(encoding="utf-8"))
    assert manifest["trust_level"] == "verified"
    assert manifest["signature"]["manifest_sha256"] == digest
    assert digest != before  # the level is part of what is signed …
    assert trust.solution_digest(root) == digest  # … the signature table is not

    ok = trust.verify_solution(root, manifest, keys)
    assert ok.level == "verified" and ok.verified and ok.signed_by == "aitmation-2026"

    # Unknown key: the declared level is worth nothing - community.
    unknown = trust.verify_solution(root, manifest, {})
    assert unknown.level == "community" and not unknown.verified

    # A changed file after signing: community, with the reason.
    (root / "constants" / "extra.toml").write_text("x = 1\n", encoding="utf-8")
    changed = trust.verify_solution(root, manifest, keys)
    assert changed.level == "community" and "changed" in changed.reason

    # Re-signing with another level replaces level and signature cleanly.
    trust.sign_solution(root, pem, "aitmation-2026", "official")
    text = (root / "solution.toml").read_text(encoding="utf-8")
    assert text.count("[signature]") == 1 and text.count("trust_level") == 1
    manifest = tomllib.loads(text)
    assert trust.verify_solution(root, manifest, keys).level == "official"

    # A forged level without re-signing fails the signature.
    forged = dict(manifest, trust_level="verified")
    assert not trust.verify_solution(root, forged, keys).verified

    with pytest.raises(ValueError):
        trust.sign_solution(root, pem, "k", "platinum")


def test_local_bundles_need_no_signature_and_validator_checks_shape(tmp_path):
    from aitmation_core.apps import validate_app

    root = _copy(tmp_path)
    manifest = tomllib.loads((root / "solution.toml").read_text(encoding="utf-8"))
    assert trust.verify_solution(root, manifest).level == "local"
    assert validate_app(root).ok
    with open(root / "solution.toml", "a", encoding="utf-8") as handle:
        handle.write('\ntrust_level = "platinum"\npublisher = { name = 1 }\n')
    report = validate_app(root)
    messages = " ".join(str(f) for f in report.errors)
    assert "trust_level" in messages and "publisher" in messages


def test_runtime_loads_only_trusted_levels(tmp_path):
    root = _copy(tmp_path)
    pem, public = trust.generate_keypair()
    trust.sign_solution(root, pem, "k1", "community")

    def runtime(**overrides) -> Runtime:
        settings = Settings(
            data_dir=tmp_path / "data", templates_dir=REPO / "core" / "templates",
            apps_dir=root.parent, **overrides,
        )
        return Runtime(settings)

    # Default: community is refused, the refusal is diagnosed, nothing else breaks.
    rt = runtime(trust_keys={"k1": public})
    try:
        assert "demo-triage" not in rt.apps
        assert rt.app_trust["demo-triage"].level == "community"
        refused = rt.conn.execute(
            "SELECT detail FROM diagnostic_events WHERE event = 'trust_refused'"
        ).fetchall()
        assert refused and "community" in refused[0]["detail"]
    finally:
        rt.close()

    # Opt in (the Skool premium case): loaded, verified by the platform key.
    everything = ("local", "official", "verified", "community")
    rt = runtime(trust_keys={"k1": public}, trusted_levels=everything)
    try:
        assert "demo-triage" in rt.apps
        assert rt.app_trust["demo-triage"].verified
    finally:
        rt.close()

    # Without the key the bundle is still community (unverified) - the
    # declared level never counts on its own.
    rt = runtime(trusted_levels=everything)
    try:
        verdict = rt.app_trust["demo-triage"]
        assert verdict.level == "community" and not verdict.verified
        warned = rt.conn.execute(
            "SELECT detail FROM diagnostic_events WHERE event = 'trust_unverified'"
        ).fetchall()
        assert warned and "unknown signing key" in warned[0]["detail"]
    finally:
        rt.close()
