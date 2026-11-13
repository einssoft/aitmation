"""Web sources of knowledge spaces (ADR-0045).

A space can keep itself up to date from a website: a *source* names a
start address, a scope (just this page, this path, the whole site), the
document types to take (web pages, PDF, Word, Excel, ...), limits and an
update interval. The crawler walks the site within those bounds and hands
back what it found; :class:`WebSources` files each page or file as a
document of the space - a new version only when the content changed - and
retires documents whose page is gone.

What the crawler does *not* do, on purpose:

- leave the site: links to other hosts are never followed; files linked
  from in-scope pages may live anywhere on the *same* site (``/fileadmin/``
  next to ``/rathaus/``), never elsewhere;
- ignore the site's wishes: ``robots.txt`` and ``<meta name="robots">``
  are honoured, requests are spaced (``delay``) and identify themselves;
- run code: no JavaScript, no forms, no logins, no query-string
  enumeration beyond the links the pages carry;
- reach inside the house: the host policy of the tool connectors applies
  to every request and every redirect hop (ADR-0041) - private and local
  addresses only with ``tools_allow_private_hosts``.

Content from the web is foreign text (ADR-0038): it lands in a space as
data; the guardrails of every model step that reads it stay in force.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from .connectors import WebReader, host_policy
from .formats import html_page, normalize_url

log = logging.getLogger("aitmation.knowledge.web")

USER_AGENT = "aitmation-knowledge/1 (+https://aitmation.com/crawler)"
ROBOTS_AGENT = "aitmation-knowledge"

SCOPES = ("page", "path", "site")

DOC_TYPES: dict[str, dict] = {
    "html": {"ext": {".html", ".htm", ".xhtml"}, "mime": ("text/html", "application/xhtml+xml")},
    "pdf": {"ext": {".pdf"}, "mime": ("application/pdf",)},
    "docx": {
        "ext": {".docx"},
        "mime": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
    },
    "xlsx": {
        "ext": {".xlsx"},
        "mime": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
    },
    "pptx": {
        "ext": {".pptx"},
        "mime": ("application/vnd.openxmlformats-officedocument.presentationml.presentation",),
    },
    "odt": {"ext": {".odt"}, "mime": ("application/vnd.oasis.opendocument.text",)},
    "ods": {"ext": {".ods"}, "mime": ("application/vnd.oasis.opendocument.spreadsheet",)},
    "odp": {"ext": {".odp"}, "mime": ("application/vnd.oasis.opendocument.presentation",)},
    "txt": {"ext": {".txt"}, "mime": ("text/plain",)},
    "md": {"ext": {".md", ".markdown"}, "mime": ("text/markdown", "text/x-markdown")},
    "csv": {"ext": {".csv"}, "mime": ("text/csv",)},
}
DEFAULT_TYPES = ("html", "pdf")

_EXT_TO_TYPE = {ext: kind for kind, spec in DOC_TYPES.items() for ext in spec["ext"]}
# Endings of dynamic pages: they are web pages, whatever the extension says.
_PAGE_EXT = {"", ".php", ".asp", ".aspx", ".jsp", ".cfm", ".shtml", ".phtml", ".do", ".action"}

MAX_PAGES = 2000
MAX_DEPTH = 10
MAX_INTERVAL_HOURS = 24 * 366
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_SITEMAPS = 10
MAX_SITEMAP_BYTES = 10 * 1024 * 1024


class SourceError(ValueError):
    """A source declaration the caller has to correct."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


# -- declaration ---------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    url: str
    scope: str = "path"
    doc_types: tuple[str, ...] = DEFAULT_TYPES
    max_pages: int = 200
    max_depth: int = 3
    exclude: tuple[str, ...] = ()


