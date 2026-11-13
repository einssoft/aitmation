"""Backend ``http`` (ADR-0030): the household's transcription container."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"


# -- backend http (deploy/transcribe) -----------------------------------------


def test_http_transcriber_uses_the_service_and_maps_its_answer(tmp_path):
    from aitmation_core.connectors import ConnectorError
    from aitmation_core.transcribe import HttpTranscriber, TranscriptionError

    clip = tmp_path / "note.webm"
    clip.write_bytes(b"\x1aE\xdf\xa3")
    seen = {}

    def call(url, token, path, *, language, model):
        seen.update(url=url, token=token, path=path, language=language, model=model)
        return {
            "text": "Guten Tag zusammen.",
            "language": "de",
            "duration": 2.5,
            "segments": [{"start": 0.0, "end": 2.5, "text": " Guten Tag zusammen. "}],
        }

    t = HttpTranscriber(
        "http://aitmation-transcribe:8000/",
        "tok",
        "large-v3-turbo",
        call=call,
        probe=lambda u: True,
    )
    assert t.available()
    out = t.transcribe(clip, language="de")
    assert (
        out.text == "Guten Tag zusammen."
        and out.engine == "http:large-v3-turbo"
        and out.duration_s == 2.5
    )
    assert out.segments[0].text == "Guten Tag zusammen." and out.language == "de"
    assert seen == {
        "url": "http://aitmation-transcribe:8000",
        "token": "tok",
        "path": clip,
        "language": "de",
        "model": "large-v3-turbo",
    }

    def down(*a, **k):
        raise ConnectorError("transcription service unreachable: ConnectError")

    with pytest.raises(TranscriptionError, match="unreachable"):
        HttpTranscriber("http://x", call=down, probe=lambda u: True).transcribe(clip)
    assert not HttpTranscriber("http://x", probe=lambda u: False).available()


def test_runtime_picks_the_service_when_configured(tmp_path, monkeypatch):
    from aitmation_core import connectors
    from aitmation_core.config import Settings
    from aitmation_core.runtime import Runtime

    monkeypatch.setattr(
        connectors, "transcription_service_ok", lambda url, timeout=5.0: url.endswith(":8000")
    )
    data = tmp_path / "data"
    data.mkdir()
    rt = Runtime(
        Settings(
            data_dir=data,
            templates_dir=CORE_TEMPLATES,
            apps_dir=tmp_path / "none",
            whisper_backend="http",
            whisper_url="http://aitmation-transcribe:8000",
        )
    )
    try:
        assert rt.transcriber_local is not None and rt.transcriber_local.name == "http"
    finally:
        rt.close()
    rt = Runtime(
        Settings(
            data_dir=data,
            templates_dir=CORE_TEMPLATES,
            apps_dir=tmp_path / "none",
            whisper_backend="http",
            whisper_url="http://down:9",
        )
    )
    try:
        assert rt.transcriber_local is None
    finally:
        rt.close()
