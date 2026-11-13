"""The solution catalog - shop v0 without a backend (ADR-0043 §4, Etappe A).

The platform publishes a signed ``catalog.json`` next to the bundles
(``<id>-<version>.tgz``) on a plain https host. An instance fetches the
catalog, verifies the platform's signature, shows the entries with their
trust level, and installs a bundle after checking its digest, extracting
it safely, running the validator and verifying the bundle's own
signature against ``trusted_levels``. Revocations travel in the catalog
too. No account, no payment, no code - declarations only (Zielbild §6).

Platform side: ``pack_solution`` and ``build_catalog`` (CLI
``build-catalog``). Instance side: ``fetch_catalog`` and ``install_bundle``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import trust
from .connectors import fetch_bytes

CATALOG_FORMAT = 1
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_CATALOG_BYTES = 2 * 1024 * 1024
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache"}


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    version: str
    name: dict[str, str]
    description: dict[str, str]
    publisher: dict[str, str]
    trust_level: str
    file: str
    sha256: str
    size: int
    requires: dict = field(default_factory=dict)
    model_roles: list[str] = field(default_factory=list)
    dependencies: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Catalog:
    generated_at: str
    base_url: str
    entries: list[CatalogEntry]
    revoked: list[dict]
    signed_by: str


# -- platform side ---------------------------------------------------------


def pack_solution(path: str | Path, out_dir: str | Path) -> Path:
    """``<id>-<version>.tgz`` of a solution directory (no caches, no VCS),
    paths relative to the bundle root."""
    root = Path(path)
    data = tomllib.loads((root / "solution.toml").read_text(encoding="utf-8"))
    out = Path(out_dir) / f"{data['id']}-{data['version']}.tgz"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        for file in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = file.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel.parts):
                continue
            tar.add(file, arcname=rel.as_posix(), recursive=False)
    return out


def _canonical(data: dict) -> bytes:
    body = {k: v for k, v in data.items() if k not in ("signed_by", "signature")}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def build_catalog(
    bundles: list[str | Path],
    out_dir: str | Path,
    private_pem: str,
    key_id: str,
    *,
    base_url: str = "",
    revoked: list[dict] | None = None,
) -> Path:
    """Write a signed ``catalog.json`` for the given bundles. Every bundle
    must carry a platform signature already (``sign-solution``) - the
    catalog lists trust levels, it does not grant them."""
    entries = []
    for bundle in bundles:
        bundle = Path(bundle)
        raw = bundle.read_bytes()
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            member = tar.getmember("solution.toml")
            manifest = tomllib.loads(tar.extractfile(member).read().decode("utf-8"))  # type: ignore[union-attr]
        if manifest.get("trust_level") not in trust.LEVELS or "signature" not in manifest:
            raise CatalogError(f"{bundle.name}: bundle is not signed (sign-solution first)")
        entries.append(
            {
                "id": manifest["id"],
                "version": manifest["version"],
                "name": _texts(manifest.get("name")),
                "description": _texts(manifest.get("description")),
                "publisher": dict(manifest.get("publisher") or {}),
                "trust_level": manifest["trust_level"],
                "requires": dict(manifest.get("requires") or {}),
                "model_roles": sorted((manifest.get("model_roles") or {}).keys()),
                "dependencies": dict(manifest.get("dependencies") or {}),
                "file": bundle.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        )
    data = {
        "format": CATALOG_FORMAT,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "base_url": base_url,
        "entries": entries,
        "revoked": list(revoked or []),
    }
    private = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise CatalogError("the signing key must be an Ed25519 private key")
    data["signed_by"] = key_id
    data["signature"] = base64.b64encode(private.sign(_canonical(data))).decode("ascii")
    out = Path(out_dir) / "catalog.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return out


def _texts(value) -> dict[str, str]:
    if isinstance(value, str):
        return {"de": value}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


# -- instance side ---------------------------------------------------------


def parse_catalog(text: str | bytes, keys: dict[str, str] | None = None) -> Catalog:
    """A catalog is only ever used with a valid platform signature."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CatalogError(f"catalog is not JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != CATALOG_FORMAT:
        raise CatalogError("unsupported catalog format")
    keys = dict(trust.PLATFORM_KEYS) | dict(keys or {})
    key_id = str(data.get("signed_by", ""))
    public_b64 = keys.get(key_id)
    if not public_b64:
        raise CatalogError(f"catalog signed by unknown key {key_id!r}")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(
            base64.b64decode(str(data.get("signature", ""))), _canonical(data)
        )
    except (InvalidSignature, ValueError) as exc:
        raise CatalogError("catalog signature does not verify") from exc
    entries = []
    for raw in data.get("entries") or []:
        try:
            entries.append(
                CatalogEntry(
                    id=str(raw["id"]), version=str(raw["version"]),
                    name=_texts(raw.get("name")), description=_texts(raw.get("description")),
                    publisher={str(k): str(v) for k, v in (raw.get("publisher") or {}).items()},
                    trust_level=str(raw.get("trust_level", "community")),
                    file=str(raw["file"]), sha256=str(raw["sha256"]), size=int(raw.get("size", 0)),
                    requires=dict(raw.get("requires") or {}),
                    model_roles=[str(r) for r in raw.get("model_roles") or []],
                    dependencies={
                        str(k): str(v) for k, v in (raw.get("dependencies") or {}).items()
                    },
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"catalog entry unreadable: {exc}") from exc
    return Catalog(
        generated_at=str(data.get("generated_at", "")),
        base_url=str(data.get("base_url", "")),
        entries=entries,
        revoked=[r for r in data.get("revoked") or [] if isinstance(r, dict)],
        signed_by=key_id,
    )


def fetch_catalog(
    url: str,
    keys: dict[str, str] | None = None,
    *,
    transport=None,
    timeout: int = 20,
    allow_private: bool = False,
) -> Catalog:
    """The catalog behind ``url``, verified. ``allow_private`` is the
    instance's ``tools_allow_private_hosts`` - a catalog on a local host
    is a development setup, never the default."""
    try:
        raw = fetch_bytes(
            url, MAX_CATALOG_BYTES, transport=transport, timeout_seconds=timeout,
            allow_private=allow_private,
        )
    except ValueError as exc:
        raise CatalogError(str(exc)) from exc
    catalog = parse_catalog(raw, keys)
    if not catalog.base_url:
        catalog = Catalog(
            catalog.generated_at, url.rsplit("/", 1)[0] + "/", catalog.entries,
            catalog.revoked, catalog.signed_by,
        )
    return catalog


def fetch_bundle(
    catalog: Catalog,
    entry: CatalogEntry,
    *,
    transport=None,
    timeout: int = 60,
    allow_private: bool = False,
) -> bytes:
    """The bundle behind an entry, checked against the catalog's digest."""
    if "/" in entry.file or entry.file.startswith("."):
        raise CatalogError(f"bundle name {entry.file!r} is not a plain file name")
    try:
        raw = fetch_bytes(
            catalog.base_url + entry.file, MAX_BUNDLE_BYTES, transport=transport,
            timeout_seconds=timeout, allow_private=allow_private,
        )
    except ValueError as exc:
        raise CatalogError(str(exc)) from exc
    if hashlib.sha256(raw).hexdigest() != entry.sha256:
        raise CatalogError(f"{entry.file}: digest differs from the catalog")
    return raw


@dataclass(frozen=True)
class InstallResult:
    id: str
    version: str
    trust: trust.TrustResult
    replaced_version: str | None
    path: Path


def _safe_extract(raw: bytes, target: Path) -> None:
    """Extract a bundle: regular files under relative paths only - no
    absolute paths, no ``..``, no links, no devices."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")  # noqa: SIM115 - closed below
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise CatalogError(f"not a bundle (.tgz): {exc}") from exc
    with tar:
        for member in tar.getmembers():
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts or not member.name:
                raise CatalogError(f"bundle contains an unsafe path {member.name!r}")
            if member.isdir():
                (target / name).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise CatalogError(f"bundle contains a non-file entry {member.name!r}")
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise CatalogError(f"bundle entry {member.name!r} unreadable")
            dest.write_bytes(source.read())


def install_bundle(
    raw: bytes,
    apps_dir: str | Path,
    *,
    keys: dict[str, str] | None = None,
    trusted_levels: tuple[str, ...] = trust.DEFAULT_TRUSTED,
    core_definitions: dict | None = None,
    retired_dir: str | Path | None = None,
    expected_id: str | None = None,
) -> InstallResult:
    """Validate, verify and place a bundle under ``apps_dir/<id>``. An
    existing version is moved to ``retired_dir`` (never deleted). Nothing
    is placed unless validator and trust check pass; the caller reloads
    the runtime afterwards and restores on failure."""
    from .apps import validate_app

    apps_dir = Path(apps_dir)
    work = Path(tempfile.mkdtemp(prefix="aitmation-install-"))
    try:
        staged = work / "bundle"
        staged.mkdir()
        _safe_extract(raw, staged)
        manifest_file = staged / "solution.toml"
        if not manifest_file.is_file():
            raise CatalogError("bundle has no solution.toml")
        manifest = tomllib.loads(manifest_file.read_text(encoding="utf-8"))
        solution_id, version = str(manifest.get("id", "")), str(manifest.get("version", ""))
        if expected_id and solution_id != expected_id:
            raise CatalogError(f"bundle is {solution_id!r}, expected {expected_id!r}")
        # The validator sees the bundle next to the solutions it depends on.
        # Dependencies must already be installed.
        provided = {}
        if apps_dir.is_dir():
            from .apps import load_apps

            try:
                provided = load_apps(apps_dir, core_definitions=core_definitions)
            except Exception:  # a broken neighbour must not block the install
                provided = {}
        report = validate_app(staged, core_definitions=core_definitions, provided=provided)
        if not report.ok:
            raise CatalogError(
                "bundle fails validation:\n" + "\n".join(f"  {f}" for f in report.errors)
            )
        verdict = trust.verify_solution(staged, manifest, keys)
        if verdict.level not in trusted_levels:
            raise CatalogError(
                f"trust level {verdict.level!r} is not installable here"
                f" (trusted_levels = {list(trusted_levels)}): {verdict.reason}"
            )
        target = apps_dir / solution_id
        replaced = None
        if target.exists():
            try:
                old = tomllib.loads((target / "solution.toml").read_text(encoding="utf-8"))
                replaced = str(old.get("version", "?"))
            except (OSError, ValueError):
                replaced = "?"
            retired = Path(retired_dir) if retired_dir else apps_dir.parent / "retired"
            retired.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            shutil.move(str(target), str(retired / f"{solution_id}-{replaced}-{stamp}"))
        apps_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(target))
        return InstallResult(solution_id, version, verdict, replaced, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
