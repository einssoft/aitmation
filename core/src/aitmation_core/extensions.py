"""Extension points: a solution docks its own process onto a process of
another solution.

A process may carry named extension points (a ``callActivity`` with
``<aimn:extensionPoint name="..." mode="..."/>`` and no ``calledElement``).
What runs there is not written in the process: it comes from the
solutions enabled in the instance. A solution declares it in
``extensions/<id>.toml``::

    extends  = "website-chat"      # the process that has the point
    point    = "topic"
    process  = "booking-offer"     # this solution's process that runs there
    priority = 50

    [returns]                      # the fields the point asks for, from this process' steps
    answer = "offer.text"          # or a list: the first path that holds a value wins,
                                   # e.g. one per branch: ["offer.answer", "none.answer"]

    [topic]                        # what the module is responsible for
    title    = { de = "Termin buchen", en = "Book an appointment" }
    describe = { de = "...", en = "..." }
    examples = { de = ["Wann ist der naechste Termin frei?"], en = ["..."] }
    keywords = { de = ["termin", "buch"], en = ["appointment", "book"] }

Mode ``takeover`` (the only one implemented so far; ``before`` and
``after`` are reserved): the docked process answers *instead of* the
host's own way. Who takes over is decided in two stages with different
jobs:

1. **Pre-filter, no model.** It never decides that a module takes over -
   only whether one *comes into question*. A topic is a candidate when a
   keyword matches the text (as the beginning of a word, so ``buch`` finds
   ``buchen`` and ``Buchung``) or when the previous turn of the same
   conversation was handled by it. No candidate: the host goes its own
   way, without any model call.
2. **Decision, one short model call** - only when there are candidates.
   The model sees the text, the recent turns and each candidate's title,
   description and examples, and names exactly one topic or none.

So a keyword alone never triggers a module, and the model is asked only
where a module is likely.
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field

MODES = ("takeover", "before", "after")
IMPLEMENTED_MODES = ("takeover",)
NONE = "none"  # the router's answer, and the point's outcome, when no module takes over
_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
_WORD = re.compile(r"[a-z0-9]+")
_KEYS = frozenset({"extends", "point", "process", "priority", "topic", "returns"})
_RETURN_PATH = re.compile(r"^[a-z][a-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_TOPIC_KEYS = frozenset({"title", "describe", "examples", "keywords"})
MAX_KEYWORDS = 40
MAX_EXAMPLES = 12


class ExtensionError(ValueError):
    pass


@dataclass(frozen=True)
class Topic:
    """What a takeover module is responsible for, per language."""

    title: dict[str, str] = field(default_factory=dict)
    describe: dict[str, str] = field(default_factory=dict)
    examples: dict[str, tuple[str, ...]] = field(default_factory=dict)
    keywords: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def all_keywords(self) -> tuple[str, ...]:
        """Keywords of every language, folded - a visitor's language is
        not known for sure, and a keyword is only a hint anyway."""
        seen: dict[str, None] = {}
        for words in self.keywords.values():
            for word in words:
                folded = fold(word)
                if folded:
                    seen.setdefault(folded)
        return tuple(seen)


@dataclass(frozen=True)
class Extension:
    id: str
    extends: str  # the process that carries the point
    point: str
    process: str  # the docked process
    priority: int = 50
    topic: Topic | None = None
    # field the point asks for -> "<step>.<path>" in the docked process, or
    # several such paths of which the first that holds a value counts
    returns: dict[str, str | tuple[str, ...]] = field(default_factory=dict)
    app: str = ""  # the solution that declares it; set when the instance loads it


def fold(text: str) -> str:
    """Lower case without accents: ``Réservation`` and ``reservation`` are
    one word to the pre-filter; ``ä`` and ``ae`` are too."""
    text = text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    return _WORD.findall(fold(text))


def _texts(raw: object, where: str, key: str) -> dict[str, str]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {"de": raw}
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        raise ExtensionError(f"{where}: topic.{key} is a text or a table of texts per language")
    return {str(lang): text for lang, text in raw.items()}


def _lists(raw: object, where: str, key: str, limit: int) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if isinstance(raw, list):
        raw = {"de": raw}
    if not isinstance(raw, dict):
        raise ExtensionError(f"{where}: topic.{key} is a list or a table of lists per language")
    out: dict[str, tuple[str, ...]] = {}
    for lang, items in raw.items():
        if not isinstance(items, list) or not all(isinstance(i, str) and i.strip() for i in items):
            raise ExtensionError(f"{where}: topic.{key}.{lang} is a list of non-empty texts")
        if len(items) > limit:
            raise ExtensionError(f"{where}: topic.{key}.{lang} has more than {limit} entries")
        out[str(lang)] = tuple(i.strip() for i in items)
    return out


def parse_extension(text: str, where: str, extension_id: str) -> Extension:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ExtensionError(f"{where}: {exc}") from exc
    unknown = set(raw) - _KEYS
    if unknown:
        raise ExtensionError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    if not _ID.match(extension_id):
        raise ExtensionError(f"{where}: the file name {extension_id!r} is not a valid id")
    values = {}
    for key in ("extends", "point", "process"):
        value = raw.get(key)
        if not isinstance(value, str) or not _ID.match(value):
            raise ExtensionError(f"{where}: {key} names a process or point (lower case id)")
        values[key] = value
    priority = raw.get("priority", 50)
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 1000:
        raise ExtensionError(f"{where}: priority is a number from 0 to 1000")
    topic = None
    if "topic" in raw:
        table = raw["topic"]
        if not isinstance(table, dict) or set(table) - _TOPIC_KEYS:
            raise ExtensionError(
                f"{where}: [topic] takes {', '.join(sorted(_TOPIC_KEYS))} and nothing else"
            )
        topic = Topic(
            title=_texts(table.get("title"), where, "title"),
            describe=_texts(table.get("describe"), where, "describe"),
            examples=_lists(table.get("examples"), where, "examples", MAX_EXAMPLES),
            keywords=_lists(table.get("keywords"), where, "keywords", MAX_KEYWORDS),
        )
        if not topic.title or not topic.describe:
            raise ExtensionError(f"{where}: [topic] needs a title and a description")
        if not topic.all_keywords():
            raise ExtensionError(
                f"{where}: [topic] needs keywords - without them the module never comes"
                " into question"
            )
        short = [k for k in topic.all_keywords() if len(k) < 3]
        if short:
            raise ExtensionError(
                f"{where}: keyword(s) {', '.join(short)} are shorter than three letters"
            )
    raw_returns = raw.get("returns", {})
    if not isinstance(raw_returns, dict):
        raise ExtensionError(f'{where}: [returns] is a table of field = "<step>.<path>"')
    returns: dict[str, str | tuple[str, ...]] = {}
    for name, path in raw_returns.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(name)):
            raise ExtensionError(f"{where}: returns.{name} is not a field name")
        paths = path if isinstance(path, list) else [path]
        if not paths or not all(isinstance(p, str) and _RETURN_PATH.match(p) for p in paths):
            raise ExtensionError(
                f'{where}: returns.{name} is a path like "<step>.<field>" or a list of them'
            )
        returns[str(name)] = paths[0] if isinstance(path, str) else tuple(paths)
    return Extension(
        id=extension_id, priority=priority, topic=topic, returns=returns, **values
    )


def return_paths(path: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """The candidate paths of one returned field, in order."""
    return (path,) if isinstance(path, str) else tuple(path)


def check_against_point(
    extension: Extension, point: dict, docked_steps: set[str], where: str
) -> None:
    """A declaration has to fit the point it docks to: the mode, and every
    field the point's process reads afterwards. ``point`` is the config of
    the step that carries the point, ``docked_steps`` the step ids of the
    docked process."""
    mode = str(point.get("mode", ""))
    if mode not in IMPLEMENTED_MODES:
        raise ExtensionError(f"{where}: extension points of mode {mode!r} are not supported yet")
    if mode == "takeover" and extension.topic is None:
        raise ExtensionError(f"{where}: docking to a takeover point needs a [topic]")
    missing = [name for name in point.get("returns") or [] if name not in extension.returns]
    if missing:
        raise ExtensionError(
            f"{where}: the point asks for {', '.join(missing)} - name the step that delivers"
            " it under [returns]"
        )
    for name, path in extension.returns.items():
        for candidate in return_paths(path):
            if candidate.split(".", 1)[0] not in docked_steps:
                raise ExtensionError(
                    f"{where}: returns.{name} reads step {candidate.split('.', 1)[0]!r}, which"
                    f" process {extension.process!r} does not have"
                )


@dataclass(frozen=True)
class Candidate:
    extension: Extension
    reason: str  # "keyword:<word>" or "conversation"


def candidates(extensions: list[Extension], text: str, last: str = "") -> list[Candidate]:
    """Stage 1: which topics come into question for this text. ``last`` is
    the id of the extension that handled the previous turn of the same
    conversation, if any - a follow-up ("and on Thursday?") carries no
    keyword of its own."""
    tokens = words(text)
    out: list[Candidate] = []
    for extension in sorted(extensions, key=lambda e: (-e.priority, e.id)):
        if extension.topic is None:
            continue
        hit = next(
            (k for k in extension.topic.all_keywords() if any(t.startswith(k) for t in tokens)),
            None,
        )
        if hit:
            out.append(Candidate(extension, f"keyword:{hit}"))
        elif last and last == extension.id:
            out.append(Candidate(extension, "conversation"))
    return out


def _pick(texts: dict, lang: str):
    return texts.get(lang) or texts.get("en") or texts.get("de") or next(iter(texts.values()), "")


def router_task(found: list[Candidate], lang: str = "en") -> str:
    """Stage 2: the instruction of the deciding model call. Deterministic
    for a given set of candidates, so a recorded answer replays."""
    lines = [
        "A visitor wrote the message under `text` (earlier turns under `history`, if any).",
        "Decide whether it is a request for exactly one of the topics below - judged by what"
        " the visitor wants, not by single words. A message that merely mentions a word of a"
        " topic but asks for something else belongs to none.",
        "Topics:",
    ]
    for candidate in found:
        topic = candidate.extension.topic
        assert topic is not None
        examples = "; ".join(list(_pick(topic.examples, lang) or ())[:3])
        title, describe = _pick(topic.title, lang), _pick(topic.describe, lang)
        line = f"- {candidate.extension.id}: {title}. {describe}"
        lines.append(line + (f" Examples: {examples}" if examples else ""))
    lines.append(
        f'Answer with {{"topic": "<topic id>", "confidence": 0.0-1.0}}. If none fits, use "{NONE}".'
    )
    return "\n".join(lines)


ROUTER_OUTPUT = {"topic": "string", "confidence": "number"}
