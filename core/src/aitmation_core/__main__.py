"""Entry point: ``python -m aitmation_core``.

Starts, reads its configuration, migrates the schema, opens a port. It
does not know whether the launcher, Docker or a human started it
(ADR-0002); uvicorn's signal handling provides the graceful SIGTERM
shutdown the deployment contract requires.

``python -m aitmation_core validate-app <path>`` runs the app bundle
validator (ADR-0023) and exits 1 on errors - the same check the runtime,
the workshop, the shop and a Colabbot contract use.

``python -m aitmation_core mcp`` serves the MCP channel over stdio for a
desktop assistant (ADR-0024); ``AITMATION_URL`` names the instance it
talks to.

Credentials resolve environment-first, then the secrets file written by
the setup UI (see secrets.py) - so containers keep using env injection
while local installations configure everything through the browser.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from .apps import validate_app
from .config import load_settings
from .runtime import Runtime
from .template import load_templates
from .web import create_app, default_mail_source, default_model_client

log = logging.getLogger("aitmation.main")


def validate_app_command(paths: list[str]) -> int:
    """Validate one or more bundles; print findings; 1 if any has errors."""
    if not paths:
        print("usage: python -m aitmation_core validate-app <bundle-dir> [...]", file=sys.stderr)
        return 2
    settings = load_settings()
    core = load_templates(settings.templates_dir) if settings.templates_dir.is_dir() else {}
    worst = 0
    for path in paths:
        # Declared dependencies next to the bundle are loaded first, as the
        # proving run does - so a solution that writes into another one's
        # collection validates the way it will be installed.
        from pathlib import Path as _Path

        from .testing import _load_provided, _sibling_solutions

        bundle = _Path(path)
        try:
            provided = _load_provided(bundle, _sibling_solutions(bundle), core)
        except Exception as exc:
            provided = {}
            print(f"{path}: dependencies not loaded ({exc})", file=sys.stderr)
        report = validate_app(path, core_definitions=core, provided=provided or None)
        status = "ok" if report.ok else "FAILED"
        print(
            f"{path}: {status} ({len(report.errors)} error(s),"
            f" {len(report.warnings)} warning(s))"
        )
        for finding in report.findings:
            print(f"  {finding}")
        if not report.ok:
            worst = 1
    return worst


def migrate_command(args: list[str]) -> int:
    """``migrate-solution <bundle> [--to <dir>]`` (ADR-0035 §11)."""
    from .migrate import MigrationError, migrate_solution

    if not args:
        print("usage: python -m aitmation_core migrate-solution <bundle-dir> [--to <dir>]",
              file=sys.stderr)
        return 2
    target = None
    if "--to" in args:
        index = args.index("--to")
        target = args[index + 1] if index + 1 < len(args) else None
        args = args[:index] + args[index + 2 :]
    try:
        out = migrate_solution(args[0], target)
    except MigrationError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    print(f"migrated {args[0]} -> {out} (format 2)")
    return 0


def test_command(args: list[str]) -> int:
    """``test <solution> [--config <name>] [--record] [--json] [--only <id>]``
    runs the solution's sidecar tests (ADR-0035 §8)."""
    from .testing import format_report, run_solution_tests

    if not args or args[0].startswith("--"):
        print(
            "usage: python -m aitmation_core test <solution-dir> [--config test]"
            " [--record] [--json] [--only <case-id>] [--repeat <k>]",
            file=sys.stderr,
        )
        return 2
    options = {"config": "test", "record": False, "json": False, "only": None, "repeat": None}
    rest = args[1:]
    while rest:
        flag = rest.pop(0)
        if flag == "--config" and rest:
            options["config"] = rest.pop(0)
        elif flag == "--only" and rest:
            options["only"] = rest.pop(0)
        elif flag == "--repeat" and rest:
            options["repeat"] = int(rest.pop(0))
        elif flag == "--record":
            options["record"] = True
        elif flag == "--json":
            options["json"] = True
        else:
            print(f"unknown option {flag!r}", file=sys.stderr)
            return 2
    logging.basicConfig(level=logging.ERROR)  # the report is the output, not the log
    settings = load_settings()
    core = load_templates(settings.templates_dir) if settings.templates_dir.is_dir() else {}
    inner = default_model_client(settings) if options["record"] else None
    report = run_solution_tests(
        args[0],
        configuration=str(options["config"]),
        record=bool(options["record"]),
        inner_client=inner,
        only=options["only"],
        repeat=options["repeat"],
        core_definitions=core,
        core_templates_dir=settings.templates_dir,
    )
    print(report.to_json() if options["json"] else format_report(report))
    return 0 if report.ok else 1


def sign_command(argv: list[str]) -> int:
    """``sign-solution <path> --key <pem file> --key-id <id> --level
    official|verified|community`` - the platform's step when it accepts a
    solution into the shop (ADR-0043). Writes trust_level and [signature]
    into solution.toml."""
    import argparse

    from . import trust

    parser = argparse.ArgumentParser(prog="aitmation_core sign-solution")
    parser.add_argument("path")
    parser.add_argument("--key", required=True, help="Ed25519 private key (PEM)")
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--level", required=True, choices=trust.LEVELS)
    args = parser.parse_args(argv)
    with open(args.key, encoding="ascii") as handle:
        pem = handle.read()
    digest = trust.sign_solution(args.path, pem, args.key_id, args.level)
    print(f"signed {args.path} as {args.level} by {args.key_id}: {digest}")
    return 0


