"""Multilingual text fields (ADR-0022).

Human-readable fields in declarations - app names, document type titles,
setting labels - are either a plain string (which counts as German, the
lead language) or a table keyed by language code::

    name = "Dokumente lesen"
    name = { de = "Dokumente lesen", fr = "Lire les documents", en = "Read documents" }

The core stores the normalized table and picks the requested language
with a fallback to German. Missing languages are a validation *warning*,
never a load error - an app may start monolingual and is then visibly
incomplete (ADR-0022, consequences).
"""

from __future__ import annotations

LANGUAGES: tuple[str, ...] = ("de", "fr", "it", "en")
DEFAULT_LANGUAGE = "de"

Text = str | dict[str, str]


class TextError(ValueError):
    """A multilingual field has an invalid shape."""


def normalize(value: object, field: str = "text") -> dict[str, str]:
    """Turn a plain string or a language table into ``{lang: text}``.

    A plain string is German. A table must have string keys and values
    and must contain the lead language.
    """
    if isinstance(value, str):
        return {DEFAULT_LANGUAGE: value}
    if isinstance(value, dict):
        if not value:
            raise TextError(f"{field}: empty language table")
        for lang, text in value.items():
            if not isinstance(lang, str) or not isinstance(text, str):
                raise TextError(f"{field}: language table needs string keys and values")
            if lang not in LANGUAGES:
                raise TextError(
                    f"{field}: unknown language {lang!r} (known: {', '.join(LANGUAGES)})"
                )
        if DEFAULT_LANGUAGE not in value:
            raise TextError(f"{field}: lead language {DEFAULT_LANGUAGE!r} is required")
        return dict(value)
    raise TextError(f"{field}: expected a string or a language table, got {type(value).__name__}")


def pick(text: Text | dict[str, str], lang: str = DEFAULT_LANGUAGE) -> str:
    """The text in ``lang``, falling back to German, then to anything."""
    if isinstance(text, str):
        return text
    if lang in text:
        return text[lang]
    if DEFAULT_LANGUAGE in text:
        return text[DEFAULT_LANGUAGE]
    return next(iter(text.values()), "")


def missing_languages(text: dict[str, str]) -> tuple[str, ...]:
    """Languages of the first tier that the table does not cover."""
    return tuple(lang for lang in LANGUAGES if lang not in text)
