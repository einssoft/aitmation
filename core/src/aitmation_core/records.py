"""Declared records: structured master data the instance keeps about itself.

A *record schema* (ADR-0026) declares a named, single-instance dataset -
the company profile is the first one - as fields with types, labels in
four languages (ADR-0022), constraints and optional **variants** keyed by
one discriminator field (the country: a Swiss company has a UID and a
canton, a German one a Handelsregister number and a Bundesland). Schemas
are data of the app layer: they ship in bundles under ``records/`` or
live in the instance's own local bundle; the core knows nothing about
companies, only about fields.

Values live in the database (``records`` table, migration 0007), one row
per saved version - the current state is the latest version, and who
changed what when stays reconstructible for a revisor (principle 5).
Validation is structural only (required, type, choices, pattern); what a
UID *means* is the schema author's business.

Templates read records through the agent context (``context.records``
in the step config, K4) - a step sees the company's own name and VAT
number only when it says so.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .i18n import DEFAULT_LANGUAGE, TextError, normalize, pick

FIELD_TYPES = frozenset({"text", "choice", "bool", "number", "email"})
SCOPES = frozenset({"any", "company", "private"})

_SCHEMA_KEYS = frozenset(
    {
        "id", "title", "description", "scope", "kind", "label_field", "search_fields",
        "variant_field", "fields", "variants",
    }
)
KINDS = frozenset({"single", "collection"})
ROW_CONTEXT_LIMIT = 100  # rows an agent step may see from a collection
_FIELD_KEYS = frozenset(
    {"type", "label", "help", "required", "choices", "pattern", "example", "default", "group"}
)
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RecordError(ValueError):
    """A schema is invalid or values do not fit their schema."""


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    label: dict[str, str]
    help: dict[str, str] = field(default_factory=dict)
    required: bool = False
    choices: tuple[str, ...] = ()
    choice_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    pattern: str | None = None
    example: str = ""
    default: object = None
    group: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "label": self.label,
            "help": self.help,
            "required": self.required,
            "choices": list(self.choices),
            "choice_labels": self.choice_labels,
            "pattern": self.pattern,
            "example": self.example,
            "default": self.default,
            "group": self.group,
        }


@dataclass(frozen=True)
class RecordSchema:
    id: str
    title: dict[str, str]
    description: dict[str, str] = field(default_factory=dict)
    scope: str = "any"
    # "single": one record per instance (company profile); "collection":
    # many rows (contacts, accounts) - the ADR-0026 extension.
    kind: str = "single"
    label_field: str | None = None  # which field names a row
    search_fields: tuple[str, ...] = ()
    variant_field: str | None = None
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    # variant value -> additional/overriding fields (order preserved)
    variants: dict[str, dict[str, FieldSpec]] = field(default_factory=dict)

    def title_in(self, lang: str = DEFAULT_LANGUAGE) -> str:
        return pick(self.title, lang)

    def applies_to(self, instance_type: str) -> bool:
        return self.scope == "any" or self.scope == instance_type

    def resolve(self, values: dict | None) -> list[FieldSpec]:
        """The effective field list for the given values: the common
        fields, then the variant's fields for the discriminator's value
        (a variant may override a common field of the same name)."""
        out = dict(self.fields)
        if self.variant_field and values:
            variant = values.get(self.variant_field)
            for name, spec in self.variants.get(str(variant), {}).items():
                out[name] = spec
        return list(out.values())

    def validate_values(self, values: dict) -> dict:
        """Check and normalize values; unknown keys are dropped, missing
        required fields and malformed values raise :class:`RecordError`
        listing every problem at once."""
        if not isinstance(values, dict):
            raise RecordError("values must be an object")
        problems: list[str] = []
        clean: dict = {}
        for spec in self.resolve(values):
            raw = values.get(spec.name)
            empty = raw is None or (isinstance(raw, str) and not raw.strip())
            if empty:
                if spec.required:
                    problems.append(f"{spec.name}: required")
                continue
            try:
                clean[spec.name] = _coerce(spec, raw)
            except RecordError as exc:
                problems.append(str(exc))
        if problems:
            raise RecordError("; ".join(problems))
        return clean

    def label_of(self, values: dict) -> str:
        if self.label_field and values.get(self.label_field):
            return str(values[self.label_field])
        for spec in self.fields.values():
            if spec.type == "text" and values.get(spec.name):
                return str(values[spec.name])
        return ""

    def as_dict(self, values: dict | None = None) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "scope": self.scope,
            "kind": self.kind,
            "label_field": self.label_field,
            "search_fields": list(self.search_fields),
            "variant_field": self.variant_field,
            "variants": sorted(self.variants),
            "fields": [f.as_dict() for f in self.resolve(values)],
            # For a form that switches the variant live: the common fields
            # and each variant's additions, so the client resolves locally.
            "common_fields": [f.as_dict() for f in self.fields.values()],
            "variant_fields": {
                key: [f.as_dict() for f in specs.values()] for key, specs in self.variants.items()
            },
        }


def _coerce(spec: FieldSpec, raw: object) -> object:
    if spec.type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in {"true", "false", "1", "0", "yes", "no"}:
            return raw.lower() in {"true", "1", "yes"}
        raise RecordError(f"{spec.name}: expected true/false")
    if spec.type == "number":
        if isinstance(raw, bool):
            raise RecordError(f"{spec.name}: expected a number")
        if isinstance(raw, int | float):
            return raw
        try:
            text = str(raw).strip().replace("'", "").replace(" ", "")
            return float(text) if "." in text else int(text)
        except ValueError as exc:
            raise RecordError(f"{spec.name}: expected a number") from exc
    text = str(raw).strip()
    if spec.type == "choice":
        if text not in spec.choices:
            raise RecordError(f"{spec.name}: {text!r} is not one of {list(spec.choices)}")
        return text
    if spec.type == "email" and not _EMAIL_PATTERN.match(text):
        raise RecordError(f"{spec.name}: not an e-mail address")
    if spec.pattern and not re.fullmatch(spec.pattern, text):
        hint = f" (example: {spec.example})" if spec.example else ""
        raise RecordError(f"{spec.name}: {text!r} does not match the expected format{hint}")
    return text


# -- schema parsing ----------------------------------------------------------


def parse_schema(text: str, *, source: str = "<string>") -> RecordSchema:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RecordError(f"{source}: invalid TOML: {exc}") from exc
    unknown = set(data) - _SCHEMA_KEYS
    if unknown:
        raise RecordError(f"{source}: unknown keys {sorted(unknown)}")
    schema_id = data.get("id")
    if not isinstance(schema_id, str) or not _ID_PATTERN.match(schema_id):
        raise RecordError(f"{source}: id must match {_ID_PATTERN.pattern}")
    if "title" not in data:
        raise RecordError(f"{source}: title is required")
    try:
        title = normalize(data["title"], "title")
        description = normalize(data["description"], "description") if "description" in data else {}
    except TextError as exc:
        raise RecordError(f"{source}: {exc}") from exc
    scope = str(data.get("scope", "any"))
    if scope not in SCOPES:
        raise RecordError(f"{source}: scope {scope!r} must be one of {sorted(SCOPES)}")
    kind = str(data.get("kind", "single"))
    if kind not in KINDS:
        raise RecordError(f"{source}: kind {kind!r} must be one of {sorted(KINDS)}")
    fields = _parse_fields(data.get("fields", {}), f"{source} [fields]")
    label_field = data.get("label_field")
    if label_field is not None and label_field not in fields:
        raise RecordError(f"{source}: label_field {label_field!r} is not a declared field")
    search_fields = data.get("search_fields", [])
    if not isinstance(search_fields, list) or any(f not in fields for f in search_fields):
        raise RecordError(f"{source}: search_fields must list declared fields")
    variant_field = data.get("variant_field")
    variants: dict[str, dict[str, FieldSpec]] = {}
    if variant_field is not None:
        if variant_field not in fields:
            raise RecordError(f"{source}: variant_field {variant_field!r} is not a declared field")
        if fields[variant_field].type != "choice":
            raise RecordError(f"{source}: variant_field {variant_field!r} must be a choice field")
        raw_variants = data.get("variants", {})
        if not isinstance(raw_variants, dict):
            raise RecordError(f"{source}: variants must be a table")
        for key, spec in raw_variants.items():
            if key not in fields[variant_field].choices:
                raise RecordError(
                    f"{source}: variant {key!r} is not a choice of {variant_field!r}"
                )
            variants[key] = _parse_fields(spec, f"{source} [variants.{key}]")
    elif data.get("variants"):
        raise RecordError(f"{source}: variants need a variant_field")
    return RecordSchema(
        id=schema_id,
        title=title,
        description=description,
        scope=scope,
        kind=kind,
        label_field=label_field,
        search_fields=tuple(str(f) for f in search_fields),
        variant_field=variant_field,
        fields=fields,
        variants=variants,
    )


def _parse_fields(raw: object, where: str) -> dict[str, FieldSpec]:
    if not isinstance(raw, dict):
        raise RecordError(f"{where}: must be a table of tables")
    out: dict[str, FieldSpec] = {}
    for name, spec in raw.items():
        if not _NAME_PATTERN.match(str(name)):
            raise RecordError(f"{where}: field name {name!r} must match {_NAME_PATTERN.pattern}")
        if not isinstance(spec, dict):
            raise RecordError(f"{where}.{name}: must be a table")
        unknown = set(spec) - _FIELD_KEYS
        if unknown:
            raise RecordError(f"{where}.{name}: unknown keys {sorted(unknown)}")
        ftype = spec.get("type", "text")
        if ftype not in FIELD_TYPES:
            raise RecordError(
                f"{where}.{name}: type {ftype!r} must be one of {sorted(FIELD_TYPES)}"
            )
        if "label" not in spec:
            raise RecordError(f"{where}.{name}: label is required")
        try:
            label = normalize(spec["label"], "label")
            help_ = normalize(spec["help"], "help") if "help" in spec else {}
        except TextError as exc:
            raise RecordError(f"{where}.{name}: {exc}") from exc
        choices: tuple[str, ...] = ()
        choice_labels: dict[str, dict[str, str]] = {}
        if ftype == "choice":
            raw_choices = spec.get("choices")
            if not isinstance(raw_choices, list) or not raw_choices:
                raise RecordError(f"{where}.{name}: choice fields need a non-empty choices list")
            keys: list[str] = []
            for item in raw_choices:
                # Either a plain value or { value = "...", label = { de = ... } }.
                if isinstance(item, str):
                    keys.append(item)
                elif isinstance(item, dict) and isinstance(item.get("value"), str):
                    keys.append(item["value"])
                    if "label" in item:
                        try:
                            choice_labels[item["value"]] = normalize(item["label"], "label")
                        except TextError as exc:
                            raise RecordError(f"{where}.{name}: {exc}") from exc
                else:
                    raise RecordError(f"{where}.{name}: choices are strings or {{value, label}}")
            choices = tuple(keys)
        elif "choices" in spec:
            raise RecordError(f"{where}.{name}: choices only apply to choice fields")
        pattern = spec.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise RecordError(f"{where}.{name}: pattern must be a string")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise RecordError(f"{where}.{name}: invalid pattern: {exc}") from exc
        out[name] = FieldSpec(
            name=name,
            type=ftype,
            label=label,
            help=help_,
            required=bool(spec.get("required", False)),
            choices=choices,
            choice_labels=choice_labels,
            pattern=pattern,
            example=str(spec.get("example", "")),
            default=spec.get("default"),
            group=str(spec.get("group", "")),
        )
    return out


def load_schemas(directory: str | Path) -> dict[str, RecordSchema]:
    """Load every ``*.toml`` schema below ``directory``; duplicate ids fail."""
    directory = Path(directory)
    out: dict[str, RecordSchema] = {}
    if not directory.is_dir():
        return out
    for file in sorted(directory.glob("*.toml")):
        schema = parse_schema(file.read_text(encoding="utf-8"), source=str(file))
        if schema.id in out:
            raise RecordError(f"{file}: duplicate record schema id {schema.id!r}")
        out[schema.id] = schema
    return out


# -- storage -----------------------------------------------------------------


def current_values(conn: sqlite3.Connection, schema_id: str) -> dict | None:
    row = conn.execute(
        "SELECT values_json FROM records WHERE schema_id = ? ORDER BY version DESC LIMIT 1",
        (schema_id,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["values_json"])


def current_meta(conn: sqlite3.Connection, schema_id: str) -> dict | None:
    row = conn.execute(
        "SELECT version, saved_at, saved_by FROM records WHERE schema_id = ?"
        " ORDER BY version DESC LIMIT 1",
        (schema_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def save_values(
    conn: sqlite3.Connection, schema: RecordSchema, values: dict, actor_id: str
) -> dict:
    """Validate and append a new version. Returns the stored values."""
    clean = schema.validate_values(values)
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM records WHERE schema_id = ?", (schema.id,)
    ).fetchone()
    version = int(row["v"]) + 1
    conn.execute(
        "INSERT INTO records (schema_id, version, values_json, saved_at, saved_by)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            schema.id,
            version,
            json.dumps(clean, ensure_ascii=False, sort_keys=True),
            datetime.now(UTC).isoformat(timespec="seconds"),
            actor_id,
        ),
    )
    conn.commit()
    return clean


def history(conn: sqlite3.Connection, schema_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT version, saved_at, saved_by FROM records WHERE schema_id = ?"
        " ORDER BY version DESC LIMIT ?",
        (schema_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# -- collections (rows) -------------------------------------------------------

_CURRENT_ROWS = (
    "SELECT r.row_id, r.version, r.values_json, r.saved_at, r.saved_by, r.deleted"
    " FROM record_rows r JOIN (SELECT row_id, MAX(version) AS v FROM record_rows"
    " WHERE schema_id = ? GROUP BY row_id) m ON m.row_id = r.row_id AND m.v = r.version"
)


def list_rows(
    conn: sqlite3.Connection,
    schema: RecordSchema,
    *,
    query: str = "",
    limit: int = 500,
    include_deleted: bool = False,
) -> list[dict]:
    """Current rows of a collection, optionally filtered by a substring over
    the search fields (or every text field when none are declared)."""
    rows = conn.execute(_CURRENT_ROWS + " ORDER BY r.saved_at DESC", (schema.id,)).fetchall()
    needle = query.strip().lower()
    fields = schema.search_fields or tuple(
        spec.name for spec in schema.fields.values() if spec.type in ("text", "email")
    )
    out = []
    for row in rows:
        if row["deleted"] and not include_deleted:
            continue
        values = json.loads(row["values_json"])
        if needle and not any(needle in str(values.get(f, "")).lower() for f in fields):
            continue
        out.append(_row_view(schema, row, values))
        if len(out) >= limit:
            break
    return out


def get_row(conn: sqlite3.Connection, schema: RecordSchema, row_id: str) -> dict | None:
    row = conn.execute(
        "SELECT row_id, version, values_json, saved_at, saved_by, deleted FROM record_rows"
        " WHERE schema_id = ? AND row_id = ? ORDER BY version DESC LIMIT 1",
        (schema.id, row_id),
    ).fetchone()
    if row is None:
        return None
    return _row_view(schema, row, json.loads(row["values_json"]))


def _row_view(schema: RecordSchema, row, values: dict) -> dict:
    return {
        "row_id": row["row_id"],
        "version": row["version"],
        "label": schema.label_of(values),
        "values": values,
        "saved_at": row["saved_at"],
        "saved_by": row["saved_by"],
        "deleted": bool(row["deleted"]),
    }


def save_row(
    conn: sqlite3.Connection,
    schema: RecordSchema,
    values: dict,
    actor_id: str,
    row_id: str | None = None,
) -> dict:
    """Create (row_id None) or change a row: always a new version."""
    import uuid

    clean = schema.validate_values(values)
    row_id = row_id or uuid.uuid4().hex
    version = int(
        conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM record_rows WHERE row_id = ?", (row_id,)
        ).fetchone()["v"]
    ) + 1
    conn.execute(
        "INSERT INTO record_rows (schema_id, row_id, version, values_json, saved_at, saved_by,"
        " deleted) VALUES (?, ?, ?, ?, ?, ?, 0)",
        (
            schema.id,
            row_id,
            version,
            json.dumps(clean, ensure_ascii=False, sort_keys=True),
            datetime.now(UTC).isoformat(timespec="seconds"),
            actor_id,
        ),
    )
    conn.commit()
    return get_row(conn, schema, row_id) or {}


def delete_row(conn: sqlite3.Connection, schema: RecordSchema, row_id: str, actor_id: str) -> bool:
    """Soft delete: a final version flagged deleted; history stays."""
    current = get_row(conn, schema, row_id)
    if current is None or current["deleted"]:
        return False
    conn.execute(
        "INSERT INTO record_rows (schema_id, row_id, version, values_json, saved_at, saved_by,"
        " deleted) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (
            schema.id,
            row_id,
            current["version"] + 1,
            json.dumps(current["values"], ensure_ascii=False, sort_keys=True),
            datetime.now(UTC).isoformat(timespec="seconds"),
            actor_id,
        ),
    )
    conn.commit()
    return True


def row_history(conn: sqlite3.Connection, row_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT version, saved_at, saved_by, deleted, values_json FROM record_rows"
        " WHERE row_id = ? ORDER BY version DESC LIMIT ?",
        (row_id, limit),
    ).fetchall()
    return [
        {**{k: r[k] for k in ("version", "saved_at", "saved_by")}, "deleted": bool(r["deleted"]),
         "values": json.loads(r["values_json"])}
        for r in rows
    ]


def row_count(conn: sqlite3.Connection, schema_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM (" + _CURRENT_ROWS + ") WHERE deleted = 0", (schema_id,)
    ).fetchone()
    return int(row["n"])


def completeness(schema: RecordSchema, values: dict | None) -> dict:
    """How far a record is filled in: required fields present vs. total."""
    specs = schema.resolve(values)
    required = [s for s in specs if s.required]
    present = [s for s in required if values.get(s.name) not in (None, "")] if values else []
    return {
        "required": len(required),
        "filled": len(present),
        "complete": len(present) == len(required) and values is not None,
    }
