"""``migrate-solution``: format 1 bundle -> format 2 solution (ADR-0035 §11).

Deterministic and additive: ``app.toml`` becomes ``solution.toml`` with
``format = 2``; every ``templates/*.toml`` becomes ``processes/<id>.bpmn``
plus one ``ai/<step>.aimn.toml`` per AgentStep; ``records/`` moves to
``types/``; format-1 acceptance cases stay as end-to-end cases under
``tests/``. ``doctypes/``, ``knowledge/`` and ``samples/`` are copied as
they are. When migrating in place, the format-1 files (``app.toml``,
``templates/``, ``records/``) are removed afterwards - a directory with
both manifests would be ambiguous; version control keeps the history.
The loader itself keeps reading format 1 for one release (ADR-0013).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import aimn
from .apps import App, AppError, load_app
from .bpmn import to_bpmn
from .definition import StepKind
from .template import TemplateError, parse_template


class MigrationError(RuntimeError):
    """The bundle could not be migrated."""


def migrate_solution(source: str | Path, target: str | Path | None = None) -> Path:
    """Migrate the format-1 bundle at ``source`` into ``target`` (default:
    ``<source>`` itself, writing the new layout next to the old files).
    Returns the target path."""
    source = Path(source)
    try:
        app = load_app(source)
    except AppError as exc:
        raise MigrationError(str(exc)) from exc
    if app.format != 1:
        raise MigrationError(f"{source}: already format {app.format}")
    target = Path(target) if target is not None else source
    target.mkdir(parents=True, exist_ok=True)

    (target / "solution.toml").write_text(_manifest_text(source, app), encoding="utf-8")
    processes = target / "processes"
    ai_dir = target / "ai"
    processes.mkdir(exist_ok=True)
    # Briefing ids: the step id when unique across the solution, else
    # qualified with the process id - stable and collision-free.
    counts: dict[str, int] = {}
    for definition in app.templates.values():
        for step in definition.steps.values():
            if step.kind is StepKind.AGENT_STEP:
                counts[step.id] = counts.get(step.id, 0) + 1
    seen_briefings: dict[str, aimn.Briefing] = {}
    for template_id in app.templates:
        # Re-read the template unbound: the briefing must carry the
        # ``{param:...}`` placeholders, not one binding's values.
        file = source / "templates" / f"{template_id}.toml"
        try:
            definition = parse_template(
                file.read_text(encoding="utf-8"), str(file), substitute=False
            )
        except TemplateError as exc:
            raise MigrationError(str(exc)) from exc
        ids = {
            step.id: (step.id if counts.get(step.id, 0) == 1 else f"{definition.id}-{step.id}")
            for step in definition.steps.values()
            if step.kind is StepKind.AGENT_STEP
        }
        xml, briefings = to_bpmn(definition, briefing_ids=ids)
        (processes / f"{definition.id}.bpmn").write_text(xml, encoding="utf-8")
        for bid, briefing in briefings.items():
            if not briefing.model_role:
                briefing = aimn.Briefing(**{**briefing.__dict__, "model_role": "default"})
            seen_briefings[bid] = briefing
    if seen_briefings:
        ai_dir.mkdir(exist_ok=True)
        for bid, briefing in seen_briefings.items():
            (ai_dir / f"{bid}.aimn.toml").write_text(
                aimn.briefing_to_toml(briefing), encoding="utf-8"
            )

    for folder in ("doctypes", "knowledge", "samples", "tests"):
        _copy_tree(source / folder, target / folder)
    _copy_tree(source / "records", target / "types")
    for name in ("README.md", "LICENSE"):
        if (source / name).is_file() and not (target / name).exists():
            shutil.copy2(source / name, target / name)
    if target == source:
        _retire(source)
    return target


def _retire(source: Path) -> None:
    """Remove the format-1 files: ``app.toml`` next to ``solution.toml``
    would make the directory ambiguous, and ``templates/``/``records/``
    are not part of the format-2 layout."""
    for name in ("app.toml",):
        item = source / name
        if item.is_file():
            item.unlink()
    for name in ("templates", "records"):
        item = source / name
        if item.is_dir():
            shutil.rmtree(item)


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_dir():
            _copy_tree(item, dst / item.name)
        elif not (dst / item.name).exists():
            shutil.copy2(item, dst / item.name)


def _manifest_text(source: Path, app: App) -> str:
    """Rewrite the manifest textually: keep the author's comments and
    order, bump ``format``, add ``[model_roles]`` for the roles the
    briefings will need (one neutral ``default`` role)."""
    text = (source / "app.toml").read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    bumped = False
    for line in lines:
        stripped = line.strip()
        if not bumped and stripped.startswith("format") and "=" in stripped:
            comment = line.split("#", 1)[1] if "#" in line else ""
            out.append("format = 2" + (f"  #{comment}" if comment else ""))
            bumped = True
            continue
        out.append(line)
    if not bumped:
        out.insert(0, "format = 2")
    has_agent = any(
        step.kind is StepKind.AGENT_STEP
        for definition in app.templates.values()
        for step in definition.steps.values()
    )
    if has_agent and "[model_roles" not in text:
        out.append("")
        out.append("# Model roles (ADR-0035 §3): what the solution needs, never which model.")
        out.append("[model_roles.default]")
        out.append("structured_output = true")
        out.append("vision = false")
        out.append(f'max_data_class = "{app.manifest.requires.data_class}"')
    return "\n".join(out) + "\n"
