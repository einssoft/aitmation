"""Text out of web pages and office files (ADR-0045), standard library only.

The knowledge spaces read PDFs, scans and recordings through
:mod:`aitmation_core.document`; this module adds what a web source brings
along: HTML pages and the office formats municipalities, associations and
firms publish next to them - Word, Excel, PowerPoint (OOXML) and the
OpenDocument family. All of them are ZIP containers of XML; reading the
text needs no third-party package (ADR-0009 guardrail: nothing copyleft,
nothing speculative).

Defensive by construction: ZIP members are read with a size cap (a
"zip bomb" stops at :data:`MAX_UNPACKED_BYTES`), XML goes through the
standard parser, which neither resolves external entities nor fetches
anything, and only the members that carry text are opened.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

MAX_UNPACKED_BYTES = 50 * 1024 * 1024
"""Upper bound for all text-carrying members of one office file together."""

HTML_TYPES = {".html", ".htm", ".xhtml"}
OOXML_TYPES = {".docx", ".xlsx", ".pptx"}
ODF_TYPES = {".odt", ".ods", ".odp"}
OFFICE_TYPES = OOXML_TYPES | ODF_TYPES


class FormatError(ValueError):
    """The file is not what its name says, or carries no readable text."""


# -- HTML ---------------------------------------------------------------------


@dataclass
class HtmlPage:
    title: str
    text: str
    links: list[str] = field(default_factory=list)
    """Absolute http(s) links of the page (``<a href>``), fragments removed,
    in document order and without duplicates."""
    noindex: bool = False
    """``<meta name="robots" content="noindex">`` - the site asks not to be
    indexed; the crawler keeps following the links (unless ``nofollow``)."""
    nofollow: bool = False


class _HtmlReader(HTMLParser):
    """Readable text of a page: page chrome (navigation, header, footer,
    forms, scripts) is dropped, headings are marked so a passage keeps its
    context, and when the page has a ``<main>`` (or a single ``<article>``)
    only that is kept."""

    _SKIP = frozenset({
        "script", "style", "noscript", "svg", "template", "head", "nav", "footer",
        "header", "aside", "form", "button", "select", "iframe", "object",
    })
    _VOID = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr",
    })
    _BLOCK = frozenset({
        "p", "div", "br", "li", "tr", "section", "article", "main", "blockquote", "pre",
        "table", "ul", "ol", "dl", "dt", "dd", "figcaption", "address", "hr",
    })
    _HEADINGS: ClassVar[dict[str, str]] = {
        "h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "#### ", "h6": "#### ",
    }

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.links: list[str] = []
        self._seen: set[str] = set()
        self.all_parts: list[str] = []
        self.main_parts: list[str] = []
        self._skip = 0
        self._skip_stack: list[str] = []
        self._pre = 0
        self._main = 0
        self._in_title = False
        self.has_main = False
        self.noindex = False
        self.nofollow = False

    def _emit(self, text: str) -> None:
        self.all_parts.append(text)
        if self._main:
            self.main_parts.append(text)

    def handle_starttag(self, tag, attrs):
        attr = {k: (v or "") for k, v in attrs}
        if tag == "base" and attr.get("href"):
            self.base_url = urljoin(self.base_url, attr["href"])
        if tag == "meta" and attr.get("name", "").lower() == "robots":
            content = attr.get("content", "").lower()
            self.noindex = self.noindex or "noindex" in content or "none" in content
            self.nofollow = self.nofollow or "nofollow" in content or "none" in content
        if tag == "a" and attr.get("href"):
            rel = attr.get("rel", "").lower()
            link = normalize_url(urljoin(self.base_url, attr["href"].strip()))
            if link and "nofollow" not in rel and link not in self._seen:
                self._seen.add(link)
                self.links.append(link)
        if tag == "title":
            self._in_title = True
        if tag in self._VOID:
            if tag in self._BLOCK:
                self._emit("\n")
            return
        if tag in self._SKIP and not (tag == "header" and self._main):
            # A header inside <main> is the article's own (title, date).
            self._skip_stack.append(tag)
            self._skip += 1
        if tag == "main":
            self._main += 1
            self.has_main = True
        if tag == "pre":
            self._pre += 1
        if tag in self._HEADINGS and not self._skip:
            self._emit("\n" + self._HEADINGS[tag])
        elif tag in self._BLOCK:
            self._emit("\n")
        elif tag in ("td", "th"):
            self._emit(" | ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self._VOID:
            return
        if self._skip_stack and self._skip_stack[-1] == tag:
            self._skip_stack.pop()
            self._skip -= 1
        if tag == "pre" and self._pre:
            self._pre -= 1
        if (tag == "main") and self._main:
            self._main -= 1
        if tag in self._BLOCK or tag in self._HEADINGS:
            self._emit("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            # Line breaks in the markup are no line breaks on the page.
            self._emit(data if self._pre else re.sub(r"\s+", " ", data))


def _tidy(raw: str) -> str:
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    out: list[str] = []
    for line in lines:
        line = line.strip(" |")
        if re.fullmatch(r"#+", line):
            continue
        if line:
            out.append(line)
    return "\n".join(out)


def html_page(content: bytes | str, url: str = "") -> HtmlPage:
    """Title, readable text and links of one HTML page."""
    if isinstance(content, bytes):
        content = decode_html(content)
    reader = _HtmlReader(url)
    reader.feed(content)
    reader.close()
    main = _tidy("".join(reader.main_parts)) if reader.has_main else ""
    text = main if len(main) >= 200 else _tidy("".join(reader.all_parts))
    title = " ".join(reader.title.split())
    return HtmlPage(title, text, reader.links, reader.noindex, reader.nofollow)


def decode_html(content: bytes) -> str:
    """Bytes of a page to text: BOM, then ``<meta charset>``, then UTF-8,
    with Latin-1 as the never-failing last resort."""
    if content.startswith(b"\xef\xbb\xbf"):
        return content[3:].decode("utf-8", errors="replace")
    head = content[:2048].decode("ascii", errors="ignore")
    match = re.search(r"charset=[\"']?([A-Za-z0-9_-]+)", head, re.I)
    if match:
        try:
            return content.decode(match.group(1), errors="replace")
        except LookupError:
            pass
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def normalize_url(url: str) -> str:
    """The comparable form of a link: http(s) only, scheme and host lower
    case, fragment dropped, default port dropped, empty path as ``/``.
    Returns "" for anything that is not a web address (mailto:, tel:,
    javascript:)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname.lower().rstrip(".")
    port = parts.port if parts.port and parts.port not in (80, 443) else None
    netloc = f"{host}:{port}" if port else host
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


