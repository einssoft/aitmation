"""Document I/O: PDF round-trip, plain text, OCR interface behavior."""

import pytest

from aitmation_core.document import DocumentError, UnavailableOcr, create_pdf, extract_text


def test_pdf_roundtrip_create_then_extract(tmp_path):
    pdf = create_pdf(
        tmp_path / "out.pdf",
        "Summary",
        ["first line of content", "second line of content"],
    )
    extracted = extract_text(pdf)
    assert extracted.method == "text_layer"
    assert extracted.page_count == 1
    assert "Summary" in extracted.text
    assert "second line of content" in extracted.text


def test_plain_text_file(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hello, körper", encoding="utf-8")
    extracted = extract_text(f)
    assert extracted.method == "plain_text"
    assert "körper" in extracted.text


def test_pdf_without_text_layer_asks_ocr(tmp_path):
    # A structurally valid, empty one-page PDF: no text layer at all.
    empty = tmp_path / "scan.pdf"
    empty.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )

    class FakeOcr:
        name = "fake"

        def recognize(self, path):
            return "recognized by fake ocr"

    extracted = extract_text(empty, ocr=FakeOcr())
    assert extracted.method == "ocr:fake"
    assert extracted.text == "recognized by fake ocr"

    # The OCR result is cached next to the file (ADR-0030): the second read
    # does not need an engine at all - until the file changes.
    cache = empty.with_name(empty.name + ".extracted.json")
    assert cache.is_file()
    assert extract_text(empty, ocr=UnavailableOcr()).text == "recognized by fake ocr"
    cache.unlink()
    with pytest.raises(DocumentError, match="no OCR engine configured"):
        extract_text(empty, ocr=UnavailableOcr())


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(DocumentError, match="not a file"):
        extract_text(tmp_path / "ghost.pdf")


def test_binary_non_pdf_is_an_error(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(DocumentError, match="neither PDF, image, recording nor UTF-8"):
        extract_text(f)
