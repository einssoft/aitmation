"""Parse XML that comes from outside: process and decision files of
installed or imported solutions, office documents, sitemaps.

The standard parser does not fetch external entities, but a document may
still declare entities of its own that expand to very large text. Nothing
this system reads needs a document type declaration, so a document that
carries one is refused instead of parsed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

_MARKERS = ("<!DOCTYPE", "<!ENTITY")


def fromstring(data: str | bytes) -> ET.Element:
    """Like ``ElementTree.fromstring``; a document with a DOCTYPE or an
    entity declaration raises ``ParseError`` like any malformed document."""
    probe = data if isinstance(data, str) else data.replace(b"\x00", b"").decode("latin-1")
    if any(marker in probe for marker in _MARKERS):
        raise ET.ParseError("document type and entity declarations are not accepted")
    return ET.fromstring(data)  # noqa: S314 - declarations are refused above