# -- office files ---------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_TABLE_NS = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


class _Reader:
    """Size-capped access to the members of one ZIP container."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self.budget = MAX_UNPACKED_BYTES

    def names(self) -> list[str]:
        return self.archive.namelist()

    def xml(self, name: str) -> ElementTree.Element | None:
        try:
            info = self.archive.getinfo(name)
        except KeyError:
            return None
        if info.file_size > self.budget:
            raise FormatError("office file unpacks to more than the allowed size")
        self.budget -= info.file_size
        with self.archive.open(info) as handle:
            data = handle.read(info.file_size + 1)
        if len(data) > info.file_size:
            raise FormatError("office file member is larger than declared")
        try:
            return ElementTree.fromstring(data)
        except ElementTree.ParseError as exc:
            raise FormatError(f"{name}: not well-formed XML") from exc


def _natural(name: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name))


def _docx(reader: _Reader) -> str:
    out: list[str] = []
    parts = [
        "word/document.xml",
        *sorted(n for n in reader.names() if re.fullmatch(r"word/(footnotes|endnotes)\.xml", n)),
    ]
    for part in parts:
        root = reader.xml(part)
        if root is None:
            continue
        for para in root.iter(f"{_W}p"):
            style = para.find(f"{_W}pPr/{_W}pStyle")
            level = ""
            if style is not None:
                value = style.get(f"{_W}val", "")
                heading = re.fullmatch(r"(?i)(heading|berschrift|titre|titolo)\s*(\d)", value)
                if heading:
                    level = "#" * min(int(heading.group(2)), 4) + " "
                elif value.lower() == "title":
                    level = "# "
            pieces = []
            for node in para.iter():
                if node.tag == f"{_W}t" and node.text:
                    pieces.append(node.text)
                elif node.tag == f"{_W}tab":
                    pieces.append("\t")
                elif node.tag in (f"{_W}br", f"{_W}cr"):
                    pieces.append("\n")
            line = "".join(pieces).strip()
            if line:
                out.append(level + line)
    return "\n\n".join(out)


def _xlsx(reader: _Reader) -> str:
    shared: list[str] = []
    root = reader.xml("xl/sharedStrings.xml")
    if root is not None:
        for item in root.iter(f"{_S}si"):
            shared.append("".join(t.text or "" for t in item.iter(f"{_S}t")))
    names: dict[str, str] = {}
    book = reader.xml("xl/workbook.xml")
    rels = reader.xml("xl/_rels/workbook.xml.rels")
    if book is not None and rels is not None:
        targets = {r.get("Id"): r.get("Target", "") for r in rels}
        for sheet in book.iter(f"{_S}sheet"):
            rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = targets.get(rid, "").lstrip("/")
            target = target if target.startswith("xl/") else f"xl/{target}"
            names[target] = sheet.get("name", "")
    sheets = sorted(
        (n for n in reader.names() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
        key=_natural,
    )
    out: list[str] = []
    for part in sheets:
        root = reader.xml(part)
        if root is None:
            continue
        rows = []
        for row in root.iter(f"{_S}row"):
            cells = []
            for cell in row.iter(f"{_S}c"):
                kind = cell.get("t", "")
                value = cell.find(f"{_S}v")
                if kind == "s" and value is not None and (value.text or "").isdigit():
                    index = int(value.text or 0)
                    cells.append(shared[index] if index < len(shared) else "")
                elif kind == "inlineStr":
                    cells.append("".join(t.text or "" for t in cell.iter(f"{_S}t")))
                elif value is not None and value.text:
                    cells.append(value.text)
            if any(c.strip() for c in cells):
                rows.append(" | ".join(c.strip() for c in cells))
        if rows:
            title = names.get(part) or Path(part).stem
            out.append(f"## {title}\n" + "\n".join(rows))
    return "\n\n".join(out)


def _pptx(reader: _Reader) -> str:
    slides = sorted(
        (n for n in reader.names() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=_natural,
    )
    out: list[str] = []
    for number, part in enumerate(slides, 1):
        root = reader.xml(part)
        if root is None:
            continue
        paras = []
        for para in root.iter(f"{_A}p"):
            line = "".join(t.text or "" for t in para.iter(f"{_A}t")).strip()
            if line:
                paras.append(line)
        if paras:
            out.append(f"## Folie {number}\n" + "\n".join(paras))
    return "\n\n".join(out)


def _odf(reader: _Reader) -> str:
    root = reader.xml("content.xml")
    if root is None:
        raise FormatError("no content.xml - not an OpenDocument file")
    out: list[str] = []

    def text_of(node: ElementTree.Element) -> str:
        pieces = [node.text or ""]
        for child in node:
            if child.tag == f"{_TEXT_NS}s":
                pieces.append(" " * int(child.get(f"{_TEXT_NS}c", "1") or 1))
            elif child.tag == f"{_TEXT_NS}tab":
                pieces.append("\t")
            elif child.tag == f"{_TEXT_NS}line-break":
                pieces.append("\n")
            else:
                pieces.append(text_of(child))
            pieces.append(child.tail or "")
        return "".join(pieces)

    for node in root.iter():
        if node.tag == f"{_TEXT_NS}h":
            level = int(node.get(f"{_TEXT_NS}outline-level", "1") or 1)
            line = text_of(node).strip()
            if line:
                out.append("#" * min(level, 4) + " " + line)
        elif node.tag == f"{_TEXT_NS}p":
            line = text_of(node).strip()
            if line:
                out.append(line)
        elif node.tag == f"{_TABLE_NS}table" and node.get(f"{_TABLE_NS}name"):
            out.append(f"## {node.get(f'{_TABLE_NS}name')}")
    return "\n".join(out)


def office_text(path: str | Path) -> str:
    """Readable text of a .docx/.xlsx/.pptx/.odt/.ods/.odp file."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in OFFICE_TYPES:
        raise FormatError(f"{path.name}: not an office file")
    try:
        with zipfile.ZipFile(path) as archive:
            reader = _Reader(archive)
            if suffix == ".docx":
                text = _docx(reader)
            elif suffix == ".xlsx":
                text = _xlsx(reader)
            elif suffix == ".pptx":
                text = _pptx(reader)
            else:
                text = _odf(reader)
    except zipfile.BadZipFile as exc:
        # Old binary formats (.doc/.xls) and mislabeled files land here.
        raise FormatError(f"{path.name}: not a {suffix} file (no ZIP container)") from exc
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise FormatError(f"{path.name}: no readable text")
    return text