def keygen_command(argv: list[str]) -> int:
    """``trust-keygen <prefix>`` writes ``<prefix>.pem`` (private, keep it
    on the platform) and prints the public key for PLATFORM_KEYS /
    trust_keys."""
    from . import trust

    if len(argv) != 1:
        print("usage: trust-keygen <prefix>", file=sys.stderr)
        return 2
    pem, public = trust.generate_keypair()
    with open(f"{argv[0]}.pem", "w", encoding="ascii") as handle:
        handle.write(pem)
    print(f"private key: {argv[0]}.pem")
    print(f"public key (base64): {public}")
    return 0


def pack_command(argv: list[str]) -> int:
    """``pack-solution <path> <out-dir>`` - the bundle file of a solution
    (validated first), ready for the catalog or for sharing."""
    from . import catalog

    if len(argv) != 2:
        print("usage: pack-solution <solution path> <out dir>", file=sys.stderr)
        return 2
    report = validate_app(argv[0])
    if not report.ok:
        for finding in report.errors:
            print(f"  {finding}", file=sys.stderr)
        return 1
    out = catalog.pack_solution(argv[0], argv[1])
    print(f"packed {out}")
    return 0


def import_workflow_command(argv: list[str]) -> int:
    """``import-workflow <export.json|.zip> [--from n8n|activepieces|make|zapier]
    [--id x] [--zap n] [--into <solution dir>]`` - translate another platform's workflow
    (ADR-0060). Without ``--into`` only the report is printed; with it the
    files are added to the solution (never overwriting) and validated."""
    import json as _json
    from pathlib import Path

    from . import workflow_import

    opts = {"--from": "", "--id": "", "--into": "", "--zap": "0"}
    rest: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg in opts:
            opts[arg] = next(it, "")
        else:
            rest.append(arg)
    if len(rest) != 1:
        print("usage: import-workflow <export.json|.zip> [--from n8n|activepieces|make|zapier]"
              " [--id x] [--zap n] [--into <solution dir>]", file=sys.stderr)
        return 2
    try:
        text = workflow_import.export_text(Path(rest[0]).read_bytes())
        result = workflow_import.translate(
            text, source=opts["--from"], process_id=opts["--id"], index=int(opts["--zap"] or 0)
        )
    except (OSError, ValueError) as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 1
    print(_json.dumps(result.summary(), ensure_ascii=False, indent=2))
    if not opts["--into"]:
        return 0
    root = Path(opts["--into"])
    clash = [rel for rel in result.files if (root / rel).exists()]
    if clash:
        print(f"refused, these exist already: {clash}", file=sys.stderr)
        return 1
    for rel, text in result.files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    manifest = root / "solution.toml"
    for role in result.model_roles:
        text = manifest.read_text(encoding="utf-8")
        if f"[model_roles.{role}]" not in text:
            block = f"\n\n[model_roles.{role}]\nstructured_output = true\n"
            manifest.write_text(text.rstrip() + block, encoding="utf-8")
    report = validate_app(root)
    for finding in [*report.errors, *report.warnings]:
        print(f"  {finding}", file=sys.stderr)
    return 0 if report.ok else 1


def build_catalog_command(argv: list[str]) -> int:
    """``build-catalog <out-dir> <bundle.tgz>... --key <pem> --key-id <id>
    [--base-url <https://…/>] [--revoke id[@version]:reason]`` - the
    platform's catalog.json over signed bundles (ADR-0043)."""
    import argparse

    from . import catalog

    parser = argparse.ArgumentParser(prog="aitmation_core build-catalog")
    parser.add_argument("out_dir")
    parser.add_argument("bundles", nargs="+")
    parser.add_argument("--key", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--revoke", action="append", default=[])
    args = parser.parse_args(argv)
    revoked = []
    for item in args.revoke:
        target, _, reason = item.partition(":")
        solution_id, _, version = target.partition("@")
        revoked.append({"id": solution_id, "version": version, "reason": reason})
    with open(args.key, encoding="ascii") as handle:
        pem = handle.read()
    try:
        out = catalog.build_catalog(
            args.bundles, args.out_dir, pem, args.key_id, base_url=args.base_url, revoked=revoked
        )
    except catalog.CatalogError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"wrote {out} ({len(args.bundles)} bundle(s))")
    return 0


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "validate-app":
        sys.exit(validate_app_command(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] in ("validate-solution", "validate"):
        sys.exit(validate_app_command(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "migrate-solution":
        sys.exit(migrate_command(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        sys.exit(test_command(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "sign-solution":
        sys.exit(sign_command(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "trust-keygen":
        sys.exit(keygen_command(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "pack-solution":
        sys.exit(pack_command(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "import-workflow":
        sys.exit(import_workflow_command(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "build-catalog":
        sys.exit(build_catalog_command(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "host":
        from .host import main as host_main

        sys.exit(host_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = load_settings()
    settings.ensure_layout()
    model_client = default_model_client(settings)
    if model_client is None:
        log.warning(
            "no model API key configured - agent steps run as stubs"
            " (Einstellungen → Modelle in the shell)"
        )
    runtime = Runtime(
        settings,
        model_client=model_client,
        mail_source=default_mail_source(settings),
    )
    app = create_app(runtime)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