def make_spec(
    url: str,
    *,
    scope: str = "path",
    doc_types: list[str] | tuple[str, ...] | None = None,
    max_pages: int = 200,
    max_depth: int = 3,
    exclude: list[str] | tuple[str, ...] | None = None,
    allow_private: bool = False,
) -> SourceSpec:
    """Validated source declaration (raises :class:`SourceError`)."""
    raw = (url or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    start = normalize_url(raw)
    if not start:
        raise SourceError("not a web address (http/https)")
    refused = host_policy(start, allow_private=allow_private)
    if refused:
        raise SourceError(refused)
    if scope not in SCOPES:
        raise SourceError(f"scope must be one of {', '.join(SCOPES)}")
    kinds = tuple(dict.fromkeys(str(t).strip().lower() for t in (doc_types or DEFAULT_TYPES)))
    unknown = [k for k in kinds if k not in DOC_TYPES]
    if unknown:
        raise SourceError(f"unknown document type(s): {', '.join(unknown)}")
    if not kinds:
        raise SourceError("at least one document type")
    if not 1 <= int(max_pages) <= MAX_PAGES:
        raise SourceError(f"max_pages must be between 1 and {MAX_PAGES}")
    if not 0 <= int(max_depth) <= MAX_DEPTH:
        raise SourceError(f"max_depth must be between 0 and {MAX_DEPTH}")
    patterns = tuple(p.strip() for p in (exclude or ()) if p and p.strip())
    if len(patterns) > 50 or any(len(p) > 300 for p in patterns):
        raise SourceError("at most 50 exclude patterns of 300 characters")
    return SourceSpec(start, scope, kinds, int(max_pages), int(max_depth), patterns)


# -- crawling ------------------------------------------------------------------


@dataclass
class Fetched:
    url: str
    kind: str  # one of DOC_TYPES
    content: bytes
    title: str = ""
    text: str | None = None  # readable text of a web page
    depth: int = 0


@dataclass
class CrawlResult:
    items: list[Fetched] = field(default_factory=list)
    gone: set[str] = field(default_factory=set)
    """Addresses that answered 404/410 - a document of them is retired."""
    kept: set[str] = field(default_factory=set)
    """Addresses that failed for a passing reason (5xx, timeout, too
    large): their documents stay as they are."""
    errors: list[tuple[str, str]] = field(default_factory=list)
    skipped: int = 0
    complete: bool = True
    """The frontier ran empty within the limits - only then may documents
    that were not seen again be retired."""
    start_failed: str | None = None
    requests: int = 0


def _site_of(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _extension(url: str) -> str:
    return PurePosixPath(unquote(urlsplit(url).path)).suffix.lower()


def _kind_of_mime(content_type: str) -> str | None:
    mime = content_type.split(";", 1)[0].strip().lower()
    for kind, spec in DOC_TYPES.items():
        if mime in spec["mime"]:
            return kind
    return None


class Crawler:
    """Breadth-first walk of one site within a :class:`SourceSpec`."""

    def __init__(
        self,
        spec: SourceSpec,
        *,
        transport=None,
        allow_private: bool = False,
        delay: float = 0.5,
        timeout_seconds: int = 20,
        sleep: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.spec = spec
        self.transport = transport
        self.allow_private = allow_private
        self.delay = max(0.0, float(delay))
        self.timeout = timeout_seconds
        self.sleep = sleep
        self.should_stop = should_stop or (lambda: False)
        parts = urlsplit(spec.url)
        self.site = _site_of(parts.hostname or "")
        path = parts.path or "/"
        self.prefix = path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"
        self.robots: RobotFileParser | None = None
        self._last_request = 0.0
        self._reader: WebReader | None = None

    # scope ---------------------------------------------------------------

    def same_site(self, url: str) -> bool:
        return _site_of(urlsplit(url).hostname or "") == self.site

    def page_in_scope(self, url: str) -> bool:
        if not self.same_site(url):
            return False
        if self.spec.scope == "page":
            return url == self.spec.url
        if self.spec.scope == "path":
            return (urlsplit(url).path or "/").startswith(self.prefix)
        return True

    def excluded(self, url: str) -> bool:
        for pattern in self.spec.exclude:
            if any(c in pattern for c in "*?["):
                if fnmatch.fnmatch(url, pattern) or fnmatch.fnmatch(urlsplit(url).path, pattern):
                    return True
            elif pattern in url:
                return True
        return False

    def allowed_by_robots(self, url: str) -> bool:
        return self.robots is None or self.robots.can_fetch(ROBOTS_AGENT, url)

    # network -------------------------------------------------------------

    def _pause(self) -> None:
        if self.delay and self._last_request:
            wait = self.delay - (time.monotonic() - self._last_request)
            if wait > 0:
                self.sleep(wait)
        self._last_request = time.monotonic()

    def _get(
        self, url: str, limit: int, accept: Callable[[str, str], bool] | None = None
    ) -> tuple[int, str, str, bytes | None]:
        assert self._reader is not None
        try:
            return self._reader.get(url, limit, accept=accept, follow=self.same_site)
        finally:
            self.result.requests = self._reader.requests

    def _load_robots(self) -> list[str]:
        """Read robots.txt of the start host; returns the sitemaps it
        names. A missing or unreadable robots.txt allows everything."""
        parts = urlsplit(self.spec.url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            status, _, _, body = self._get(robots_url, 512 * 1024)
        except ValueError:
            return []
        if status != 200 or not body:
            return []
        lines = body.decode("utf-8", errors="replace").splitlines()
        parser = RobotFileParser()
        parser.parse(lines)
        self.robots = parser
        return [
            line.split(":", 1)[1].strip()
            for line in lines
            if line.lower().startswith("sitemap:")
        ]

    def _sitemap_urls(self, named: list[str]) -> list[str]:
        parts = urlsplit(self.spec.url)
        queue = deque(named or [f"{parts.scheme}://{parts.netloc}/sitemap.xml"])
        seen: set[str] = set()
        found: list[str] = []
        while queue and len(seen) < MAX_SITEMAPS:
            url = normalize_url(queue.popleft())
            if not url or url in seen or not self.same_site(url):
                continue
            seen.add(url)
            try:
                status, _, _, body = self._get(url, MAX_SITEMAP_BYTES)
            except ValueError:
                continue
            if status != 200 or not body:
                continue
            try:
                root = ElementTree.fromstring(body)
            except ElementTree.ParseError:
                continue
            tag = root.tag.rsplit("}", 1)[-1]
            locs = [
                (el.text or "").strip()
                for el in root.iter()
                if el.tag.rsplit("}", 1)[-1] == "loc" and el.text
            ]
            if tag == "sitemapindex":
                queue.extend(locs)
            else:
                found.extend(locs)
        return found

    # walk ----------------------------------------------------------------

    def run(self) -> CrawlResult:
        self.result = CrawlResult()
        with WebReader(
            user_agent=USER_AGENT, transport=self.transport, allow_private=self.allow_private,
            timeout_seconds=self.timeout, max_redirects=MAX_REDIRECTS,
            before_request=self._pause, normalize=normalize_url,
        ) as reader:
            self._reader = reader
            try:
                self._walk()
            finally:
                self._reader = None
        strip_boilerplate(self.result.items)
        return self.result

    def _walk(self) -> None:
        result = self.result
        spec = self.spec
        wanted = set(spec.doc_types)
        named = self._load_robots()
        queue: deque[tuple[str, int]] = deque([(spec.url, 0)])
        queued: set[str] = {spec.url}
        if spec.scope != "page" and spec.max_depth > 0:
            for loc in self._sitemap_urls(named):
                url = normalize_url(loc)
                if url and url not in queued and self.page_in_scope(url):
                    queued.add(url)
                    queue.append((url, 1))
        texts: set[str] = set()
        stored = 0
        budget = spec.max_pages * 3 + 20  # requests for pages that end up not stored
        while queue:
            if self.should_stop():
                result.complete = False
                result.errors.append(("", "stopped"))
                return
            if stored >= spec.max_pages or result.requests >= budget:
                result.complete = False
                return
            url, depth = queue.popleft()
            if self.excluded(url) or not self.allowed_by_robots(url):
                result.skipped += 1
                continue
            ext = _extension(url)
            guess = _EXT_TO_TYPE.get(ext)
            if guess is None and ext not in _PAGE_EXT:
                result.skipped += 1  # images, archives, media, scripts ...
                continue
            if guess is not None and guess != "html" and guess not in wanted:
                result.skipped += 1
                continue

            def accept(final: str, content_type: str) -> bool:
                kind = _kind_of_mime(content_type) or _EXT_TO_TYPE.get(_extension(final))
                return kind == "html" or kind in wanted

            is_page = guess in (None, "html")
            try:
                status, final, content_type, body = self._get(
                    url, MAX_HTML_BYTES if is_page else MAX_FILE_BYTES, accept
                )
            except ValueError as exc:
                result.errors.append((url, str(exc)))
                result.kept.add(url)
                if url == spec.url:
                    result.start_failed = str(exc)
                continue
            if status in (404, 410):
                result.gone.add(url)
                if url == spec.url:
                    result.start_failed = f"HTTP {status}"
                continue
            if status != 200:
                result.errors.append((url, f"HTTP {status}"))
                result.kept.add(url)
                if url == spec.url:
                    result.start_failed = f"HTTP {status}"
                continue
            if body is None:
                result.skipped += 1
                continue
            if final != url:
                if final in queued and final != spec.url:
                    continue
                queued.add(final)
            kind = _kind_of_mime(content_type) or _EXT_TO_TYPE.get(_extension(final)) or "html"
            if kind != "html":
                if kind in wanted:
                    result.items.append(
                        Fetched(final, kind, body, title=_title_of_file(final), depth=depth)
                    )
                    stored += 1
                continue
            page = html_page(body, final)
            if (not page.noindex) and "html" in wanted and page.text:
                digest = hashlib.sha256(page.text.encode()).hexdigest()
                if digest not in texts:  # the same page under a second address
                    texts.add(digest)
                    result.items.append(
                        Fetched(final, "html", body, page.title, page.text, depth)
                    )
                    stored += 1
            if page.nofollow:
                continue
            for link in page.links:
                if link in queued:
                    continue
                link_ext = _extension(link)
                link_kind = _EXT_TO_TYPE.get(link_ext)
                is_file = link_kind not in (None, "html")
                if is_file:
                    # Files hang off in-scope pages: same site, any path,
                    # one step further - also with scope "page".
                    if not self.same_site(link) or link_kind not in wanted:
                        continue
                elif depth >= spec.max_depth or not self.page_in_scope(link):
                    continue
                queued.add(link)
                queue.append((link, depth + 1))


def _title_of_file(url: str) -> str:
    name = PurePosixPath(unquote(urlsplit(url).path)).name
    stem = PurePosixPath(name).stem or urlsplit(url).hostname or "document"
    return re.sub(r"[_]+", " ", stem).strip() or "document"


def strip_boilerplate(items: list[Fetched], *, min_pages: int = 5, share: float = 0.5) -> int:
    """Drop the lines that recur on most pages of a site (menus that
    slipped through, the footer's address block, cookie notes): with at
    least ``min_pages`` web pages, a short line on more than ``share`` of
    them is chrome, not content. Returns the number of lines dropped."""
    pages = [i for i in items if i.kind == "html" and i.text]
    if len(pages) < min_pages:
        return 0
    counts: Counter[str] = Counter()
    for page in pages:
        counts.update({line for line in page.text.splitlines() if len(line) <= 200})  # type: ignore[union-attr]
    threshold = max(3, int(len(pages) * share) + 1)
    chrome = {line for line, n in counts.items() if n >= threshold and not line.startswith("#")}
    dropped = 0
    for page in pages:
        lines = page.text.splitlines()  # type: ignore[union-attr]
        kept = [line for line in lines if line not in chrome]
        dropped += len(lines) - len(kept)
        page.text = "\n".join(kept) if kept else page.text
    return dropped


# -- sources in the database -----------------------------------------------------


def _loads(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def _source_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "space": row["space_id"],
        "url": row["url"],
        "title": row["title"] or "",
        "scope": row["scope"],
        "doc_types": _loads(row["doc_types"], list(DEFAULT_TYPES)),
        "max_pages": int(row["max_pages"]),
        "max_depth": int(row["max_depth"]),
        "exclude": _loads(row["exclude"], []),
        "interval_hours": int(row["interval_hours"]),
        "enabled": bool(row["enabled"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_run_at": row["last_run_at"],
        "next_run_at": row["next_run_at"],
        "last_status": row["last_status"],
        "last_error": row["last_error"],
        "last_stats": _loads(row["last_stats"], None),
    }


def spec_of(view: dict, *, allow_private: bool = False) -> SourceSpec:
    return make_spec(
        view["url"], scope=view["scope"], doc_types=view["doc_types"],
        max_pages=view["max_pages"], max_depth=view["max_depth"], exclude=view["exclude"],
        allow_private=allow_private,
    )


def file_name_of(item: Fetched) -> str:
    """A readable, stable file name for a fetched page or file."""
    path = unquote(urlsplit(item.url).path).rstrip("/")
    name = PurePosixPath(path).name or (urlsplit(item.url).hostname or "index")
    if item.kind == "html":
        stem = PurePosixPath(name).stem if PurePosixPath(name).suffix.lower() in _PAGE_EXT | {
            ".html", ".htm", ".xhtml"} else name
        return f"{stem or 'index'}.md"
    return name


class WebSources:
    """The ``knowledge_sources`` table and the filing of crawl results."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # declarations ----------------------------------------------------------

    def list(self, space_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM knowledge_sources WHERE space_id = ? AND deleted_at IS NULL"
            " ORDER BY created_at",
            (space_id,),
        ).fetchall()
        out = []
        for row in rows:
            view = _source_view(row)
            view["documents"] = self._doc_count(row["id"])
            out.append(view)
        return out

    def _doc_count(self, source_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM knowledge_documents"
            " WHERE source_id = ? AND deleted_at IS NULL",
            (source_id,),
        ).fetchone()
        return int(row["n"] or 0)

    def get(self, source_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM knowledge_sources WHERE id = ? AND deleted_at IS NULL", (source_id,)
        ).fetchone()
        if row is None:
            return None
        view = _source_view(row)
        view["documents"] = self._doc_count(source_id)
        return view

    def save(
        self,
        space_id: str,
        spec: SourceSpec,
        *,
        title: str = "",
        interval_hours: int = 168,
        enabled: bool = True,
        actor: str = "",
        source_id: str | None = None,
    ) -> dict:
        if not 0 <= int(interval_hours) <= MAX_INTERVAL_HOURS:
            raise SourceError(f"interval_hours must be between 0 and {MAX_INTERVAL_HOURS}")
        now = _now()
        values = (
            spec.url, title.strip()[:200], spec.scope, json.dumps(list(spec.doc_types)),
            spec.max_pages, spec.max_depth, json.dumps(list(spec.exclude)), int(interval_hours),
            1 if enabled else 0,
        )
        if source_id is None:
            source_id = uuid.uuid4().hex[:12]
            # A new source runs at the next tick - the space fills up now,
            # not after the first interval.
            self.conn.execute(
                "INSERT INTO knowledge_sources (id, space_id, url, title, scope, doc_types,"
                " max_pages, max_depth, exclude, interval_hours, enabled, created_by,"
                " created_at, updated_at, next_run_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, space_id, *values, actor, _iso(now), _iso(now),
                 _iso(now) if enabled else None),
            )
        else:
            current = self.get(source_id)
            if current is None or current["space"] != space_id:
                raise SourceError(f"unknown source {source_id!r}")
            next_run = current["next_run_at"]
            if not enabled:
                next_run = None
            elif current["last_run_at"] is None:
                next_run = _iso(now)
            elif int(interval_hours) > 0:
                base = datetime.fromisoformat(current["last_run_at"])
                next_run = _iso(base + timedelta(hours=int(interval_hours)))
            else:
                next_run = None
            self.conn.execute(
                "UPDATE knowledge_sources SET url = ?, title = ?, scope = ?, doc_types = ?,"
                " max_pages = ?, max_depth = ?, exclude = ?, interval_hours = ?, enabled = ?,"
                " updated_at = ?, next_run_at = ? WHERE id = ?",
                (*values, _iso(now), next_run, source_id),
            )
        return self.get(source_id)  # type: ignore[return-value]

    def delete(self, source_id: str) -> list[str]:
        """Retire the source; returns the ids of its live documents (the
        caller removes them through the store)."""
        docs = [
            r["id"]
            for r in self.conn.execute(
                "SELECT id FROM knowledge_documents WHERE source_id = ? AND deleted_at IS NULL",
                (source_id,),
            )
        ]
        self.conn.execute(
            "UPDATE knowledge_sources SET deleted_at = ?, next_run_at = NULL WHERE id = ?",
            (_iso(_now()), source_id),
        )
        return docs

    # scheduling ------------------------------------------------------------

    def due(self, now: datetime | None = None) -> list[dict]:
        moment = _iso(now or _now())
        rows = self.conn.execute(
            "SELECT k.* FROM knowledge_sources k JOIN knowledge_spaces s ON s.id = k.space_id"
            " WHERE k.deleted_at IS NULL AND k.enabled = 1 AND s.archived_at IS NULL"
            " AND k.next_run_at IS NOT NULL AND k.next_run_at <= ?"
            " AND COALESCE(k.last_status, '') != 'running'"
            " ORDER BY k.next_run_at",
            (moment,),
        ).fetchall()
        return [_source_view(r) for r in rows]

    def mark_running(self, source_id: str) -> None:
        self.conn.execute(
            "UPDATE knowledge_sources SET last_status = 'running', last_error = NULL WHERE id = ?",
            (source_id,),
        )

    def reset_running(self) -> int:
        """After a restart nothing is running any more: a crawl that was
        cut off is retried at the next tick."""
        cur = self.conn.execute(
            "UPDATE knowledge_sources SET last_status = 'failed',"
            " last_error = 'interrupted by a restart', next_run_at = ?"
            " WHERE last_status = 'running' AND deleted_at IS NULL",
            (_iso(_now()),),
        )
        return cur.rowcount or 0

    def finish(self, source_id: str, status: str, stats: dict, error: str | None = None) -> None:
        view = self.get(source_id)
        if view is None:
            return
        now = _now()
        next_run = None
        if view["enabled"] and view["interval_hours"] > 0:
            next_run = _iso(now + timedelta(hours=view["interval_hours"]))
        self.conn.execute(
            "UPDATE knowledge_sources SET last_run_at = ?, next_run_at = ?, last_status = ?,"
            " last_error = ?, last_stats = ? WHERE id = ?",
            (_iso(now), next_run, status, (error or None) and error[:500],
             json.dumps(stats), source_id),
        )

    # filing ----------------------------------------------------------------

    def known(self, source_id: str) -> dict[str, dict]:
        """url -> {id, sha256, deleted} of the documents this source filed."""
        rows = self.conn.execute(
            "SELECT id, url, sha256, deleted_at FROM knowledge_documents WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        return {
            r["url"]: {"id": r["id"], "sha256": r["sha256"], "deleted": r["deleted_at"] is not None}
            for r in rows
            if r["url"]
        }


def content_of(item: Fetched) -> bytes:
    """What is stored as the document's original: the readable text of a
    web page (stable between visits, unlike the markup; the address lives
    in the document's ``url`` column), the file itself otherwise. The page
    title becomes the first heading unless the text already starts with
    one."""
    if item.kind == "html":
        text = item.text or ""
        head = ""
        if item.title and not text.lstrip().startswith("#"):
            head = f"# {item.title}\n\n"
        return (head + text + "\n").encode("utf-8")
    return item.content


def digest_of(item: Fetched) -> str:
    return hashlib.sha256(content_of(item)).hexdigest()
