"""App bundles: the catalog unit (ADR-0020) in declaration format v1 (ADR-0023).

An app is a directory with a manifest and, optionally, process templates,
document types and acceptance cases::

    apps/<id>/
      app.toml
      templates/*.toml
      doctypes/*.toml
      records/*.toml     (record schemas, ADR-0026)
      tests/*.toml
      samples/*          (inputs referenced by tests)

Nothing else is allowed - a bundle carries **no executable code**; the
loader rejects unknown files instead of ignoring them. This is what makes
"anyone may publish" safe for a business without an IT department
(docs/ZIELBILD.md §6).

:func:`validate_app` is THE validator: it runs when the instance loads a
bundle, in the workshop before submission, in the shop on acceptance and
as the external acceptance check of a Colabbot contract
(docs/COLABBOT-ANFORDERUNGEN.md C-04). There is no second one.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, aimn, schedule, solution, trust
from .bpmn import BpmnError, parse_bpmn
from .data_classes import DataClassError, demand, rank
from .definition import INTERNAL_ACTIONS, DefinitionError, ProcessDefinition, StepKind
from .doctypes import Doctype, DoctypeError, parse_doctype
from .guardrails import GuardrailError, validate_names
from .i18n import TextError, missing_languages, normalize
from .records import RecordError, RecordSchema, parse_schema
from .template import TemplateError, parse_template

MAIL_INPUTS = ("to", "cc", "reply_to", "subject", "body", "in_reply_to", "attachments")

SUPPORTED_FORMATS = frozenset({1, 2})
SETTING_TYPES = frozenset({"principal", "amount", "duration", "choice", "text", "bool"})

_MANIFEST_KEYS = frozenset(
    {
        "format",
        "id",
        "version",
        "name",
        "description",
        "license",
        "author",
        "requires",
        "settings",
        "provenance",
    }
)
_MANIFEST_KEYS_V2 = _MANIFEST_KEYS | {
    "model_roles", "dependencies", "publisher", "trust_level", "signature",
}
_REQUIRES_KEYS = frozenset({"core", "data_class", "connectors", "guardrails"})
_SETTING_KEYS = frozenset({"type", "label", "required", "default", "help", "choices", "binds"})
_TEST_KEYS = frozenset({"id", "description", "given", "expect"})
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")
_BIND_PATTERN = re.compile(r"^([a-z0-9][a-z0-9_-]*):([a-z0-9_]+)$")
_ALLOWED_TOP_FILES = frozenset({"app.toml", "README.md", "LICENSE"})
_ALLOWED_DIRS = frozenset({"templates", "doctypes", "records", "knowledge", "tests", "samples"})
_SAMPLE_SUFFIXES = frozenset({".txt", ".md", ".pdf", ".eml", ".csv", ".json", ".toml"})


class AppError(ValueError):
    """The bundle cannot be loaded (at least one validation error)."""


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: {self.where}: {self.message}"


@dataclass(frozen=True)
class Setting:
    name: str
    type: str
    label: dict[str, str]
    required: bool = False
    default: object = None
    help: dict[str, str] = field(default_factory=dict)
    choices: tuple[str, ...] = ()
    binds: tuple[str, ...] = ()


@dataclass(frozen=True)
class Requires:
    core: str = "0.0.0"
    data_class: str = "internal"
    connectors: tuple[str, ...] = ()
    guardrails: tuple[str, ...] = ()


@dataclass(frozen=True)
class AcceptanceCase:
    id: str
    description: str
    given: dict
    expect: dict


@dataclass(frozen=True)
class Manifest:
    format: int
    id: str
    version: str
    name: dict[str, str]
    description: dict[str, str] = field(default_factory=dict)
    license: str = ""
    author: dict[str, str] = field(default_factory=dict)
    requires: Requires = field(default_factory=Requires)
    settings: dict[str, Setting] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)
    # Format 2 (ADR-0035): roles a solution needs from models, and other
    # solutions it builds on.
    model_roles: dict[str, solution.ModelRole] = field(default_factory=dict)
    dependencies: dict[str, str] = field(default_factory=dict)
    # Shop fields (ADR-0043): who publishes it, the level the platform
    # assigned, and the platform's signature. The runtime verifies the
    # signature at load (trust.verify_solution); the parser only checks shape.
    publisher: dict[str, str] = field(default_factory=dict)
    trust_level: str | None = None
    signature: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SpaceDeclaration:
    """A knowledge space a bundle expects to exist (ADR-0031): seeded
    insert-if-missing, then the instance's own. Content is never in a
    bundle - a bundle declares the shelf, the owner fills it."""

    id: str
    title: dict[str, str]
    description: dict[str, str]
    data_class: str


@dataclass(frozen=True)
class App:
    path: Path
    manifest: Manifest
    templates: dict[str, ProcessDefinition] = field(default_factory=dict)
    doctypes: dict[str, Doctype] = field(default_factory=dict)
    records: dict[str, RecordSchema] = field(default_factory=dict)
    knowledge: dict[str, SpaceDeclaration] = field(default_factory=dict)
    tests: tuple[AcceptanceCase, ...] = ()
    # Format 2 artefacts (ADR-0035); empty for format-1 bundles.
    briefings: dict[str, aimn.Briefing] = field(default_factory=dict)
    skills: dict[str, aimn.Briefing] = field(default_factory=dict)
    tools: dict[str, solution.ToolDeclaration] = field(default_factory=dict)
    types: dict[str, solution.TypeDeclaration] = field(default_factory=dict)
    constants: dict = field(default_factory=dict)
    configurations: dict[str, solution.Configuration] = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    sidecars: tuple[solution.SidecarCase, ...] = ()
    packages: dict[str, App] = field(default_factory=dict)

    @property
    def format(self) -> int:
        return self.manifest.format

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> str:
        return self.manifest.version


@dataclass
class Report:
    """Validation outcome: the app (if it loaded) plus all findings."""

    app: App | None
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


# -- public API ----------------------------------------------------------


def validate_app(
    path: str | Path,
    *,
    core_definitions: dict[str, ProcessDefinition] | None = None,
    core_version: str = __version__,
    provided: dict[str, App] | None = None,
) -> Report:
    """Validate a bundle directory. Never raises for bundle content errors.

    ``core_definitions`` are the templates the instance already carries
    (intake, mailroom); bundle templates may route into them but never
    replace them. ``provided`` are the already loaded solutions this one
    may depend on (ADR-0035 §4): the root elements of each declared
    dependency are in sight. Without ``provided`` (a single solution
    validated on its own) unresolved references to a declared dependency
    are warnings; the installed set settles them.
    """
    path = Path(path)
    core_definitions = core_definitions or {}
    findings: list[Finding] = []

    def error(where: str, message: str) -> None:
        findings.append(Finding("error", where, message))

    def warning(where: str, message: str) -> None:
        findings.append(Finding("warning", where, message))

    if not path.is_dir():
        error(str(path), "not a directory")
        return Report(None, findings)
    if (path / "solution.toml").is_file():
        return _validate_v2(path, findings, core_definitions, core_version, provided)
    manifest_path = path / "app.toml"
    if not manifest_path.is_file():
        error(str(manifest_path), "manifest app.toml or solution.toml is missing")
        return Report(None, findings)

    _check_files(path, error)

    manifest = _parse_manifest(manifest_path, error, warning, core_version)
    if manifest is None:
        return Report(None, findings)
    if manifest.format != 1:
        error("app.toml", "format 2 bundles use solution.toml as manifest (ADR-0035)")
        return Report(None, findings)
    if manifest.id != path.name:
        warning("app.toml", f"id {manifest.id!r} differs from directory name {path.name!r}")

    # Templates: the full ADR-0014/ADR-0007 loader, per file.
    templates: dict[str, ProcessDefinition] = {}
    for file in _toml_files(path / "templates"):
        where = f"templates/{file.name}"
        try:
            definition = parse_template(file.read_text(encoding="utf-8"), source=where)
        except TemplateError as exc:
            error(where, str(exc))
            continue
        if definition.id in templates:
            error(where, f"duplicate template id {definition.id!r} in bundle")
        elif definition.id in core_definitions:
            error(where, f"template id {definition.id!r} would shadow a core template")
        else:
            templates[definition.id] = definition
        _check_meta_languages(definition, where, warning)

    # Document types: chains must resolve inside the bundle or the core.
    doctypes: dict[str, Doctype] = {}
    for file in _toml_files(path / "doctypes"):
        where = f"doctypes/{file.name}"
        try:
            doctype = parse_doctype(file.read_text(encoding="utf-8"), source=where)
        except DoctypeError as exc:
            error(where, str(exc))
            continue
        if doctype.id in doctypes:
            error(where, f"duplicate document type id {doctype.id!r} in bundle")
            continue
        doctypes[doctype.id] = doctype
        if doctype.chain not in templates and doctype.chain not in core_definitions:
            error(where, f"chain {doctype.chain!r} is neither in this bundle nor in the core")
        for lang in missing_languages(doctype.titles):
            warning(where, f"title has no {lang} text")

    # Record schemas (ADR-0026): structural validation, four languages advised.
    records: dict[str, RecordSchema] = {}
    for file in _toml_files(path / "records"):
        where = f"records/{file.name}"
        try:
            schema = parse_schema(file.read_text(encoding="utf-8"), source=where)
        except RecordError as exc:
            error(where, str(exc))
            continue
        if schema.id in records:
            error(where, f"duplicate record schema id {schema.id!r} in bundle")
            continue
        records[schema.id] = schema
        for lang in missing_languages(schema.title):
            warning(where, f"title has no {lang} text")
        for spec in list(schema.fields.values()) + [
            f for variant in schema.variants.values() for f in variant.values()
        ]:
            for lang in missing_languages(spec.label):
                warning(where, f"field {spec.name!r}: label has no {lang} text")

    # Chain targets (process:) must resolve too - a silent hole otherwise.
    known = set(templates) | set(core_definitions)
    for tid, definition in templates.items():
        for step in definition.steps.values():
            for target in step.next.values():
                if isinstance(target, str) and target.startswith("process:"):
                    successor = target.split(":", 1)[1]
                    if successor not in known:
                        error(
                            f"templates/{tid}",
                            f"step {step.id!r} routes to unknown process {successor!r}",
                        )

    # Data class: no step may demand more than the app declares.
    try:
        declared_rank = rank(manifest.requires.data_class)
    except DataClassError as exc:
        error("app.toml", f"requires.data_class: {exc}")
        declared_rank = None
    if declared_rank is not None:
        for tid, definition in templates.items():
            for step in definition.steps.values():
                if step.kind is not StepKind.AGENT_STEP:
                    continue
                try:
                    need = demand(step.config, definition.policies)
                except DataClassError as exc:  # already reported by the loader
                    error(f"templates/{tid}", f"step {step.id!r}: {exc}")
                    continue
                if rank(need) > declared_rank:
                    error(
                        f"templates/{tid}",
                        f"step {step.id!r} needs data class {need!r} but the app declares"
                        f" requires.data_class = {manifest.requires.data_class!r}",
                    )

    # Settings bind to declared template parameters.
    for name, setting in manifest.settings.items():
        for bind in setting.binds:
            match = _BIND_PATTERN.match(bind)
            if not match:
                error(f"settings.{name}", f"bind {bind!r} must look like '<template>:<parameter>'")
                continue
            tid, param = match.groups()
            if tid not in templates:
                error(f"settings.{name}", f"bind {bind!r}: template {tid!r} is not in this bundle")
            elif param not in templates[tid].parameters:
                error(
                    f"settings.{name}",
                    f"bind {bind!r}: template {tid!r} declares no parameter {param!r}",
                )

    # Knowledge spaces (ADR-0031): declared shelves; a template that binds
    # to a space of this bundle must find it here (fail-closed later too).
    knowledge: dict[str, SpaceDeclaration] = {}
    for file in _toml_files(path / "knowledge"):
        where = f"knowledge/{file.name}"
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
            unknown = set(data) - {"id", "title", "description", "data_class"}
            if unknown:
                raise ValueError(f"unknown keys {sorted(unknown)}")
            sid = str(data.get("id", file.stem))
            if not re.match(r"^[a-z0-9][a-z0-9-]{1,63}$", sid):
                raise ValueError(f"invalid space id {sid!r}")
            rank(str(data.get("data_class", "internal")))
            decl = SpaceDeclaration(
                id=sid,
                title=normalize(data.get("title", sid), "title"),
                description=normalize(data["description"], "description")
                if data.get("description")
                else {},
                data_class=str(data.get("data_class", "internal")),
            )
        except (tomllib.TOMLDecodeError, ValueError, TextError, DataClassError) as exc:
            error(where, str(exc))
            continue
        if decl.id in knowledge:
            error(where, f"duplicate knowledge space id {decl.id!r} in bundle")
            continue
        knowledge[decl.id] = decl
        for lang in missing_languages(decl.title):
            warning(where, f"title has no {lang} text")
    for tid, definition in templates.items():
        for step in definition.steps.values():
            for sid in (step.config.get("context") or {}).get("knowledge") or []:
                if str(sid).startswith("{"):
                    continue  # chosen by the input, checked per case (ADR-0054)
                if str(sid) not in knowledge:
                    warning(
                        f"templates/{tid}",
                        f"step {step.id!r} binds knowledge space {sid!r} that this bundle does"
                        " not declare - the instance must provide it",
                    )

    tests = _parse_tests(path, templates, doctypes, error)

    app = App(
        path=path,
        manifest=manifest,
        templates=templates,
        doctypes=doctypes,
        records=records,
        knowledge=knowledge,
        tests=tests,
    )
    report = Report(app if not any(f.severity == "error" for f in findings) else None, findings)
    return report


def load_app(
    path: str | Path,
    *,
    core_definitions: dict[str, ProcessDefinition] | None = None,
    provided: dict[str, App] | None = None,
) -> App:
    """Load a bundle or raise :class:`AppError` listing every error."""
    report = validate_app(path, core_definitions=core_definitions, provided=provided)
    if report.app is None:
        raise AppError(
            f"{path}: {len(report.errors)} error(s)\n" + "\n".join(f"  {f}" for f in report.errors)
        )
    return report.app


def load_apps(
    directory: str | Path, *, core_definitions: dict[str, ProcessDefinition] | None = None
) -> dict[str, App]:
    """Load every bundle below ``directory`` and check cross-bundle collisions.

    Any error anywhere aborts the whole load - a catalog with one broken
    app is not a catalog the runtime should start from.
    """
    directory = Path(directory)
    apps: dict[str, App] = {}
    template_owner: dict[str, str] = {}
    doctype_owner: dict[str, str] = {}
    record_owner: dict[str, str] = {}
    problems: list[str] = []
    if not directory.is_dir():
        return apps
    for child in _dependency_order(directory):
        try:
            app = load_app(child, core_definitions=core_definitions, provided=apps)
        except AppError as exc:
            problems.append(str(exc))
            continue
        if app.id in apps:
            problems.append(f"{child}: duplicate app id {app.id!r} (also {apps[app.id].path})")
            continue
        for tid in app.templates:
            if tid in template_owner:
                problems.append(
                    f"{child}: template {tid!r} is also provided by app {template_owner[tid]!r}"
                )
            template_owner[tid] = app.id
        for did in app.doctypes:
            if did in doctype_owner:
                problems.append(
                    f"{child}: document type {did!r} is also provided by app {doctype_owner[did]!r}"
                )
            doctype_owner[did] = app.id
        for rid in app.records:
            if rid in record_owner:
                problems.append(
                    f"{child}: record schema {rid!r} is also provided by app {record_owner[rid]!r}"
                )
            record_owner[rid] = app.id
        apps[app.id] = app
    problems.extend(_check_dependencies(apps, core_definitions or {}))
    if problems:
        raise AppError("\n".join(problems))
    return apps


def _dependency_order(directory: Path) -> list[Path]:
    """Bundle directories, dependencies before dependants (a solution's
    manifest names them), otherwise alphabetical. Cycles and unknown
    dependencies fall back to alphabetical order; _check_dependencies
    reports them."""
    candidates: list[Path] = []
    declared: dict[str, tuple[Path, list[str]]] = {}
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        if (child / "solution.toml").is_file():
            deps: list[str] = []
            solution_id = child.name
            try:
                data = tomllib.loads((child / "solution.toml").read_text(encoding="utf-8"))
                solution_id = str(data.get("id", child.name))
                raw = data.get("dependencies")
                deps = [str(k) for k in raw] if isinstance(raw, dict) else []
            except (OSError, tomllib.TOMLDecodeError):
                pass
            declared[solution_id] = (child, deps)
            candidates.append(child)
        elif (child / "app.toml").is_file():
            candidates.append(child)
    ordered: list[Path] = []
    state: dict[Path, int] = {}

    def visit(child: Path, solution_id: str | None) -> None:
        if state.get(child) == 2:
            return
        if state.get(child) == 1:
            return  # cycle: reported later
        state[child] = 1
        if solution_id is not None:
            for dep in declared.get(solution_id, (child, []))[1]:
                if dep in declared:
                    visit(declared[dep][0], dep)
        state[child] = 2
        ordered.append(child)

    by_path = {path: sid for sid, (path, _deps) in declared.items()}
    for child in candidates:
        visit(child, by_path.get(child))
    return ordered


def version_satisfies(version: str, spec: str) -> bool:
    """SemVer ranges as the manifest allows them: ``1.2.3`` exact,
    ``^1.2`` same major (and minor for 0.x), ``~1.2.3`` same major.minor,
    ``=1.2.3`` exact."""
    if not solution.SEMVER_RANGE.match(spec or ""):
        return False
    try:
        have = _version_tuple(version)
    except (ValueError, AttributeError):
        return False
    op = spec[0] if spec[0] in "^~=" else ""
    want = _version_tuple(spec[1:] if op else spec)
    want = tuple(list(want) + [0] * (3 - len(want)))
    if op in ("", "="):
        return have == want
    if have < want:
        return False
    if op == "~":
        return have[:2] == want[:2]
    # caret: 0.x pins the minor, otherwise the major
    return have[:2] == want[:2] if want[0] == 0 else have[0] == want[0]


def _check_dependencies(apps: dict[str, App], core_definitions: dict) -> list[str]:
    """Dependencies between solutions (ADR-0035 §4): present, version in
    range, no cycles - and every cross-solution chain target provided."""
    problems: list[str] = []
    for app in apps.values():
        for dep, spec in app.manifest.dependencies.items():
            other = apps.get(dep)
            if other is None:
                problems.append(f"{app.id}: depends on {dep!r} which is not installed")
            elif not version_satisfies(other.version, spec):
                problems.append(
                    f"{app.id}: needs {dep} {spec}, installed is {other.version}"
                )
        provided = set(app.templates) | set(core_definitions)
        for dep in app.manifest.dependencies:
            if dep in apps:
                provided |= set(apps[dep].templates)
        for tid, definition in app.templates.items():
            for successor in definition.process_targets():
                if successor not in provided:
                    problems.append(
                        f"{app.id}: process {tid!r} routes to {successor!r}, which neither the"
                        " solution nor its dependencies provide"
                    )
            for called in definition.call_targets():
                if called not in provided:
                    problems.append(
                        f"{app.id}: process {tid!r} calls {called!r}, which neither the"
                        " solution nor its dependencies provide"
                    )
    # Cycles: depth-first over the declared edges.
    state: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> None:
        if state.get(node) == 1:
            problems.append("dependency cycle: " + " -> ".join([*trail, node]))
            return
        if state.get(node) == 2 or node not in apps:
            return
        state[node] = 1
        for dep in apps[node].manifest.dependencies:
            visit(dep, [*trail, node])
        state[node] = 2

    for app_id in apps:
        visit(app_id, [])
    return problems


# -- internals -------------------------------------------------------------


def _check_files(path: Path, error) -> None:
    """The bundle is declarations only. Anything else is rejected."""
    for item in sorted(path.rglob("*")):
        rel = item.relative_to(path)
        parts = rel.parts
        if any(part.startswith(".") for part in parts):
            continue  # editor droppings, VCS metadata
        if item.is_dir():
            if len(parts) == 1 and parts[0] not in _ALLOWED_DIRS:
                error(str(rel), "directory not allowed in a declarative bundle")
            elif len(parts) > 1 and parts[0] != "samples":
                error(str(rel), "nested directories are not allowed here")
            continue
        if len(parts) == 1:
            if parts[0] not in _ALLOWED_TOP_FILES:
                error(str(rel), "file not allowed at bundle root")
            continue
        top = parts[0]
        if top in {"templates", "doctypes", "records", "knowledge", "tests"}:
            if len(parts) != 2 or item.suffix != ".toml":
                error(str(rel), f"only *.toml files are allowed under {top}/")
        elif top == "samples":
            if item.suffix.lower() not in _SAMPLE_SUFFIXES:
                error(str(rel), "sample type not allowed (no executables, no archives)")
        else:
            error(str(rel), "file outside the allowed bundle layout")


def _parse_manifest(manifest_path: Path, error, warning, core_version: str) -> Manifest | None:
    where = manifest_path.name
    try:
        data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        error(where, f"invalid TOML: {exc}")
        return None
    allowed = _MANIFEST_KEYS_V2 if data.get("format") == 2 else _MANIFEST_KEYS
    unknown = set(data) - allowed
    if unknown:
        error(where, f"unknown keys {sorted(unknown)}")
    ok = True
    for key in ("format", "id", "version", "name"):
        if key not in data:
            error(where, f"missing required key {key!r}")
            ok = False
    if not ok:
        return None

    fmt = data["format"]
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt not in SUPPORTED_FORMATS:
        error(where, f"unsupported format {fmt!r} (supported: {sorted(SUPPORTED_FORMATS)})")
        return None
    app_id = data["id"]
    if not isinstance(app_id, str) or not _ID_PATTERN.match(app_id):
        error(where, f"id {app_id!r} must match {_ID_PATTERN.pattern}")
        return None
    version = data["version"]
    if not isinstance(version, str) or not _SEMVER_PATTERN.match(version):
        error(where, f"version {version!r} must be semantic (MAJOR.MINOR.PATCH)")
        version = "0.0.0"

    try:
        name = normalize(data["name"], "name")
    except TextError as exc:
        error(where, str(exc))
        return None
    for lang in missing_languages(name):
        warning(where, f"name has no {lang} text")
    description: dict[str, str] = {}
    if "description" in data:
        try:
            description = normalize(data["description"], "description")
        except TextError as exc:
            error(where, str(exc))
        else:
            for lang in missing_languages(description):
                warning(where, f"description has no {lang} text")

    license_ = data.get("license", "")
    if not isinstance(license_, str):
        error(where, "license must be a string")
        license_ = ""
    author = data.get("author", {})
    if not isinstance(author, dict) or not all(isinstance(v, str) for v in author.values()):
        error(where, "author must be a table of strings")
        author = {}

    requires = _parse_requires(data.get("requires", {}), error, warning, core_version, fmt)
    settings = _parse_settings(data.get("settings", {}), error, warning)
    model_roles: dict[str, solution.ModelRole] = {}
    dependencies: dict[str, str] = {}
    if fmt == 2:
        try:
            model_roles = solution.parse_model_roles(data.get("model_roles"), where)
            dependencies = solution.parse_dependencies(data.get("dependencies"), where)
        except solution.SolutionError as exc:
            error(where, str(exc))
        if app_id in dependencies:
            error(where, "a solution cannot depend on itself")

    provenance = data.get("provenance", {})
    if not isinstance(provenance, dict) or not all(isinstance(v, str) for v in provenance.values()):
        error(where, "provenance must be a table of strings")
        provenance = {}
    elif any(provenance.values()):
        warning(where, "provenance is set but unsigned - the platform verifies it on acceptance")
    publisher = data.get("publisher", {})
    if not isinstance(publisher, dict) or not all(isinstance(v, str) for v in publisher.values()):
        error(where, "publisher must be a table of strings (name, url, contact)")
        publisher = {}
    trust_level = data.get("trust_level")
    if trust_level is not None and trust_level not in trust.LEVELS:
        error(where, f"trust_level must be one of {list(trust.LEVELS)}")
        trust_level = None
    signature = data.get("signature", {})
    if not isinstance(signature, dict) or not all(isinstance(v, str) for v in signature.values()):
        error(where, "signature must be a table of strings")
        signature = {}
    elif signature and trust_level is None:
        warning(where, "signature without trust_level - the bundle counts as local")

    return Manifest(
        format=fmt,
        id=app_id,
        version=version,
        name=name,
        description=description,
        license=license_,
        author=dict(author),
        requires=requires,
        settings=settings,
        provenance={k: str(v) for k, v in provenance.items()},
        model_roles=model_roles,
        dependencies=dependencies,
        publisher={k: str(v) for k, v in publisher.items()},
        trust_level=trust_level,
        signature={k: str(v) for k, v in signature.items()},
    )


def _parse_requires(raw: object, error, warning, core_version: str, fmt: int = 1) -> Requires:
    where = "[requires]"
    if not isinstance(raw, dict):
        error(where, "requires must be a table")
        return Requires()
    unknown = set(raw) - _REQUIRES_KEYS
    if unknown:
        error(where, f"unknown keys {sorted(unknown)}")
    core = str(raw.get("core", "0.0.0"))
    if not _SEMVER_PATTERN.match(core):
        error(where, f"core {core!r} must be a semantic version")
    elif _version_tuple(core) > _version_tuple(core_version):
        error(where, f"needs core >= {core}, this core is {core_version}")
    data_class = str(raw.get("data_class", "internal"))
    try:
        rank(data_class)
    except DataClassError as exc:
        error(where, str(exc))
    connectors = raw.get("connectors", [])
    if not isinstance(connectors, list) or not all(isinstance(c, str) for c in connectors):
        error(where, "connectors must be a list of strings")
        connectors = []
    elif connectors and fmt == 1:
        # Format v1: no connector layer exists yet (ADR-0006 is the plan,
        # not the state). Declaring one would promise what cannot run.
        error(where, f"connectors are not supported in format 1 (declared: {connectors})")
    elif connectors:
        warning(where, f"connectors are declared but no connector layer runs yet: {connectors}")
    guardrails = raw.get("guardrails", [])
    if not isinstance(guardrails, list) or not all(isinstance(g, str) for g in guardrails):
        error(where, "guardrails must be a list of strings")
        guardrails = []
    else:
        try:
            validate_names(guardrails, where)
        except GuardrailError as exc:
            error(where, str(exc))
    return Requires(
        core=core,
        data_class=data_class,
        connectors=tuple(connectors),
        guardrails=tuple(guardrails),
    )


def _parse_settings(raw: object, error, warning) -> dict[str, Setting]:
    if not isinstance(raw, dict):
        error("[settings]", "settings must be a table of tables")
        return {}
    out: dict[str, Setting] = {}
    for name, spec in raw.items():
        where = f"[settings.{name}]"
        if not re.match(r"^[a-z][a-z0-9_]*$", str(name)):
            error(where, "setting names are lowercase identifiers")
            continue
        if not isinstance(spec, dict):
            error(where, "must be a table")
            continue
        unknown = set(spec) - _SETTING_KEYS
        if unknown:
            error(where, f"unknown keys {sorted(unknown)}")
        stype = spec.get("type")
        if stype not in SETTING_TYPES:
            error(where, f"type {stype!r} must be one of {sorted(SETTING_TYPES)}")
            continue
        if "label" not in spec:
            error(where, "label is required")
            continue
        try:
            label = normalize(spec["label"], "label")
            help_ = normalize(spec["help"], "help") if "help" in spec else {}
        except TextError as exc:
            error(where, str(exc))
            continue
        for lang in missing_languages(label):
            warning(where, f"label has no {lang} text")
        choices = spec.get("choices", [])
        if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
            error(where, "choices must be a list of strings")
            choices = []
        if stype == "choice" and not choices:
            error(where, "type 'choice' needs a non-empty choices list")
        binds = spec.get("binds", [])
        if not isinstance(binds, list) or not all(isinstance(b, str) for b in binds):
            error(where, "binds must be a list of '<template>:<parameter>' strings")
            binds = []
        required = spec.get("required", False)
        if not isinstance(required, bool):
            error(where, "required must be true or false")
            required = False
        out[str(name)] = Setting(
            name=str(name),
            type=str(stype),
            label=label,
            required=required,
            default=spec.get("default"),
            help=help_,
            choices=tuple(choices),
            binds=tuple(binds),
        )
    return out


def _parse_tests(
    path: Path, templates: dict[str, ProcessDefinition], doctypes: dict[str, Doctype], error
) -> tuple[AcceptanceCase, ...]:
    """Acceptance cases are validated structurally (format v1); they run
    in the platform's proving instance (Horizont 2), because they need a
    model."""
    tests_dir = path / "tests"
    if not tests_dir.is_dir():
        return ()
    cases: list[AcceptanceCase] = []
    seen: set[str] = set()
    for file in sorted(tests_dir.glob("*.toml")):
        where = f"tests/{file.name}"
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            error(where, f"invalid TOML: {exc}")
            continue
        unknown = set(data) - _TEST_KEYS
        if unknown:
            error(where, f"unknown keys {sorted(unknown)}")
        case_id = data.get("id")
        if not isinstance(case_id, str) or not _ID_PATTERN.match(case_id):
            error(where, f"id {case_id!r} must match {_ID_PATTERN.pattern}")
            continue
        if case_id in seen:
            error(where, f"duplicate case id {case_id!r}")
            continue
        seen.add(case_id)
        given = data.get("given")
        if not isinstance(given, dict) or not (("text" in given) ^ ("file" in given)):
            error(where, "given needs exactly one of 'text' or 'file'")
            continue
        if "file" in given:
            sample = path / str(given["file"])
            if not solution.sample_inside(path, str(given["file"])) or not sample.is_file():
                error(
                    where, f"given.file {given['file']!r} must point to an existing samples/ file"
                )
        expect = data.get("expect")
        if not isinstance(expect, dict) or not expect:
            error(where, "expect must be a non-empty table")
            continue
        if "doctype" in expect:
            if expect["doctype"] not in doctypes and expect["doctype"] != "unknown":
                error(where, f"expect.doctype {expect['doctype']!r} is not declared in this bundle")
        elif "template" in expect and "outcome" in expect:
            tid = expect["template"]
            if tid not in templates:
                error(where, f"expect.template {tid!r} is not in this bundle")
            else:
                outcomes = {
                    outcome for step in templates[tid].steps.values() for outcome in step.next
                }
                if expect["outcome"] not in outcomes:
                    error(where, f"expect.outcome {expect['outcome']!r} is no outcome of {tid!r}")
        else:
            error(where, "expect needs 'doctype' or 'template' + 'outcome'")
        cases.append(
            AcceptanceCase(
                id=case_id,
                description=str(data.get("description", "")),
                given=dict(given),
                expect=dict(expect),
            )
        )
    return tuple(cases)


def _check_meta_languages(definition: ProcessDefinition, where: str, warning) -> None:
    for key in ("title", "description"):
        value = definition.meta.get(key)
        if value is None:
            continue
        try:
            table = normalize(value, f"meta.{key}")
        except TextError as exc:
            warning(where, str(exc))
            continue
        for lang in missing_languages(table):
            warning(where, f"meta.{key} has no {lang} text")


def _toml_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.toml")) if directory.is_dir() else []


def _version_tuple(version: str) -> tuple[int, ...]:
    core = version.split("-", 1)[0].split("+", 1)[0]
    return tuple(int(part) for part in core.split("."))


# -- format 2: solutions (ADR-0035) --------------------------------------------


@dataclass
class _Scope:
    """One visibility scope: the solution root or a package below it
    (ADR-0035 §4). An element sees its own scope and every scope above;
    siblings are invisible. Ids stay unique solution-wide."""

    where: str  # "" at the root, "packages/<name>/" below
    briefings: dict[str, aimn.Briefing] = field(default_factory=dict)
    skills: dict[str, aimn.Briefing] = field(default_factory=dict)
    tools: dict[str, solution.ToolDeclaration] = field(default_factory=dict)
    types: dict[str, solution.TypeDeclaration] = field(default_factory=dict)
    records: dict[str, RecordSchema] = field(default_factory=dict)
    constants: dict = field(default_factory=dict)
    knowledge: dict[str, SpaceDeclaration] = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    templates: dict[str, ProcessDefinition] = field(default_factory=dict)
    doctypes: dict[str, Doctype] = field(default_factory=dict)
    sidecars: list[solution.SidecarCase] = field(default_factory=list)
    children: dict[str, _Scope] = field(default_factory=dict)

    def merged(self, other: _Scope) -> _Scope:
        """What ``other`` (a child) sees: this scope plus its own."""
        out = _Scope(where=other.where)
        for name in (
            "briefings", "skills", "tools", "types", "records", "constants", "knowledge",
            "decisions", "templates", "doctypes",
        ):
            table = dict(getattr(self, name))
            table.update(getattr(other, name))
            setattr(out, name, table)
        return out


def _validate_v2(
    path: Path,
    findings: list[Finding],
    core_definitions: dict[str, ProcessDefinition],
    core_version: str,
    provided: dict[str, App] | None = None,
) -> Report:
    """The format-2 branch of THE validator: same contract, more folders,
    scoped visibility through packages."""

    def error(where: str, message: str) -> None:
        findings.append(Finding("error", where, message))

    def warning(where: str, message: str) -> None:
        findings.append(Finding("warning", where, message))

    _check_files_v2(path, error)
    manifest = _parse_manifest(path / "solution.toml", error, warning, core_version)
    if manifest is None:
        return Report(None, findings)
    if manifest.format != 2:
        error("solution.toml", "solution.toml is the format-2 manifest; set format = 2")
        return Report(None, findings)
    if manifest.id != path.name:
        warning("solution.toml", f"id {manifest.id!r} differs from directory name {path.name!r}")

    configurations = _parse_configurations(path / "configurations", error)
    seen: dict[str, dict[str, str]] = {}
    inherited, lenient = _inherited_from_dependencies(manifest, provided, seen)
    root = _parse_scope(
        path, "", manifest, core_definitions, inherited, seen, configurations, error, warning,
        lenient=lenient,
    )

    # Flatten for the runtime: the engine works on solution-wide ids.
    flat = _Scope(where="")
    _collect(root, flat)
    _cross_checks(path, manifest, flat.templates, core_definitions, flat.knowledge, error, warning)
    tests = _parse_tests(path, flat.templates, flat.doctypes, error)

    cases_dir = path / "cases"
    for file in sorted(cases_dir.glob("*.cmmn")) if cases_dir.is_dir() else []:
        where = f"cases/{file.name}"
        text = file.read_text(encoding="utf-8")
        if "<!DOCTYPE" in text or "<!ENTITY" in text:
            error(where, "DOCTYPE/ENTITY declarations are not allowed")
            continue
        try:
            import xml.etree.ElementTree as ET

            ET.fromstring(text)
        except ET.ParseError as exc:
            error(where, f"invalid XML: {exc}")
            continue
        warning(where, "CMMN cases are stored but not executed yet (ADR-0034 §3)")

    app = App(
        path=path,
        manifest=manifest,
        templates=flat.templates,
        doctypes=flat.doctypes,
        records=flat.records,
        knowledge=flat.knowledge,
        tests=tests,
        briefings=flat.briefings,
        skills=flat.skills,
        tools=flat.tools,
        types=flat.types,
        constants=flat.constants,
        configurations=configurations,
        decisions=flat.decisions,
        sidecars=tuple(flat.sidecars),
        packages={name: _package_summary(child) for name, child in root.children.items()},
    )
    return Report(app if not any(f.severity == "error" for f in findings) else None, findings)


def _collect(scope: _Scope, into: _Scope) -> None:
    for name in (
        "briefings", "skills", "tools", "types", "records", "constants", "knowledge",
        "decisions", "templates", "doctypes",
    ):
        getattr(into, name).update(getattr(scope, name))
    into.sidecars.extend(scope.sidecars)
    for child in scope.children.values():
        _collect(child, into)


def _package_summary(scope: _Scope) -> dict:
    return {
        "where": scope.where,
        "processes": sorted(scope.templates),
        "decisions": sorted(scope.decisions),
        "ai": sorted(scope.briefings),
        "skills": sorted(scope.skills),
        "tools": sorted(scope.tools),
        "types": sorted(scope.types) + sorted(scope.records),
        "knowledge": sorted(scope.knowledge),
        "doctypes": sorted(scope.doctypes),
        "packages": {name: _package_summary(c) for name, c in scope.children.items()},
    }


def _inherited_from_dependencies(
    manifest: Manifest, provided: dict[str, App] | None, seen: dict[str, dict[str, str]]
) -> tuple[_Scope, bool]:
    """The scope a solution's root inherits from its dependencies: their
    root elements (ADR-0035 §4 - over solution boundaries only through
    ``[dependencies]``). Returns the scope and whether unresolved
    references should be warnings because a dependency is not at hand."""
    scope = _Scope(where="")
    lenient = False
    for dep in manifest.dependencies:
        app = (provided or {}).get(dep)
        if app is None:
            lenient = True
            continue
        origin = f"dependency {dep}"
        for attr, kind in (
            ("briefings", "briefing"), ("skills", "skill"), ("tools", "tool"),
            ("types", "type"), ("records", "record"), ("knowledge", "knowledge space"),
            ("decisions", "decision"), ("templates", "process"),
        ):
            table = dict(getattr(scope, attr))
            for element_id, element in getattr(app, attr).items():
                table.setdefault(element_id, element)
                seen.setdefault(kind, {}).setdefault(element_id, origin)
            setattr(scope, attr, table)
        merged = dict(scope.constants)
        for key, value in app.constants.items():
            merged.setdefault(key, value)
        scope.constants = merged
    return scope, lenient


def _unique(seen: dict[str, dict[str, str]], kind: str, element_id: str, where: str, error) -> bool:
    owners = seen.setdefault(kind, {})
    if element_id in owners:
        error(where, f"{kind} id {element_id!r} is already defined in {owners[element_id]}")
        return False
    owners[element_id] = where
    return True


def _parse_scope(
    path: Path,
    where: str,
    manifest: Manifest,
    core_definitions: dict[str, ProcessDefinition],
    inherited: _Scope,
    seen: dict[str, dict[str, str]],
    configurations: dict[str, solution.Configuration],
    error,
    warning,
    *,
    lenient: bool = False,
) -> _Scope:
    def perror(sub: str, message: str) -> None:
        error(where + sub, message)

    def pwarning(sub: str, message: str) -> None:
        warning(where + sub, message)

    # A reference that may be satisfied by a dependency not at hand is a
    # warning when validating this solution alone, an error when installed.
    foreign = pwarning if lenient else perror

    own = _Scope(where=where)
    own.briefings = _parse_briefings(path / "ai", perror, pwarning)
    own.skills = _parse_skills(path / "skills", perror, pwarning)
    own.tools = _parse_tools(path / "tools", perror, pwarning)
    own.types, own.records = _parse_types(path / "types", perror, pwarning)
    own.constants = _parse_constants_dir(path / "constants", perror)
    own.knowledge = _parse_knowledge(path / "knowledge", perror, pwarning)
    own.decisions = _parse_decisions(path / "decisions", perror, pwarning)
    for kind, table in (
        ("briefing", own.briefings),
        ("skill", own.skills),
        ("tool", own.tools),
        ("type", own.types),
        ("record", own.records),
        ("knowledge space", own.knowledge),
        ("decision", own.decisions),
    ):
        for element_id in list(table):
            if not _unique(seen, kind, element_id, where or "<root>", error):
                del table[element_id]
    visible = inherited.merged(own)

    # Briefings: model roles, tools and skills must exist (in sight).
    for owner, table in (("ai", own.briefings), ("skills", own.skills)):
        for bid, briefing in table.items():
            sub = f"{owner}/{bid}"
            if briefing.model_role and briefing.model_role not in manifest.model_roles:
                perror(
                    sub,
                    f"model_role {briefing.model_role!r} is not declared in solution.toml"
                    " [model_roles]",
                )
            for tool_id in briefing.tools:
                if tool_id not in visible.tools and tool_id not in INTERNAL_ACTIONS:
                    foreign(sub, f"tool {tool_id!r} is not declared under tools/ in this scope")
            for skill_id in briefing.skills:
                if skill_id not in visible.skills:
                    foreign(sub, f"skill {skill_id!r} is not declared under skills/ in this scope")
            if owner == "ai" and not briefing.model_role and manifest.model_roles:
                pwarning(sub, "briefing names no model_role - the instance binds 'default'")

    # Processes: the BPMN profile loader, per file; references resolved in sight.
    processes_dir = path / "processes"
    for file in sorted(processes_dir.glob("*.bpmn")) if processes_dir.is_dir() else []:
        sub = f"processes/{file.name}"
        try:
            definition = parse_bpmn(
                file.read_text(encoding="utf-8"),
                where + sub,
                briefings=visible.briefings,
                skills=visible.skills,
                constants=visible.constants,
                tools=visible.tools,
            )
        except (BpmnError, TemplateError) as exc:
            if lenient and "references briefing" in str(exc):
                pwarning(sub, f"{exc} - may be provided by a dependency")
            else:
                perror(sub, str(exc))
            continue
        if definition.id != solution.subject_of(file):
            pwarning(sub, f"process id {definition.id!r} differs from file name")
        if definition.id in core_definitions:
            perror(sub, f"process id {definition.id!r} would shadow a core template")
            continue
        if not _unique(seen, "process", definition.id, where + sub, error):
            continue
        own.templates[definition.id] = definition
        _check_meta_languages(definition, where + sub, pwarning)
        _bind_tools(definition, visible.tools, where + sub, error)
        for step in definition.steps.values():
            if step.kind is StepKind.SYSTEM_ACTION and step.config.get("action") == "decide":
                ref = str(step.config.get("decision", ""))
                if ref not in visible.decisions:
                    foreign(sub, f"step {step.id!r} calls decision {ref!r} not in sight")
            if step.kind is StepKind.SYSTEM_ACTION and step.config.get("action") == "record":
                _check_record_step(step, visible.records, where + sub, error)
            if step.kind is StepKind.SYSTEM_ACTION and step.config.get("action") == "knowledge":
                _check_knowledge_step(step, visible.knowledge, where + sub, error)
            if step.kind is StepKind.SYSTEM_ACTION and step.config.get("action") == "rows":
                _check_rows_step(step, visible.records, where + sub, error)
            if step.kind is StepKind.SYSTEM_ACTION and step.config.get("action") == "links":
                _check_links_step(step, where + sub, error)
            if step.kind is StepKind.SYSTEM_ACTION and step.config.get("action") == "figures":
                days = step.config.get("days", 7)
                if not isinstance(days, int) or not 1 <= days <= 90:
                    error(where + sub, f"step {step.id!r}: figures.days must be 1..90")
            if step.kind is StepKind.TRIGGER and step.config.get("source") == "schedule":
                try:
                    schedule.parse_schedule(step.config, source=definition.id)
                except schedule.ScheduleError as exc:
                    error(where + sub, str(exc))
    visible.templates.update(own.templates)
    for definition in own.templates.values():
        for step in definition.steps.values():
            if step.kind is StepKind.SYSTEM_ACTION and step.config.get("action") == "call":
                target = str(step.config.get("process", ""))
                if target not in visible.templates and target not in core_definitions:
                    foreign(
                        f"processes/{definition.id}",
                        f"step {step.id!r} calls process {target!r} not in sight",
                    )
    own.doctypes = _parse_doctypes(
        path / "doctypes", visible.templates, core_definitions, perror, pwarning
    )
    for did in list(own.doctypes):
        if not _unique(seen, "document type", did, where or "<root>", error):
            del own.doctypes[did]
    root = path if not where else path.parents[len(Path(where).parts) - 1]
    _check_stub_outcomes(own.templates, configurations, where, perror)
    own.sidecars = list(
        _parse_sidecars(
            path, root, own.templates, own.briefings, own.skills, own.decisions, visible.tools,
            configurations, perror, pwarning if manifest.dependencies else None,
        )
    )
    packages_dir = path / "packages"
    if packages_dir.is_dir():
        for child in sorted(packages_dir.iterdir()):
            if not child.is_dir():
                continue
            if not re.match(r"^[a-z][a-z0-9-]*$", child.name):
                perror(f"packages/{child.name}", "package names are lowercase identifiers")
                continue
            if (child / "solution.toml").is_file():
                perror(f"packages/{child.name}", "a package has no manifest of its own")
                continue
            child_where = f"{where}packages/{child.name}/"
            _check_files_v2(child, lambda w, m, cw=child_where: error(cw + w, m), package=True)
            own.children[child.name] = _parse_scope(
                child, child_where, manifest, core_definitions, visible, seen, configurations,
                error, warning, lenient=lenient,
            )
    return own


def _check_sample_ref(path: Path, case, error, where: str) -> None:
    """``given.file`` names a file under the solution's ``samples/`` and
    nothing else - a bundle (shop, untrusted) must never be able to point
    a proving run at a customer document (ADR-0035 §8)."""
    ref = case.given.get("file")
    if ref is None:
        return
    if not solution.sample_inside(path, str(ref)):
        error(where, f"case {case.id!r}: given.file must name a file under samples/")
    elif not (path / str(ref)).is_file():
        error(where, f"case {case.id!r}: sample {ref!r} not found")


def _check_files_v2(path: Path, error, *, package: bool = False) -> None:
    top_files = solution.TOP_FILES - ({"solution.toml"} if package else set())
    for item in sorted(path.rglob("*")):
        rel = item.relative_to(path)
        parts = rel.parts
        if any(part.startswith(".") for part in parts):
            if parts[0] in (".git", ".gitignore", ".gitattributes") or parts[-1] in (
                ".gitkeep", ".DS_Store"
            ):
                continue  # VCS metadata and desktop litter are tolerated, nothing else
            error(str(rel), "hidden files and directories are not allowed in a solution")
            continue
        if item.is_symlink():
            # A link could point anywhere - into customer data, say; a
            # bundle is self-contained or it is not a bundle.
            error(str(rel), "symlinks are not allowed in a solution")
            continue
        if parts[0] == "packages":
            # Packages are validated recursively with the same rules.
            if len(parts) == 1:
                continue
            if len(parts) == 2 and item.is_dir():
                continue
            if len(parts) == 2:
                error(str(rel), "packages/ holds directories only")
            continue
        if item.is_dir():
            if len(parts) == 1 and parts[0] not in solution.TYPE_DIRS:
                error(str(rel), "directory not allowed in a declarative solution")
            elif len(parts) == 2 and parts[0] == "skills":
                continue  # skills/<id>/
            elif len(parts) == 2 and parts[0] == "tests" and parts[1] == "recordings":
                continue
            elif len(parts) > 1 and parts[0] != "samples":
                error(str(rel), "nested directories are not allowed here")
            continue
        if len(parts) == 1:
            if parts[0] not in top_files:
                error(str(rel), "file not allowed at solution root")
            continue
        top = parts[0]
        if package and top in ("configurations", "tests", "samples", "cases"):
            error(str(rel), f"{top}/ belongs to the solution root, not to a package")
            continue
        if top in {"types", "constants", "tools", "knowledge", "doctypes", "configurations", "ai"}:
            if len(parts) != 2 or not item.name.endswith(".toml"):
                error(str(rel), f"only *.toml files are allowed under {top}/")
        elif top == "processes":
            if len(parts) != 2 or item.suffix not in (".bpmn", ".toml"):
                error(str(rel), "only *.bpmn and *.test.toml are allowed under processes/")
            elif item.suffix == ".toml" and not item.name.endswith(".test.toml"):
                error(str(rel), "processes/ holds *.bpmn and *.test.toml sidecars only")
        elif top == "decisions":
            if len(parts) != 2 or item.suffix not in (".dmn", ".toml"):
                error(str(rel), "only *.dmn and *.test.toml are allowed under decisions/")
        elif top == "cases":
            if len(parts) != 2 or item.suffix != ".cmmn":
                error(str(rel), "only *.cmmn files are allowed under cases/")
        elif top == "skills":
            if len(parts) != 3 or item.name not in ("skill.toml", "skill.test.toml"):
                error(str(rel), "a skill is skills/<id>/skill.toml (+ skill.test.toml)")
        elif top == "tests":
            if len(parts) == 2 and item.suffix == ".toml":
                continue
            if len(parts) == 3 and parts[1] == "recordings" and item.suffix == ".json":
                continue
            error(str(rel), "tests/ holds *.toml suites and recordings/*.json")
        elif top == "samples":
            if item.suffix.lower() not in solution.SAMPLE_SUFFIXES:
                error(str(rel), "sample type not allowed (no executables, no archives)")
        else:
            error(str(rel), "file outside the allowed solution layout")


def _parse_briefings(directory: Path, error, warning) -> dict[str, aimn.Briefing]:
    out: dict[str, aimn.Briefing] = {}
    if not directory.is_dir():
        return out
    for file in sorted(directory.glob("*.toml")):
        if file.name.endswith(".test.toml"):
            continue
        where = f"ai/{file.name}"
        try:
            briefing = aimn.parse_briefing(
                file.read_text(encoding="utf-8"), where, default_id=solution.subject_of(file)
            )
        except aimn.BriefingError as exc:
            error(where, str(exc))
            continue
        if briefing.id != solution.subject_of(file):
            warning(where, f"briefing id {briefing.id!r} differs from file name")
        if briefing.id in out:
            error(where, f"duplicate briefing id {briefing.id!r}")
            continue
        out[briefing.id] = briefing
    return out


def _parse_skills(directory: Path, error, warning) -> dict[str, aimn.Briefing]:
    out: dict[str, aimn.Briefing] = {}
    if not directory.is_dir():
        return out
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        where = f"skills/{child.name}/skill.toml"
        file = child / "skill.toml"
        if not file.is_file():
            error(where, "skill.toml is missing")
            continue
        try:
            skill = aimn.parse_briefing(file.read_text(encoding="utf-8"), where, child.name)
        except aimn.BriefingError as exc:
            error(where, str(exc))
            continue
        if skill.id != child.name:
            error(where, f"skill id {skill.id!r} must equal the directory name")
            continue
        if skill.skills:
            error(where, "a skill cannot reference other skills (fragments stay flat)")
        if not skill.instructions:
            warning(where, "skill has no instructions")
        out[skill.id] = skill
    return out


def _parse_tools(directory: Path, error, warning) -> dict[str, solution.ToolDeclaration]:
    out: dict[str, solution.ToolDeclaration] = {}
    for file in _toml_files(directory):
        where = f"tools/{file.name}"
        try:
            tool = solution.parse_tool(file.read_text(encoding="utf-8"), where, file.stem)
        except solution.SolutionError as exc:
            error(where, str(exc))
            continue
        if tool.id in out:
            error(where, f"duplicate tool id {tool.id!r}")
            continue
        if tool.action and tool.action in INTERNAL_ACTIONS and tool.effect != "internal":
            warning(where, f"action {tool.action!r} is internal; effect should be 'internal'")
        if tool.action and tool.action not in INTERNAL_ACTIONS and tool.effect != "external":
            error(where, f"action {tool.action!r} has outside effect; declare effect = 'external'")
        if tool.http is not None and tool.effect == "read":
            error(where, "an http connector sends; its effect is 'external', never 'read'")
        for lang in missing_languages(tool.description):
            warning(where, f"description has no {lang} text")
        out[tool.id] = tool
    return out


def _parse_types(directory: Path, error, warning):
    types: dict[str, solution.TypeDeclaration] = {}
    records: dict[str, RecordSchema] = {}
    for file in _toml_files(directory):
        where = f"types/{file.name}"
        text = file.read_text(encoding="utf-8")
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            error(where, f"invalid TOML: {exc}")
            continue
        if data.get("kind") in ("single", "collection") or (
            "fields" in data and data.get("kind") not in ("enum", "type")
        ):
            try:
                schema = parse_schema(text, source=where)
            except RecordError as exc:
                error(where, str(exc))
                continue
            if schema.id in records:
                error(where, f"duplicate record schema id {schema.id!r}")
                continue
            records[schema.id] = schema
            for lang in missing_languages(schema.title):
                warning(where, f"title has no {lang} text")
            continue
        try:
            declaration = solution.parse_type(data, where, file.stem)
        except solution.SolutionError as exc:
            error(where, str(exc))
            continue
        if declaration.id in types:
            error(where, f"duplicate type id {declaration.id!r}")
            continue
        types[declaration.id] = declaration
    return types, records


def _parse_constants_dir(directory: Path, error) -> dict:
    out: dict = {}
    for file in _toml_files(directory):
        where = f"constants/{file.name}"
        try:
            values = solution.parse_constants(file.read_text(encoding="utf-8"), where)
        except solution.SolutionError as exc:
            error(where, str(exc))
            continue
        for key in values:
            if key in out:
                error(where, f"constant {key!r} is defined twice")
        out.update(values)
    return out


def _parse_knowledge(directory: Path, error, warning) -> dict[str, SpaceDeclaration]:
    knowledge: dict[str, SpaceDeclaration] = {}
    for file in _toml_files(directory):
        where = f"knowledge/{file.name}"
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
            unknown = set(data) - {"id", "title", "description", "data_class"}
            if unknown:
                raise ValueError(f"unknown keys {sorted(unknown)}")
            sid = str(data.get("id", file.stem))
            if not re.match(r"^[a-z0-9][a-z0-9-]{1,63}$", sid):
                raise ValueError(f"invalid space id {sid!r}")
            rank(str(data.get("data_class", "internal")))
            decl = SpaceDeclaration(
                id=sid,
                title=normalize(data.get("title", sid), "title"),
                description=normalize(data["description"], "description")
                if data.get("description")
                else {},
                data_class=str(data.get("data_class", "internal")),
            )
        except (tomllib.TOMLDecodeError, ValueError, TextError, DataClassError) as exc:
            error(where, str(exc))
            continue
        if decl.id in knowledge:
            error(where, f"duplicate knowledge space id {decl.id!r} in bundle")
            continue
        knowledge[decl.id] = decl
        for lang in missing_languages(decl.title):
            warning(where, f"title has no {lang} text")
    return knowledge


def _check_stub_outcomes(templates, configurations, where: str, error) -> None:
    """A stubbed tool answers with a configured outcome; that outcome must
    exist on every step that calls the tool - a stub that silently takes
    some other path would prove nothing (review 2026-10-18)."""
    for name, configuration in configurations.items():
        for key, setting in configuration.tools.items():
            if setting.mode != "stub":
                continue
            for definition in templates.values():
                for step in definition.steps.values():
                    if step.kind is not StepKind.SYSTEM_ACTION:
                        continue
                    if key not in (step.config.get("tool"), step.config.get("action")):
                        continue
                    if setting.outcome not in step.next:
                        error(
                            f"{where}configurations/{name}.toml".lstrip("/"),
                            f"stub for {key!r} answers {setting.outcome!r}, but step"
                            f" {definition.id}/{step.id} has outcomes {sorted(step.next)}",
                        )


def _parse_configurations(directory: Path, error) -> dict[str, solution.Configuration]:
    out: dict[str, solution.Configuration] = {}
    for file in _toml_files(directory):
        where = f"configurations/{file.name}"
        try:
            configuration = solution.parse_configuration(
                file.read_text(encoding="utf-8"), where, file.stem
            )
        except solution.SolutionError as exc:
            error(where, str(exc))
            continue
        if configuration.name != file.stem:
            error(where, f"configuration name {configuration.name!r} must equal the file name")
            continue
        out[configuration.name] = configuration
    return out


def _parse_decisions(directory: Path, error, warning) -> dict:
    """DMN decision tables (ADR-0034 §2); the evaluator lives in dmn.py."""
    out: dict = {}
    if not directory.is_dir():
        return out
    from .dmn import DmnError, parse_dmn

    for file in sorted(directory.glob("*.dmn")):
        where = f"decisions/{file.name}"
        try:
            decision = parse_dmn(file.read_text(encoding="utf-8"), where)
        except DmnError as exc:
            error(where, str(exc))
            continue
        if decision.id != solution.subject_of(file):
            warning(where, f"decision id {decision.id!r} differs from file name")
        if decision.id in out:
            error(where, f"duplicate decision id {decision.id!r}")
            continue
        out[decision.id] = decision
    return out


_INPUT_PATH = re.compile(
    r"^(steps\.[a-z][a-z0-9_]*(\.[a-zA-Z0-9_]+)+|trigger\.[a-zA-Z0-9_.]+"
    r"|const:[a-z][a-z0-9_.]*|literal:.*|template:.*|each(\.[a-zA-Z0-9_]+)*|case\.(id|process))$",
    re.S,  # a literal or template text may span lines (a mail body)
)


def _check_knowledge_step(step, spaces: dict, where: str, error) -> None:
    """``knowledge`` files text into a declared space: the space must be in
    sight, ``inputs.text`` bound (``title`` and ``name`` optional)."""
    space = str(step.config.get("space", ""))
    if space not in spaces:
        error(where, f"step {step.id!r}: knowledge needs a declared space, got {space!r}")
    bound = step.config.get("inputs") or {}
    if not isinstance(bound, dict) or "text" not in bound:
        error(where, f"step {step.id!r}: knowledge needs inputs.text (a context path)")
        return
    for name, path in bound.items():
        if name not in ("text", "title", "name"):
            error(
                where, f"step {step.id!r}: knowledge knows inputs text, title, name - not {name!r}"
            )
        if not isinstance(path, str) or not _INPUT_PATH.match(path):
            error(where, f"step {step.id!r}: input {name!r} must be a context path")


def _check_record_step(step, records: dict, where: str, error) -> None:
    """``record`` writes into a declared collection: the collection must be
    in sight and every bound input must be one of its fields (the same
    allowlist discipline as for tools, ADR-0041 §2)."""
    collection = str(step.config.get("collection", ""))
    schema = records.get(collection)
    if schema is None:
        error(where, f"step {step.id!r}: record needs a collection in sight, got {collection!r}")
        return
    if getattr(schema, "kind", "") != "collection":
        error(where, f"step {step.id!r}: {collection!r} is not a collection")
    bound = step.config.get("inputs") or {}
    if not isinstance(bound, dict) or not bound:
        error(where, f"step {step.id!r}: record needs inputs (field -> path)")
        return
    for name, path in bound.items():
        if name not in schema.fields:
            error(where, f"step {step.id!r}: {collection!r} has no field {name!r}")
        if not isinstance(path, str) or not _INPUT_PATH.match(path):
            error(where, f"step {step.id!r}: input {name!r} must be a context path")
    for name, field_spec in schema.fields.items():
        if getattr(field_spec, "required", False) and name not in bound:
            error(where, f"step {step.id!r}: required field {name!r} of {collection!r} is unbound")


def _check_links_step(step, where: str, error) -> None:
    """``links`` reads addresses from a bound ``inputs.text`` or the case's
    document; ``max`` is a small positive number."""
    bound = step.config.get("inputs") or {}
    if not isinstance(bound, dict) or set(bound) - {"text"}:
        error(where, f"step {step.id!r}: links takes only inputs.text")
    elif "text" in bound and (
        not isinstance(bound["text"], str) or not _INPUT_PATH.match(bound["text"])
    ):
        error(where, f"step {step.id!r}: inputs.text must be a context path")
    limit = step.config.get("max", 5)
    if not isinstance(limit, int) or not 1 <= limit <= 20:
        error(where, f"step {step.id!r}: max must be 1..20")


def _check_rows_step(step, records: dict, where: str, error) -> None:
    """``rows`` reads a declared collection; ``where`` binds fields to
    context paths, ``limit`` is a small positive number."""
    collection = str(step.config.get("collection", ""))
    schema = records.get(collection)
    if schema is None or getattr(schema, "kind", "") != "collection":
        error(where, f"step {step.id!r}: rows needs a collection in sight, got {collection!r}")
        return
    bound = step.config.get("where") or {}
    if not isinstance(bound, dict):
        error(where, f"step {step.id!r}: where must be a table of field -> path")
        bound = {}
    for name, path in bound.items():
        if name not in schema.fields:
            error(where, f"step {step.id!r}: {collection!r} has no field {name!r}")
        if not isinstance(path, str) or not _INPUT_PATH.match(path):
            error(where, f"step {step.id!r}: where {name!r} must be a context path")
    limit = step.config.get("limit", 50)
    try:
        if not 1 <= int(limit) <= 500:
            raise ValueError
    except (TypeError, ValueError):
        error(where, f"step {step.id!r}: limit must be 1..500")


def _bind_tools(
    definition: ProcessDefinition, tools: dict[str, solution.ToolDeclaration], where: str, error
) -> None:
    """A serviceTask may name a declared tool instead of a core action; the
    runtime then sees the tool's action and effect (ADR-0035 §2)."""
    for step in definition.steps.values():
        if step.kind is not StepKind.SYSTEM_ACTION:
            continue
        action = str(step.config.get("action", ""))
        if action in tools:
            tool = tools[action]
            step.config["tool"] = tool.id
            step.config["effect"] = tool.effect
            if tool.exec == "client":
                step.config["exec"] = "client"
                step.config["action"] = tool.action
            elif tool.action:
                step.config["action"] = tool.action
            else:
                step.config["action"] = f"connector:{tool.connector}"
            step.config["on_error"] = tool.on_error
            if tool.on_error not in step.next and tool.effect == "external":
                error(
                    where,
                    f"step {step.id!r} calls tool {tool.id!r} but has no {tool.on_error!r}"
                    " outcome - every tool needs an error path",
                )
            # Inputs are an allowlist (ADR-0041 §2): only declared inputs may
            # be bound, each to a context path - never a whole model output.
            bound = step.config.get("inputs") or {}
            if not isinstance(bound, dict):
                error(where, f"step {step.id!r}: inputs must be a table of name -> path")
                bound = {}
            for name, path in bound.items():
                if name not in tool.inputs:
                    error(
                        where,
                        f"step {step.id!r} binds input {name!r} that tool {tool.id!r}"
                        " does not declare",
                    )
                if not isinstance(path, str) or not _INPUT_PATH.match(path):
                    error(
                        where,
                        f"step {step.id!r}: input {name!r} must be a path like"
                        " steps.<id>.<field>, trigger.<field> or const:<name>",
                    )
            missing = [n for n in tool.inputs if n not in bound]
            if missing and (tool.http is not None or tool.mcp is not None or tool.exec == "client"):
                error(
                    where,
                    f"step {step.id!r} leaves tool {tool.id!r} inputs {missing} unbound",
                )
        elif action and action not in INTERNAL_ACTIONS:
            if step.config.get("effect") in ("internal", "read"):
                error(
                    where,
                    f"step {step.id!r}: action {action!r} has an external effect - a process"
                    " file cannot declare it internal or read-only",
                )
            step.config["effect"] = "external"
            if action == "mail":
                # The core's own outbound mail (ADR-0048): bound like a
                # tool, with the fields the message cannot do without.
                bound = step.config.get("inputs") or {}
                if not isinstance(bound, dict):
                    error(where, f"step {step.id!r}: inputs must be a table of name -> path")
                    bound = {}
                for name, path in bound.items():
                    if name not in MAIL_INPUTS:
                        error(
                            where,
                            f"step {step.id!r}: mail has no input {name!r}"
                            f" (known: {', '.join(MAIL_INPUTS)})",
                        )
                    if not isinstance(path, str) or not _INPUT_PATH.match(path):
                        error(
                            where,
                            f"step {step.id!r}: input {name!r} must be a path like"
                            " steps.<id>.<field>",
                        )
                missing = [n for n in ("to", "subject", "body") if n not in bound]
                if missing:
                    error(where, f"step {step.id!r}: mail leaves {missing} unbound")
                if "failed" not in step.next:
                    error(
                        where,
                        f"step {step.id!r}: mail needs a 'failed' outcome - every outside effect"
                        " has an error path",
                    )
        elif action in INTERNAL_ACTIONS and step.config.get("effect") == "read":
            error(where, f"step {step.id!r}: 'read' is a connector effect, not one of {action!r}")
            step.config["effect"] = "internal"
        # A tool declared internal must also bind an internal core action.
        if step.config.get("tool") and step.config.get("effect") == "internal":
            bound = str(step.config.get("action", ""))
            if bound not in INTERNAL_ACTIONS:
                error(
                    where,
                    f"step {step.id!r}: tool {step.config['tool']!r} claims an internal effect"
                    f" but runs {bound!r}",
                )
    # The binding changed exec/action after the first validation; the
    # engine re-validates at load, so the validator must see the same.
    try:
        definition.validate()
    except DefinitionError as exc:
        error(where, str(exc))


