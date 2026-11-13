"""Web sources of knowledge spaces (ADR-0045): office/HTML extraction,
source declarations, the crawler's scope/robots/type rules, filing with
change detection and retirement, the scheduler hook and the API."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aitmation_core.config import Settings
from aitmation_core.document import DocumentError, create_pdf, extract_text
from aitmation_core.formats import FormatError, html_page, normalize_url, office_text
from aitmation_core.knowledge_web import (
    Crawler,
    Fetched,
    SourceError,
    make_spec,
    strip_boilerplate,
)
from aitmation_core.runtime import Runtime
from aitmation_core.web import create_app

# -- office files built by hand (the formats are ZIPs of XML) -----------------

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx_bytes(paragraphs: list[tuple[str, str]]) -> bytes:
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
        if style
        else f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        for style, text in paragraphs
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")
    return buf.getvalue()


def xlsx_bytes(rows: list[list[str]]) -> bytes:
    strings: list[str] = []
    xml_rows = []
    for r, row in enumerate(rows, 1):
        cells = []
        for value in row:
            if value.replace(".", "").isdigit():
                cells.append(f"<c><v>{value}</v></c>")
            else:
                strings.append(value)
                cells.append(f'<c t="s"><v>{len(strings) - 1}</v></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "xl/sharedStrings.xml",
            f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>",
        )
        z.writestr(
            "xl/workbook.xml",
            f'<workbook {ns} {rel}><sheets><sheet name="Gebühren" sheetId="1" r:id="rId1"/>'
            "</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {ns}><sheetData>{''.join(xml_rows)}</sheetData></worksheet>",
        )
    return buf.getvalue()


def pptx_bytes(slides: list[list[str]]) -> bytes:
    ns = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, lines in enumerate(slides, 1):
            paras = "".join(f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in lines)
            z.writestr(f"ppt/slides/slide{i}.xml", f"<sld {ns}><a:txBody>{paras}</a:txBody></sld>")
    return buf.getvalue()


def odt_bytes(heading: str, paragraphs: list[str]) -> bytes:
    ns = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    body = f'<text:h text:outline-level="1">{heading}</text:h>' + "".join(
        f"<text:p>{p}</text:p>" for p in paragraphs
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr(
            "content.xml",
            f"<office:document-content {ns} xmlns:office='o'>"
            f"<office:body>{body}</office:body></office:document-content>",
        )
    return buf.getvalue()


def test_office_formats(tmp_path):
    (tmp_path / "a.docx").write_bytes(
        docx_bytes([("Heading1", "Abfallkalender"), ("", "Biotonne jeden Dienstag.")])
    )
    assert office_text(tmp_path / "a.docx") == "# Abfallkalender\n\nBiotonne jeden Dienstag."
    (tmp_path / "b.xlsx").write_bytes(xlsx_bytes([["Leistung", "CHF"], ["Pass", "65.00"]]))
    text = office_text(tmp_path / "b.xlsx")
    assert text.startswith("## Gebühren") and "Pass | 65.00" in text
    (tmp_path / "c.pptx").write_bytes(pptx_bytes([["Willkommen"], ["Zweite", "Folie"]]))
    assert "## Folie 2\nZweite\nFolie" in office_text(tmp_path / "c.pptx")
    (tmp_path / "d.odt").write_bytes(odt_bytes("Satzung", ["§ 1 Name", "§ 2 Zweck"]))
    assert office_text(tmp_path / "d.odt") == "# Satzung\n§ 1 Name\n§ 2 Zweck"
    # The document reader dispatches by suffix; an old binary .doc renamed
    # to .docx is a readable error, not a crash.
    assert extract_text(tmp_path / "a.docx").method == "office:docx"
    (tmp_path / "bad.docx").write_bytes(b"\xd0\xcf\x11\xe0 not a zip")
    with pytest.raises(FormatError, match="no ZIP"):
        office_text(tmp_path / "bad.docx")
    with pytest.raises(DocumentError):
        extract_text(tmp_path / "bad.docx")


def test_html_reading_drops_chrome_and_finds_links():
    raw = (
        """<html><head><title>Öffnungszeiten - Gemeinde</title>
    <script>var x = 1;</script></head><body>
    <header><nav><a href="/">Start</a><a href="/rathaus/">Rathaus</a></nav></header>
    <main><h1>Öffnungszeiten</h1><p>Das Rathaus ist Montag bis Freitag
    von 8 bis 12 Uhr geöffnet.</p><p>Dienstag zusätzlich 14 bis 18 Uhr.</p>
    <p>"""
        + "Weitere Hinweise. " * 12
        + """</p>
    <a href="gebuehren.pdf#seite2">Gebühren (PDF)</a>
    <a href="mailto:info@example.org">Mail</a><a href="https://Other.EXAMPLE:443/x">x</a></main>
    <footer>Rathausstraße 1</footer></body></html>"""
    )
    page = html_page(raw.encode("utf-8"), "https://gemeinde.example/rathaus/zeiten")
    assert page.title == "Öffnungszeiten - Gemeinde"
    assert page.text.startswith("# Öffnungszeiten\nDas Rathaus ist Montag bis Freitag von 8")
    assert (
        "Start" not in page.text and "Rathausstraße" not in page.text and "var x" not in page.text
    )
    assert "https://gemeinde.example/rathaus/gebuehren.pdf" in page.links
    assert "https://other.example/x" in page.links
    assert not any(link.startswith("mailto") for link in page.links)
    assert normalize_url("javascript:alert(1)") == ""
    assert normalize_url("HTTPS://Gemeinde.Example") == "https://gemeinde.example/"


def test_source_declarations_are_validated():
    spec = make_spec("gemeinde.example/rathaus/", doc_types=["HTML", "pdf", "docx"])
    assert spec.url == "https://gemeinde.example/rathaus/" and spec.doc_types == (
        "html",
        "pdf",
        "docx",
    )
    with pytest.raises(SourceError, match="plain http"):
        make_spec("http://gemeinde.example/")
    with pytest.raises(SourceError, match="private"):
        make_spec("https://192.168.1.10/wiki/")
    assert make_spec("http://intranet.local/", allow_private=True).url == "http://intranet.local/"
    with pytest.raises(SourceError, match="unknown document type"):
        make_spec("https://gemeinde.example/", doc_types=["exe"])
    with pytest.raises(SourceError, match="scope"):
        make_spec("https://gemeinde.example/", scope="world")
    with pytest.raises(SourceError, match="max_pages"):
        make_spec("https://gemeinde.example/", max_pages=0)


# -- a small municipality website ---------------------------------------------

NAV = '<header><nav><a href="/rathaus/">Rathaus</a><a href="/blog/">Blog</a></nav></header>'
FOOTER_LINE = "Gemeinde Musterhausen, Rathausstraße 1, Telefon 012 345 67 89"


def page(title: str, body: str, links: str = "") -> bytes:
    # The footer line sits inside <main>, so only the boilerplate pass
    # can remove it.
    return (
        f"<html><head><title>{title}</title></head><body>{NAV}<main><h1>{title}</h1>"
        f"<p>{body}</p>{links}<p>{FOOTER_LINE}</p></main></body></html>"
    ).encode()


class Site:
    """Mock web server; ``pages`` may be changed between crawls."""

    def __init__(self, tmp_path: Path) -> None:
        pdf = create_pdf(
            tmp_path / "gebuehren.pdf",
            "Gebührenordnung",
            [
                "Reisepass für Erwachsene: 70 Euro.",
                "Personalausweis: 37 Euro.",
            ],
        )
        index_links = (
            '<a href="/rathaus/oeffnungszeiten">Zeiten</a>'
            '<a href="/rathaus/abfall">Abfall</a><a href="standesamt">Standesamt</a>'
            '<a href="/fileadmin/gebuehren.pdf">Gebühren</a>'
            '<a href="/fileadmin/formular.docx">Formular</a>'
            '<a href="/fileadmin/plan.xlsx">Plan</a>'
            '<a href="/intern/geheim">intern</a><a href="https://other.example/x">fremd</a>'
            '<a href="/bilder/rathaus.jpg">Bild</a><a href="/rathaus/umzug">Umzug</a>'
            '<a href="/rathaus/kaputt">kaputt</a><a href="/rathaus/schule">Schule</a>'
        )
        self.pages: dict[str, tuple[int, str, bytes] | tuple[int, str]] = {
            "/robots.txt": (
                200,
                "text/plain",
                (
                    b"User-agent: *\nDisallow: /intern/\n"
                    b"Sitemap: https://gemeinde.example/sitemap.xml\n"
                ),
            ),
            "/sitemap.xml": (
                200,
                "application/xml",
                (
                    b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    b"<url><loc>https://gemeinde.example/rathaus/steuern</loc></url>"
                    b"<url><loc>https://gemeinde.example/blog/fest</loc></url>"
                    b"</urlset>"
                ),
            ),
            "/rathaus/": (
                200,
                "text/html; charset=utf-8",
                page("Rathaus", "Willkommen im Rathaus der Gemeinde.", index_links),
            ),
            "/rathaus/oeffnungszeiten": (
                200,
                "text/html",
                page("Öffnungszeiten", "Montag bis Freitag 8 bis 12 Uhr."),
            ),
            "/rathaus/abfall": (
                200,
                "text/html",
                page("Abfall", "Die Biotonne wird dienstags geleert."),
            ),
            "/rathaus/standesamt": (
                200,
                "text/html",
                page("Standesamt", "Trauungen nach Vereinbarung."),
            ),
            "/rathaus/steuern": (
                200,
                "text/html",
                page("Steuern", "Die Hundesteuer beträgt 120 Euro."),
            ),
            "/rathaus/schule": (
                200,
                "text/html",
                page("Schule", "Die Grundschule liegt am Kirchplatz."),
            ),
            "/blog/fest": (200, "text/html", page("Fest", "Dorffest im Juli.")),
            "/fileadmin/gebuehren.pdf": (200, "application/pdf", pdf.read_bytes()),
            "/fileadmin/formular.docx": (
                200,
                "application/octet-stream",
                docx_bytes([("", "Antrag auf Sperrmüllabholung.")]),
            ),
            "/fileadmin/plan.xlsx": (
                200,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                xlsx_bytes([["a"]]),
            ),
            "/intern/geheim": (200, "text/html", page("Geheim", "nicht für Crawler")),
            "/rathaus/umzug": (301, "https://other.example/umzug"),
            "/rathaus/kaputt": (503, "text/plain", b"down"),
        }
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("aitmation-knowledge/")
        url = str(request.url)
        self.requests.append(url)
        if request.url.host != "gemeinde.example":
            return httpx.Response(200, text="foreign")
        entry = self.pages.get(request.url.path)
        if entry is None:
            return httpx.Response(404, text="not found")
        if len(entry) == 2:
            return httpx.Response(entry[0], headers={"location": entry[1]})
        status, kind, body = entry
        return httpx.Response(status, headers={"content-type": kind}, content=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def test_crawler_scope_robots_types_and_boilerplate(tmp_path):
    site = Site(tmp_path)
    spec = make_spec("https://gemeinde.example/rathaus/", doc_types=["html", "pdf", "docx"])
    result = Crawler(spec, transport=site.transport, delay=0).run()
    found = {item.url: item for item in result.items}
    assert set(found) == {
        "https://gemeinde.example/rathaus/",
        "https://gemeinde.example/rathaus/oeffnungszeiten",
        "https://gemeinde.example/rathaus/abfall",
        "https://gemeinde.example/rathaus/standesamt",
        "https://gemeinde.example/rathaus/steuern",  # from the sitemap
        "https://gemeinde.example/rathaus/schule",
        "https://gemeinde.example/fileadmin/gebuehren.pdf",  # file outside the path: same site
        "https://gemeinde.example/fileadmin/formular.docx",  # typed by extension
    }
    assert found["https://gemeinde.example/fileadmin/formular.docx"].kind == "docx"
    # Not requested at all: other hosts, disallowed paths, unwanted types,
    # pages outside the path, images.
    asked = set(site.requests)
    assert not any("other.example" in u for u in asked if "umzug" not in u)
    assert "https://gemeinde.example/intern/geheim" not in asked
    assert "https://gemeinde.example/fileadmin/plan.xlsx" not in asked
    assert "https://gemeinde.example/blog/fest" not in asked
    assert "https://gemeinde.example/bilder/rathaus.jpg" not in asked
    # A redirect off the site is refused; a 503 keeps its document.
    assert any("leaves the site" in why for _, why in result.errors)
    assert "https://gemeinde.example/rathaus/kaputt" in result.kept
    # The footer line on every page is chrome and was dropped.
    abfall = found["https://gemeinde.example/rathaus/abfall"]
    assert "Biotonne" in abfall.text and FOOTER_LINE not in abfall.text
    assert result.complete


def test_crawler_page_scope_and_limits(tmp_path):
    site = Site(tmp_path)
    spec = make_spec("https://gemeinde.example/rathaus/", scope="page", doc_types=["pdf"])
    result = Crawler(spec, transport=site.transport, delay=0).run()
    # Just the files linked from the one page (the page itself is read for
    # its links but not stored: html is not among the types).
    assert [i.url for i in result.items] == ["https://gemeinde.example/fileadmin/gebuehren.pdf"]
    limited = Crawler(
        make_spec("https://gemeinde.example/rathaus/", max_pages=2),
        transport=site.transport,
        delay=0,
    ).run()
    assert len(limited.items) == 2 and not limited.complete


def test_strip_boilerplate_needs_enough_pages():
    items = [
        Fetched(f"https://x.example/{i}", "html", b"", text=f"Inhalt {i}\nFuss") for i in range(4)
    ]
    assert strip_boilerplate(items) == 0
    items.append(Fetched("https://x.example/5", "html", b"", text="Inhalt 5\nFuss"))
    assert strip_boilerplate(items) == 5 and items[0].text == "Inhalt 0"


# -- filing through the runtime ------------------------------------------------


@pytest.fixture()
def rt(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", templates_dir=tmp_path / "unused", embeddings="off"
    )
    runtime = Runtime(settings, definitions={}, model_client=None)
    runtime.crawl_in_background = False
    runtime.crawl_delay = 0
    site = Site(tmp_path)
    runtime.web_transport = site.transport
    runtime.knowledge.save_space("gemeinde", title="Gemeinde-Website", data_class="public")
    yield runtime, site
    runtime.close()


def test_crawl_files_updates_and_retires(rt):
    runtime, site = rt
    spec = make_spec("https://gemeinde.example/rathaus/", doc_types=["html", "pdf", "docx"])
    source = runtime.knowledge_web.save("gemeinde", spec, title="Website", interval_hours=24)
    assert runtime.knowledge_web.due()  # a new source is due at once

    runtime.tick()  # the scheduler starts it (inline in this test)
    view = runtime.knowledge_web.get(source["id"])
    assert view["last_status"] == "partial"  # the redirect and the 503
    assert view["last_stats"]["new"] == 8 and view["documents"] == 8
    assert view["next_run_at"] > view["last_run_at"] and not runtime.knowledge_web.due()
    docs = {d["url"]: d for d in runtime.knowledge.documents("gemeinde")}
    pdf = docs["https://gemeinde.example/fileadmin/gebuehren.pdf"]
    assert pdf["status"] == "indexed" and pdf["method"] == "text_layer"
    assert docs["https://gemeinde.example/fileadmin/formular.docx"]["method"] == "office:docx"
    abfall = docs["https://gemeinde.example/rathaus/abfall"]
    assert abfall["source_id"] == source["id"] and abfall["file_name"] == "abfall.md"
    hits = runtime.knowledge.search(["gemeinde"], "Reisepass Gebühr")
    assert hits[0].url == "https://gemeinde.example/fileadmin/gebuehren.pdf"
    assert hits[0].as_dict()["url"] == hits[0].url

    # Second visit: one page changed, one is gone, the rest unchanged.
    site.pages["/rathaus/abfall"] = (
        200,
        "text/html",
        page("Abfall", "Die Biotonne wird ab Mai mittwochs geleert."),
    )
    del site.pages["/rathaus/standesamt"]
    assert runtime.start_crawl(source["id"], actor="alex")
    stats = runtime.knowledge_web.get(source["id"])["last_stats"]
    assert (stats["updated"], stats["removed"], stats["unchanged"]) == (1, 1, 6)
    docs = {d["url"]: d for d in runtime.knowledge.documents("gemeinde")}
    assert docs["https://gemeinde.example/rathaus/abfall"]["version"] == 2
    assert "https://gemeinde.example/rathaus/standesamt" not in docs
    assert "mittwochs" in runtime.knowledge.search(["gemeinde"], "Biotonne")[0].text

    # The page comes back: the retired document is revived, not duplicated.
    site.pages["/rathaus/standesamt"] = (
        200,
        "text/html",
        page("Standesamt", "Trauungen nach Vereinbarung."),
    )
    runtime.start_crawl(source["id"])
    assert runtime.knowledge_web.get(source["id"])["last_stats"]["new"] == 1
    all_docs = runtime.knowledge.documents("gemeinde", include_deleted=True)
    assert sum(d["url"] == "https://gemeinde.example/rathaus/standesamt" for d in all_docs) == 1


def test_failed_start_retires_nothing_and_schedule_follows_interval(rt):
    runtime, site = rt
    spec = make_spec("https://gemeinde.example/rathaus/", doc_types=["html"])
    source = runtime.knowledge_web.save("gemeinde", spec, interval_hours=0)
    runtime.start_crawl(source["id"])
    before = runtime.knowledge_web.get(source["id"])["documents"]
    assert before == 6 and runtime.knowledge_web.get(source["id"])["next_run_at"] is None

    site.pages["/rathaus/"] = (500, "text/plain", b"maintenance")
    site.pages.pop("/sitemap.xml")
    runtime.start_crawl(source["id"])
    view = runtime.knowledge_web.get(source["id"])
    assert view["last_status"] == "failed" and "HTTP 500" in view["last_error"]
    assert view["documents"] == before  # a broken start page is no reason to forget

    # Switching on an interval schedules the next run from the last one.
    runtime.knowledge_web.save("gemeinde", spec, interval_hours=24, source_id=source["id"])
    view = runtime.knowledge_web.get(source["id"])
    planned = datetime.fromisoformat(view["next_run_at"])
    assert planned - datetime.fromisoformat(view["last_run_at"]) == timedelta(hours=24)
    assert not runtime.knowledge_web.due()
    assert runtime.knowledge_web.due(datetime.now(UTC) + timedelta(hours=25))
    # Archived spaces are never crawled.
    runtime.knowledge.archive_space("gemeinde")
    assert not runtime.knowledge_web.due(datetime.now(UTC) + timedelta(hours=25))


def test_restart_resets_running(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", templates_dir=tmp_path / "x", embeddings="off")
    runtime = Runtime(settings, definitions={}, model_client=None)
    runtime.knowledge.save_space("s1", title="S", data_class="public")
    source = runtime.knowledge_web.save("s1", make_spec("https://gemeinde.example/"))
    runtime.knowledge_web.mark_running(source["id"])
    runtime.close()
    again = Runtime(settings, definitions={}, model_client=None)
    try:
        view = again.knowledge_web.get(source["id"])
        assert view["last_status"] == "failed" and "restart" in view["last_error"]
        assert again.knowledge_web.due()
    finally:
        again.close()


def test_sources_api(rt):
    runtime, _site = rt
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        bad = client.post("/api/v1/knowledge/gemeinde/sources", json={"url": "ftp://x"})
        assert bad.status_code == 422
        resp = client.post(
            "/api/v1/knowledge/gemeinde/sources",
            json={
                "url": "https://gemeinde.example/rathaus/",
                "title": "Website",
                "doc_types": ["html", "pdf"],
                "interval_hours": 168,
                "exclude": ["*/standesamt"],
            },
        )
        assert resp.status_code == 200, resp.text
        source = resp.json()
        assert source["scope"] == "path" and source["running"] is False
        run = client.post(f"/api/v1/knowledge/gemeinde/sources/{source['id']}/run")
        assert run.status_code == 200, run.text
        assert run.json()["last_stats"]["new"] == 6  # 5 pages + PDF, standesamt excluded
        space = client.get("/api/v1/knowledge/gemeinde").json()
        assert [s["id"] for s in space["sources"]] == [source["id"]]
        assert all(d["url"] for d in space["documents"])
        upd = client.put(
            f"/api/v1/knowledge/gemeinde/sources/{source['id']}",
            json={
                **{
                    k: source[k]
                    for k in (
                        "url",
                        "title",
                        "scope",
                        "doc_types",
                        "max_pages",
                        "max_depth",
                        "exclude",
                    )
                },
                "interval_hours": 24,
                "enabled": False,
            },
        )
        assert upd.status_code == 200 and upd.json()["next_run_at"] is None
        gone = client.delete(f"/api/v1/knowledge/gemeinde/sources/{source['id']}")
        assert gone.json() == {"ok": True, "documents_removed": 6}
        assert client.get("/api/v1/knowledge/gemeinde/sources").json() == []
        assert client.get("/api/v1/knowledge/gemeinde").json()["documents"] == []
        missing = client.post("/api/v1/knowledge/nope/sources", json={"url": "https://a.example"})
        assert missing.status_code == 404


def test_background_crawl_takes_the_lock_per_write(rt):
    runtime, _site = rt
    runtime.crawl_in_background = True
    source = runtime.knowledge_web.save(
        "gemeinde",
        make_spec("https://gemeinde.example/rathaus/", doc_types=["html"]),
        interval_hours=0,
    )
    with runtime.lock:
        assert runtime.start_crawl(source["id"])
        assert not runtime.start_crawl(source["id"])  # one crawl per source at a time
    for thread in list(runtime._crawl_threads):
        thread.join(timeout=30)
    view = runtime.knowledge_web.get(source["id"])
    assert view["last_status"] in ("ok", "partial") and view["documents"] == 6
    assert not runtime.crawling(source["id"])
