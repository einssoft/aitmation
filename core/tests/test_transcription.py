"""Speech and video as documents (ADR-0030): local Whisper backends,
ffmpeg as a subprocess, key frames only with permission, the extraction
cache, and the intake path for a voice note."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aitmation_core import providers
from aitmation_core.config import Settings
from aitmation_core.document import DocumentError, extract_text
from aitmation_core.model import ModelResponse
from aitmation_core.model_registry import entry_from_dict
from aitmation_core.runtime import Runtime
from aitmation_core.transcribe import (
    FasterWhisperTranscriber,
    Ffmpeg,
    Segment,
    Transcript,
    TranscriptionError,
    UnavailableTranscriber,
    WhisperCppTranscriber,
    transcribe_video,
)
from aitmation_core.web import create_app

REPO = Path(__file__).resolve().parents[2]
CORE_TEMPLATES = REPO / "core" / "templates"
APPS = REPO / "apps"


class FakeTranscriber:
    name = "fake"

    def __init__(self, text="Hallo Welt"):
        self.text = text
        self.calls: list[Path] = []

    def available(self):
        return True

    def transcribe(self, path, language=None):
        self.calls.append(Path(path))
        return Transcript(
            text=self.text,
            engine="fake:tiny",
            language="de",
            duration_s=3.0,
            segments=(Segment(0.0, 1.5, self.text[:5]), Segment(1.5, 3.0, self.text[5:])),
        )


def _fake_ffmpeg(frames: int = 0, fail_audio: bool = False) -> Ffmpeg:
    """An ffmpeg that writes what the caller asked for instead of running."""
    calls: list[list[str]] = []

    def run(args, timeout):
        calls.append(args)
        if args[1] == "-version":
            return subprocess.CompletedProcess(args, 0, b"ffmpeg version 6", b"")
        if "-show_entries" in args:
            return subprocess.CompletedProcess(args, 0, b"42.5\n", b"")
        if "-vn" in args:  # audio extraction
            if fail_audio:
                return subprocess.CompletedProcess(args, 1, b"", b"no audio stream")
            Path(args[-1]).write_bytes(b"RIFF....WAVE")
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if "-vf" in args:  # frames
            pattern = Path(args[-1])
            if "scene" in args[args.index("-vf") + 1]:
                for i in range(frames):
                    (pattern.parent / f"frame-{i + 1:04d}.jpg").write_bytes(b"jpg%d" % i)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return subprocess.CompletedProcess(args, 1, b"", b"unexpected")

    tool = Ffmpeg("/opt/ffmpeg", runner=run)
    tool.calls = calls  # type: ignore[attr-defined]
    return tool


def test_transcript_with_timestamps_and_unavailable_engine(tmp_path):
    t = Transcript(text="a b", engine="x", segments=(Segment(0, 65, "a"), Segment(65, 70, "b")))
    assert t.with_timestamps() == "[00:00] a\n[01:05] b"
    assert Transcript(text="plain", engine="x").with_timestamps() == "plain"
    with pytest.raises(TranscriptionError, match="no transcription engine"):
        UnavailableTranscriber().transcribe(tmp_path / "x.wav")


def test_whisper_cpp_runs_the_binary_and_reads_json(tmp_path):
    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"model")
    seen: list[list[str]] = []

    def run(args, timeout):
        seen.append(args)
        out = Path(args[args.index("-of") + 1]).with_suffix(".json")
        out.write_text(
            json.dumps(
                {
                    "result": {"language": "de"},
                    "transcription": [
                        {"offsets": {"from": 0, "to": 1200}, "text": " Guten Tag"},
                        {"offsets": {"from": 1200, "to": 2500}, "text": " Rechnung"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, b"", b"")

    engine = WhisperCppTranscriber("/opt/whisper-cli", str(model), runner=run)
    assert engine.available()
    wav = tmp_path / "note.wav"
    wav.write_bytes(b"RIFF")
    result = engine.transcribe(wav)
    assert result.text == "Guten Tag Rechnung" and result.language == "de"
    assert result.engine == "whisper.cpp:ggml-small" and result.duration_s == 2.5
    assert seen[0][:3] == ["/opt/whisper-cli", "-m", str(model)] and "-oj" in seen[0]
    # A non-WAV file needs ffmpeg; without it the error says so.
    m4a = tmp_path / "note.m4a"
    m4a.write_bytes(b"x")
    with pytest.raises(TranscriptionError, match="ffmpeg"):
        engine.transcribe(m4a)
    engine.ffmpeg = _fake_ffmpeg()
    assert engine.transcribe(m4a).text == "Guten Tag Rechnung"
    # Missing model = not available, loud error.
    assert not WhisperCppTranscriber("/opt/whisper-cli", "/nope.bin", runner=run).available()


def test_faster_whisper_uses_the_injected_model():
    class Info:
        language = "en"
        duration = 4.0

    class Seg:
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    class Model:
        def transcribe(self, path, language=None, vad_filter=True):
            return iter([Seg(0, 2, " The invoice"), Seg(2, 4, " is due")]), Info()

    engine = FasterWhisperTranscriber("tiny", loader=lambda name: Model())
    assert engine.available()
    result = engine.transcribe(Path("/x.wav"))
    assert result.text == "The invoice is due" and result.engine == "faster-whisper:tiny"
    assert result.language == "en" and len(result.segments) == 2


def test_ffmpeg_argument_shapes(tmp_path):
    tool = _fake_ffmpeg(frames=3)
    assert tool.available() and tool.duration(tmp_path / "v.mp4") == 42.5
    wav = tool.extract_audio(tmp_path / "v.mp4", tmp_path / "a.wav")
    assert wav.is_file()
    args = tool.calls[-1]
    assert args[args.index("-ar") + 1] == "16000" and args[args.index("-ac") + 1] == "1"
    frames = tool.key_frames(tmp_path / "v.mp4", tmp_path)
    assert [f.name for f in frames] == ["frame-0001.jpg", "frame-0002.jpg", "frame-0003.jpg"]
    # Thinning keeps the count bounded.
    for i in range(3, 12):
        (tmp_path / f"frame-{i + 1:04d}.jpg").write_bytes(b"x")
    assert len(tool.key_frames(tmp_path / "v.mp4", tmp_path, max_frames=4)) == 4


def test_video_transcribes_audio_and_describes_frames_only_with_permission(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    engine = FakeTranscriber("Bitte die Rechnung prüfen")
    # Without a describer: transcript only, no frame extraction at all.
    result = transcribe_video(video, engine, _fake_ffmpeg(frames=2))
    assert result.frames == () and "[00:00] Bitte" in result.text
    # With a describer: frames go to it, timestamps spread over the duration.
    seen = []

    def describe(image, media_type):
        seen.append((image, media_type))
        return f"Bildschirm {len(seen)}", 0.8

    result = transcribe_video(video, engine, _fake_ffmpeg(frames=2), describe)
    assert seen == [(b"jpg0", "image/jpeg"), (b"jpg1", "image/jpeg")]
    assert "[Bild 00:00] Bildschirm 1" in result.text and "[Bild 00:21] Bildschirm 2" in result.text
    # No audio stream: frames alone still make a document; without frames it is an error.
    silent = transcribe_video(video, engine, _fake_ffmpeg(frames=1, fail_audio=True), describe)
    assert silent.transcript.text == "" and silent.frames[0][1] == "Bildschirm 3"
    with pytest.raises(TranscriptionError, match="cannot extract audio"):
        transcribe_video(video, engine, _fake_ffmpeg(fail_audio=True))


def test_extract_text_handles_recordings_and_caches(tmp_path):
    note = tmp_path / "note.m4a"
    note.write_bytes(b"audio")
    engine = FakeTranscriber("Zahlung freigeben")
    extracted = extract_text(note, transcriber=engine)
    assert extracted.method == "transcript:fake:tiny"
    assert extracted.text == "[00:00] Zahlu\n[00:01] ng freigeben"
    # Cached: no second transcription, even without an engine.
    assert extract_text(note).text == extracted.text and len(engine.calls) == 1
    # A changed file invalidates the cache.
    note.write_bytes(b"audio, longer")
    with pytest.raises(DocumentError, match="no transcription engine"):
        extract_text(note)
    # Video needs ffmpeg.
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"mov")
    with pytest.raises(DocumentError, match="ffmpeg"):
        extract_text(clip, transcriber=engine)
    out = extract_text(clip, transcriber=engine, ffmpeg=_fake_ffmpeg(frames=1))
    assert out.method == "transcript:fake:tiny"
    out = extract_text(
        clip, transcriber=engine, ffmpeg=_fake_ffmpeg(frames=1), describe=lambda b, m: ("x", 1)
    )
    assert out.method == "transcript:fake:tiny"  # cached from the call before


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("AITMATION_ANTHROPIC_API_KEY", raising=False)
    settings = Settings(
        data_dir=tmp_path / "data",
        templates_dir=CORE_TEMPLATES,
        apps_dir=APPS,
        instance_type="company",
        whisper_backend="off",
    )
    rt = Runtime(settings)
    yield rt
    rt.close()


class _Vision:
    def transcribe_image(self, image, media_type):
        return ModelResponse(
            data={"text": "a form on screen"},
            model_ref="m",
            confidence=0.9,
            tokens_in=1,
            tokens_out=1,
        )


def test_runtime_gates_frame_description_like_ocr(runtime, monkeypatch):
    assert runtime.transcriber_local is None  # backend "off"
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
    assert runtime.frame_describer("internal") is not None
    assert runtime.frame_describer("sensitive") is None
    assert runtime.frame_describer(None) is None
    object.__setattr__(runtime.settings, "video_frames", False)
    assert runtime.frame_describer("internal") is None


def test_voice_note_in_the_inbox_is_transcribed_once_for_the_chain(runtime, tmp_path):
    engine = FakeTranscriber("Bitte Offerte an Müller AG senden")
    runtime.transcriber_local = engine
    (runtime.settings.inbox_dir / "sprachnotiz.weba").write_bytes(b"opus")
    runtime.tick()
    case = runtime.conn.execute("SELECT id FROM cases").fetchone()
    assert case is not None
    stored = next(runtime.settings.files_dir.glob("*_sprachnotiz.weba"))
    # Without a model the intake stub still transcribed locally (courtesy
    # for the person at the sorting gate); every later step hits the cache.
    assert engine.calls == [stored]
    payload = runtime._agent_payload(case["id"], {"context": {"document": True}}, "internal")
    [doc] = payload["documents"]
    assert doc["method"] == "transcript:fake:tiny" and "Offerte" in doc["text"]
    runtime._agent_payload(case["id"], {"context": {"document": True}}, "internal")
    assert engine.calls == [stored]
    assert stored.with_name(stored.name + ".extracted.json").is_file()


def test_api_reports_capabilities_and_serves_media(runtime):
    runtime.transcriber_local = FakeTranscriber()
    with TestClient(create_app(runtime, model_factory=lambda s: None)) as client:
        caps = client.get("/api/v1/instance").json()["capabilities"]
        assert caps["transcription"] == "fake" and "ocr" in caps and "ffmpeg" in caps
        (runtime.settings.inbox_dir / "memo.mp3").write_bytes(b"ID3")
        runtime.transcriber_local = None  # nothing can transcribe during intake
        runtime.tick()
        case = runtime.conn.execute("SELECT id FROM cases").fetchone()["id"]
        resp = client.get(f"/api/v1/cases/{case}/document")
        assert resp.headers["content-type"].startswith("audio/mpeg")
        # The preview does not transcribe in the request: pending until the tick did it.
        text = client.get(f"/api/v1/cases/{case}/document/text").json()
        assert text["text"] is None and text["pending"] is True
        runtime.transcriber_local = FakeTranscriber()
        runtime._agent_payload(case, {"context": {"document": True}}, "internal")
        text = client.get(f"/api/v1/cases/{case}/document/text").json()
        assert text["method"] == "transcript:fake:tiny" and "Hallo" in text["text"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_extracts_audio_and_frames(tmp_path):
    tool = Ffmpeg()
    assert tool.available()
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            tool.binary,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(clip),
        ],
        check=True,
    )
    assert 2.5 < (tool.duration(clip) or 0) < 3.5
    wav = tool.extract_audio(clip, tmp_path / "a.wav")
    assert wav.read_bytes()[:4] == b"RIFF"
    frames = tool.key_frames(clip, tmp_path)
    assert len(frames) >= 1 and frames[0].read_bytes()[:2] == b"\xff\xd8"