def _parse_doctypes(directory: Path, templates, core_definitions, error, warning):
    doctypes: dict[str, Doctype] = {}
    for file in _toml_files(directory):
        where = f"doctypes/{file.name}"
        try:
            doctype = parse_doctype(file.read_text(encoding="utf-8"), source=where)
        except DoctypeError as exc:
            error(where, str(exc))
            continue
        if doctype.id in doctypes:
            error(where, f"duplicate document type id {doctype.id!r} in bundle")
            continue
        doctypes[doctype.id] = doctype
        if doctype.chain not in templates and doctype.chain not in core_definitions:
            error(where, f"chain {doctype.chain!r} is neither in this bundle nor in the core")
        for lang in missing_languages(doctype.titles):
            warning(where, f"title has no {lang} text")
    return doctypes


def _cross_checks(path, manifest, templates, core_definitions, knowledge, error, warning) -> None:
    known = set(templates) | set(core_definitions)
    for tid, definition in templates.items():
        for step in definition.steps.values():
            for target in step.next.values():
                if isinstance(target, str) and target.startswith("process:"):
                    successor = target.split(":", 1)[1]
                    if successor in known:
                        continue
                    if manifest.dependencies:
                        # Resolved against the declared dependencies at
                        # install time (load_apps); here it is a promise.
                        warning(
                            f"processes/{tid}",
                            f"step {step.id!r} routes to process {successor!r} that a"
                            " dependency must provide",
                        )
                    else:
                        error(
                            f"processes/{tid}",
                            f"step {step.id!r} routes to unknown process {successor!r}",
                        )
    try:
        declared_rank = rank(manifest.requires.data_class)
    except DataClassError as exc:
        error("solution.toml", f"requires.data_class: {exc}")
        declared_rank = None
    if declared_rank is not None:
        for tid, definition in templates.items():
            for step in definition.steps.values():
                if step.kind is not StepKind.AGENT_STEP:
                    continue
                try:
                    need = demand(step.config, definition.policies)
                except DataClassError as exc:
                    error(f"processes/{tid}", f"step {step.id!r}: {exc}")
                    continue
                if rank(need) > declared_rank:
                    error(
                        f"processes/{tid}",
                        f"step {step.id!r} needs data class {need!r} but the solution declares"
                        f" requires.data_class = {manifest.requires.data_class!r}",
                    )
                role = (step.config.get("briefing") or {}).get("model_role")
                if role and role in manifest.model_roles:
                    cap = manifest.model_roles[role].max_data_class
                    if rank(need) > rank(cap):
                        error(
                            f"processes/{tid}",
                            f"step {step.id!r} needs {need!r} but model role {role!r} allows"
                            f" at most {cap!r}",
                        )
    for name, setting in manifest.settings.items():
        for bind in setting.binds:
            match = _BIND_PATTERN.match(bind)
            if not match:
                error(f"settings.{name}", f"bind {bind!r} must look like '<process>:<parameter>'")
                continue
            tid, param = match.groups()
            if tid not in templates:
                error(f"settings.{name}", f"bind {bind!r}: process {tid!r} is not in this solution")
            elif param not in templates[tid].parameters:
                error(
                    f"settings.{name}",
                    f"bind {bind!r}: process {tid!r} declares no parameter {param!r}",
                )
    for tid, definition in templates.items():
        for step in definition.steps.values():
            # search words from an earlier step: the step must exist
            for ref in re.findall(r"\{steps\.([A-Za-z0-9_]+)",
                                  str(step.config.get("knowledge_query") or "")):
                if ref not in definition.steps or ref == step.id:
                    error(
                        f"processes/{tid}",
                        f"step {step.id!r}: knowledge_query names {{steps.{ref}…}},"
                        " which is no other step of the process",
                    )
            for sid in (step.config.get("context") or {}).get("knowledge") or []:
                if str(sid).startswith("{"):
                    field = str(sid)[1:-1] if str(sid).endswith("}") else ""
                    name = field.split(".", 1)[1] if field.startswith("trigger.") else ""
                    inputs = (definition.contract or {}).get("input") or {}
                    if not name or (inputs and name.split(".")[0] not in inputs):
                        error(
                            f"processes/{tid}",
                            f"step {step.id!r}: knowledge binding {sid!r} must be"
                            " {trigger.<field>} with a field of the contract",
                        )
                    elif not step.config.get("data_class"):
                        error(
                            f"processes/{tid}",
                            f"step {step.id!r}: a space chosen by the input needs a declared"
                            " data_class (its ceiling)",
                        )
                    continue
                if str(sid) not in knowledge:
                    warning(
                        f"processes/{tid}",
                        f"step {step.id!r} binds knowledge space {sid!r} that this solution does"
                        " not declare - the instance must provide it",
                    )


