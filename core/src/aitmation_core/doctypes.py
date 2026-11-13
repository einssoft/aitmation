"""Document type declarations: data of the template layer, not core logic.

A document type (docs/AGENTIK.md, ADR-0014) declares what the intake
classifier should recognize ("features") and, decisively, which chain a
recognized document is routed into ("chain"). Routing stays deterministic:
the dispatch action reads this declaration - the model only judges the
type, never the route.

The core knows nothing about invoices or bank statements; it only loads
these declarations. Adding a new category at runtime (the third exit of
the sort-out loop in docs/AGENTIK.md) is writing one more TOML file - the
admin UI does exactly that via :func:`save_doctype`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import DEFAULT_LANGUAGE, TextError, normalize, pick

_KEYS = {"id", "title", "description", "features", "chain", "fields"}
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class DoctypeError(ValueError):
    """The document type declaration is invalid."""


@dataclass(frozen=True)
class Doctype:
    id: str
    title: str
    chain: str
    description: str = ""
    features: tuple[str, ...] = ()
    fields: dict[str, str] = field(default_factory=dict)
    # Full language tables (ADR-0022); ``title``/``description`` hold the
    # German text so existing callers keep working.
    titles: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.titles:
            object.__setattr__(self, "titles", {DEFAULT_LANGUAGE: self.title})
        if not self.descriptions:
            object.__setattr__(self, "descriptions", {DEFAULT_LANGUAGE: self.description})

    def title_in(self, lang: str) -> str:
        return pick(self.titles or self.title, lang)

    def description_in(self, lang: str) -> str:
        return pick(self.descriptions or self.description, lang)

    def validate(self) -> None:
        if not _ID_PATTERN.match(self.id):
            raise DoctypeError(
                f"document type id {self.id!r} must match {_ID_PATTERN.pattern}"
            )
        if self.id == "unknown":
            raise DoctypeError("document type id 'unknown' is reserved for the classifier")
        if not self.title.strip():
            raise DoctypeError(f"document type {self.id!r} needs a non-empty title")
        if not self.chain.strip():
            raise DoctypeError(f"document type {self.id!r} needs a target chain (chain)")


def parse_doctype(text: str, source: str = "<string>") -> Doctype:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DoctypeError(f"{source}: invalid TOML: {exc}") from exc
    unknown = set(data) - _KEYS
    if unknown:
        raise DoctypeError(f"{source}: unknown keys {sorted(unknown)}")
    for key in ("id", "chain"):
        if not isinstance(data.get(key), str):
            raise DoctypeError(f"{source}: missing or non-string key {key!r}")
    if "title" not in data:
        raise DoctypeError(f"{source}: missing key 'title'")
    try:
        titles = normalize(data["title"], "title")
        descriptions = normalize(data.get("description", ""), "description")
    except TextError as exc:
        raise DoctypeError(f"{source}: {exc}") from exc
    features = data.get("features", [])
    if not isinstance(features, list) or not all(isinstance(m, str) for m in features):
        raise DoctypeError(f"{source}: features must be a list of strings")
    fields = data.get("fields", {})
    if not isinstance(fields, dict) or not all(isinstance(v, str) for v in fields.values()):
        raise DoctypeError(f"{source}: fields must be a table of string descriptions")
    doctype = Doctype(
        id=data["id"],
        title=titles[DEFAULT_LANGUAGE],
        chain=data["chain"],
        description=descriptions[DEFAULT_LANGUAGE],
        features=tuple(features),
        fields=dict(fields),
        titles=titles,
        descriptions=descriptions,
    )
    try:
        doctype.validate()
    except DoctypeError as exc:
        raise DoctypeError(f"{source}: {exc}") from exc
    return doctype


def load_doctypes(directory: str | Path) -> dict[str, Doctype]:
    """Load every ``*.toml`` declaration in ``directory``, keyed by id."""
    out: dict[str, Doctype] = {}
    for path in sorted(Path(directory).glob("*.toml")):
        doctype = parse_doctype(path.read_text(encoding="utf-8"), source=str(path))
        if doctype.id in out:
            raise DoctypeError(f"{path}: duplicate document type id {doctype.id!r}")
        out[doctype.id] = doctype
    return out


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_text(table: dict[str, str], fallback: str) -> str:
    """A multilingual field as TOML: plain string when only German exists,
    inline table otherwise (ADR-0022, additive format)."""
    filled = {k: v for k, v in table.items() if v and v.strip()}
    if not filled or set(filled) == {DEFAULT_LANGUAGE}:
        return _toml_string(filled.get(DEFAULT_LANGUAGE, fallback))
    return "{ " + ", ".join(f"{k} = {_toml_string(v)}" for k, v in sorted(filled.items())) + " }"


def save_doctype(directory: str | Path, doctype: Doctype) -> Path:
    """Write a declaration file (the instance's local bundle; UI and admin)."""
    doctype.validate()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Written by the aitmation UI (Einstellungen → Dokumenttypen).",
        f"id = {_toml_string(doctype.id)}",
        f"title = {_toml_text(doctype.titles, doctype.title)}",
        f"description = {_toml_text(doctype.descriptions, doctype.description)}",
        "features = [" + ", ".join(_toml_string(m) for m in doctype.features) + "]",
        f"chain = {_toml_string(doctype.chain)}",
    ]
    if doctype.fields:
        lines.append("")
        lines.append("[fields]")
        # Quoted keys: valid TOML for any field name, no sanitizing needed.
        lines.extend(
            f"{_toml_string(k)} = {_toml_string(v)}" for k, v in sorted(doctype.fields.items())
        )
    path = directory / f"{doctype.id}.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
