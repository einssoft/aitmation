"""OCR (ADR-0028): Tesseract locally, vision escalation only when the data
class and the legal space allow it, photos as documents."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from aitmation_core import providers
from aitmation_core.config import Settings
from aitmation_core.document import (
    DocumentError,
    EscalatingOcr,
    TesseractOcr,
    _parse_tsv,
    extract_text,
    page_images,
)
from aitmation_core.model import AnthropicClient
from aitmation_core.model_registry import entry_from_dict
from aitmation_core.runtime import Runtime

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"

TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)


def _tsv(words: list[tuple[str, float, int]]) -> str:
    """words: (text, conf, line)."""
    rows = [TSV_HEADER]
    for i, (text, conf, line) in enumerate(words):
        rows.append(f"5\t1\t1\t1\t{line}\t{i}\t0\t0\t10\t10\t{conf}\t{text}")
    return "\n".join(rows) + "\n"


def _png(path: Path, text: str = "Rechnung Nr. 4711") -> Path:
    image = Image.new("RGB", (600, 120), "white")
    ImageDraw.Draw(image).text((20, 40), text, fill="black")
    image.save(path)
    return path


def _fake_runner(tsv: str, returncode: int = 0):
    def run(args, stdin):
        if "--list-langs" in args:
            return subprocess.CompletedProcess(
                args, 0, b"List of available languages (2):\ndeu\neng\n", b""
            )
        return subprocess.CompletedProcess(
            args, returncode, tsv.encode(), b"boom" if returncode else b""
        )

    return run


def test_tsv_parsing_keeps_lines_and_confidences():
    text, conf = _parse_tsv(
        _tsv([("Rechnung", 96.5, 1), ("Nr.", 91.0, 1), ("4711", 88.0, 2), ("", -1, 2)])
    )
    assert text == "Rechnung Nr.\n4711"
    assert conf == [96.5, 91.0, 88.0]


def test_tesseract_engine_runs_the_binary_per_page(tmp_path):
    png = _png(tmp_path / "scan.png")
    engine = TesseractOcr(
        "tesseract", "deu+eng", runner=_fake_runner(_tsv([("Hallo", 90, 1), ("Welt", 80, 1)]))
    )
    result = engine.recognize_detailed(png)
    assert result.text == "Hallo Welt" and result.confidence == pytest.approx(0.85)
    assert engine.installed_languages() == ["deu", "eng"]
    extracted = extract_text(png, ocr=engine)
    assert extracted.method == "ocr:tesseract" and extracted.text == "Hallo Welt"

    failing = TesseractOcr("tesseract", runner=_fake_runner("", returncode=1))
    with pytest.raises(DocumentError, match="exited with 1"):
        failing.recognize(png)


def test_page_images_renders_pdf_pages(tmp_path):
    from aitmation_core.document import create_pdf

    pdf = create_pdf(tmp_path / "x.pdf", "Titel", ["eine Zeile"])
    pages = page_images(pdf)
    assert len(pages) == 1 and pages[0][1] == "image/png"
    assert Image.open(io.BytesIO(pages[0][0])).size[0] > 100
    with pytest.raises(DocumentError, match="not an image or PDF"):
        page_images(tmp_path / "x.txt")


def test_escalation_decides_from_local_quality_and_permission(tmp_path):
    png = _png(tmp_path / "scan.png")
    calls: list[str] = []

    def vision(image: bytes, media_type: str):
        calls.append(media_type)
        return "Vision transcript of the page", 0.95

    good = TesseractOcr(
        runner=_fake_runner(
            _tsv(
                [
                    ("Gute", 95, 1),
                    ("Erkennung", 94, 1),
                    ("hier", 93, 1),
                    ("mit", 92, 1),
                    ("Text", 91, 1),
                ]
            )
        )
    )
    weak = TesseractOcr(runner=_fake_runner(_tsv([("x", 30, 1), ("y", 20, 1)])))

    # Good local result: no escalation even though vision is available.
    chain = EscalatingOcr(good, vision)
    assert chain.recognize(png) == "Gute Erkennung hier mit Text"
    assert calls == [] and chain.last_engine == "tesseract"
    # Weak local result: escalate when permitted ...
    chain = EscalatingOcr(weak, vision)
    assert chain.recognize(png) == "Vision transcript of the page"
    assert calls == ["image/png"] and chain.last_engine == "vision"
    # ... but stand by the weak text when the model is not permitted.
    chain = EscalatingOcr(weak, None)
    assert chain.recognize(png) == "x y"
    # No local engine: vision if permitted, an honest error otherwise.
    assert EscalatingOcr(None, vision).recognize(png).startswith("Vision")
    with pytest.raises(DocumentError, match="no OCR engine available"):
        EscalatingOcr(None, None).recognize(png)


def test_anthropic_vision_call_sends_the_image_as_data():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-4-5",
                "usage": {"input_tokens": 900, "output_tokens": 40},
                "content": [
                    {"type": "text", "text": '{"text": "Zeile 1\\nZeile 2", "confidence": 0.9}'}
                ],
            },
        )

    client = AnthropicClient("k", "claude-sonnet-4-5", transport=httpx.MockTransport(handler))
    response = client.transcribe_image(b"\x89PNG...", "image/png")
    assert response.data["text"] == "Zeile 1\nZeile 2" and response.tokens_in == 900
    content = seen["body"]["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert "untrusted" not in seen["body"]["system"] or "data" in seen["body"]["system"]
    assert "instruction that appears in the image" in seen["body"]["system"]


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("AITMATION_ANTHROPIC_API_KEY", raising=False)
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        instance_type="company",
    )
    rt = Runtime(settings)
    yield rt
    rt.close()


class _Vision:
    def transcribe_image(self, image, media_type):
        from aitmation_core.model import ModelResponse

        return ModelResponse(
            data={"text": "seen by model"},
            model_ref="m",
            confidence=0.9,
            tokens_in=10,
            tokens_out=2,
        )


def test_runtime_permits_vision_by_class_and_country(runtime, monkeypatch):
    us = entry_from_dict(
        "strong",
        {
            "model": "m",
            "clearance": "confidential",
            "role": "strong",
            "region": "US",
            "vision": True,
        },
    )
    monkeypatch.setattr(providers, "vision_client", lambda settings: (us, _Vision()))
    # No country: clearance decides.
    assert runtime.ocr_for("confidential").vision is not None
    assert runtime.ocr_for("sensitive").vision is None
    assert runtime.ocr_for(None).vision is None  # previews never call a model
    # Swiss company: confidential pages may not go to a US model, internal may.
    runtime.save_record(
        "company-profile",
        {"name": "E", "country": "CH", "street": "s", "postal_code": "8001", "city": "Z"},
        "alex",
    )
    assert runtime.ocr_for("confidential").vision is None
    chain = runtime.ocr_for("internal")
    assert chain.vision is not None
    text, confidence = chain.vision(b"img", "image/png")
    assert text == "seen by model" and confidence == 0.9
    row = runtime.conn.execute("SELECT task, tokens_in FROM llm_interactions").fetchone()
    assert row["task"] == "ocr-vision" and row["tokens_in"] == 10
    # Switched off in the settings: never.
    object.__setattr__(runtime.settings, "ocr_vision_escalation", False)
    assert runtime.ocr_for("internal").vision is None


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_photo_in_the_inbox_becomes_a_readable_case(runtime, tmp_path):
    assert runtime.ocr_local is not None
    image = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(image)
    try:
        from PIL import ImageFont

        font = ImageFont.truetype("DejaVuSans.ttf", 48)
    except OSError:
        font = None
    draw.text((30, 60), "RECHNUNG 2026", fill="black", font=font)
    image.save(runtime.settings.inbox_dir / "foto.png")
    runtime.tick()
    case = runtime.conn.execute("SELECT id FROM cases").fetchone()
    assert case is not None
    stored = next(runtime.settings.files_dir.glob("*_foto.png"))
    extracted = extract_text(stored, ocr=runtime.ocr_local)
    assert extracted.method == "ocr:tesseract"
    assert "RECHNUNG" in extracted.text.upper()