def _parse_sidecars(
    path: Path, root: Path, templates, briefings, skills, decisions, tools, configurations, error,
    foreign=None,
) -> tuple[solution.SidecarCase, ...]:
    """``<element>.test.toml`` next to each element (ADR-0035 §6)."""
    cases: list[solution.SidecarCase] = []
    plan = (
        ("processes", "process", templates, (".bpmn",)),
        ("ai", "ai", briefings, (".aimn.toml",)),
        ("decisions", "decision", decisions, (".dmn",)),
    )
    for folder, kind, subjects, _suffixes in plan:
        directory = path / folder
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.test.toml")):
            where = f"{folder}/{file.name}"
            subject = solution.subject_of(file)
            if subject not in subjects:
                error(where, f"sidecar has no {kind} {subject!r} next to it")
                continue
            try:
                parsed = solution.parse_sidecar(
                    file.read_text(encoding="utf-8"), where, kind, subject
                )
            except solution.SolutionError as exc:
                error(where, str(exc))
                continue
            for case in parsed:
                _check_sidecar(case, root, where, templates, configurations, error, foreign)
            cases.extend(parsed)
    skills_dir = path / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            file = child / "skill.test.toml"
            if not file.is_file():
                continue
            where = f"skills/{child.name}/skill.test.toml"
            if child.name not in skills:
                error(where, "sidecar has no skill next to it")
                continue
            try:
                parsed = solution.parse_sidecar(
                    file.read_text(encoding="utf-8"), where, "skill", child.name
                )
            except solution.SolutionError as exc:
                error(where, str(exc))
                continue
            for case in parsed:
                _check_sidecar(case, root, where, templates, configurations, error, foreign)
            cases.extend(parsed)
    return tuple(cases)


def _check_sidecar(
    case: solution.SidecarCase, root: Path, where: str, templates, configurations, error,
    foreign=None,
):
    """``foreign`` is the warning sink when the solution declares
    dependencies: a gate the decisions name may then live in one of them,
    which only the installed set can tell."""
    if case.config not in solution.RESERVED_CONFIGURATIONS and case.config not in configurations:
        error(where, f"case {case.id!r}: unknown configuration {case.config!r}")
    if case.subject_kind == "process":
        definition = templates[case.subject_id]
        outcomes = {o for step in definition.steps.values() for o in step.next}
        for key in ("path", "gates"):
            value = case.expect.get(key)
            if value is not None and (
                not isinstance(value, list) or any(v not in definition.steps for v in value)
            ):
                error(
                    where, f"case {case.id!r}: expect.{key} must list steps of {case.subject_id!r}"
                )
        for gate, outcome in case.decisions.items():
            # A decision may address a gate of this process or of any
            # process the chain continues into (the run drives the chain).
            gates = [
                t.steps[gate]
                for t in templates.values()
                if gate in t.steps and t.steps[gate].kind is StepKind.HUMAN_GATE
            ]
            if not gates:
                (foreign or error)(
                    where, f"case {case.id!r}: decisions.{gate} is no gate in this solution"
                )
            elif not any(outcome in g.next for g in gates):
                error(where, f"case {case.id!r}: {outcome!r} is no outcome of gate {gate!r}")
        steps_expect = case.expect.get("outcomes")
        if steps_expect is not None:
            if not isinstance(steps_expect, dict):
                error(where, f"case {case.id!r}: expect.outcomes must map steps to outcomes")
            else:
                for sid, outcome in steps_expect.items():
                    if sid not in definition.steps:
                        error(where, f"case {case.id!r}: expect.outcomes.{sid}: unknown step")
                    elif outcome not in definition.steps[sid].next:
                        error(where, f"case {case.id!r}: {outcome!r} is no outcome of {sid!r}")
        if "status" in case.expect and case.expect["status"] not in ("completed", "running"):
            error(where, f"case {case.id!r}: expect.status must be completed or running")
        if "outcome" in case.expect and case.expect["outcome"] not in outcomes:
            error(where, f"case {case.id!r}: expect.outcome {case.expect['outcome']!r} unknown")
        if not (("text" in case.given) ^ ("file" in case.given) ^ ("trigger" in case.given)):
            error(where, f"case {case.id!r}: given needs exactly one of text, file or trigger")
        _check_sample_ref(root, case, error, where)
    elif case.subject_kind in ("ai", "skill"):
        if not (("text" in case.given) ^ ("file" in case.given)):
            error(where, f"case {case.id!r}: given needs exactly one of text or file")
        _check_sample_ref(root, case, error, where)
        if not any(k in case.expect for k in ("fields", "outcome", "criteria")):
            error(where, f"case {case.id!r}: expect needs fields, outcome or criteria")
    elif case.subject_kind == "decision":
        if "inputs" not in case.given:
            error(where, f"case {case.id!r}: given.inputs is required for a decision")
        if "outputs" not in case.expect:
            error(where, f"case {case.id!r}: expect.outputs is required for a decision")
