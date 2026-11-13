"""Speech and video as documents (ADR-0030).

A voice note, a recorded meeting, a screen recording with commentary -
for the process core they are documents like a scan is: the text has to
be produced first. This module produces it, and it produces it **on this
machine**: a Whisper model runs locally, nothing leaves the instance
(the same guarantee Tesseract gives for scans, ADR-0028).

Two local backends, picked automatically:

* ``whisper.cpp`` - a native binary (``whisper-cli``) plus a model file,
  driven through a subprocess exactly like Tesseract. No Python
  dependency, MIT-licensed, the cleanest fit for the "native sidecar"
  pattern of the core.
* ``faster-whisper`` - a Python package (optional extra ``transcription``,
  CTranslate2 int8 on the CPU), imported lazily so an installation
  without it has no cost.

Video is handled by ffmpeg, again as a subprocess: the audio track is
extracted and transcribed; key frames (scene changes) can be described
by a vision model **only** when the document's data class permits that
model - the runtime decides, this module only accepts a describer.

Long recordings take minutes on a CPU. The core caches every expensive
extraction next to the file (``<file>.extracted.json``) so a chain of
agent steps transcribes once, not per step.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger("aitmation.transcribe")

AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".weba": "audio/webm",
}
"""Recordings the intake accepts as documents."""

VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
}
"""Screen recordings and clips; the audio track is transcribed, key
frames may be described. A browser voice note arrives as ``.webm`` too -
it simply has no video stream."""

SCENE_THRESHOLD = 0.15  # scene change sensitivity
MAX_FRAMES = 40  # frames offered to a vision model per video, thinned beyond
FRAME_WIDTH = 1024

Runner = Callable[[list[str], float | None], subprocess.CompletedProcess]


def _default_run(args: list[str], timeout: float | None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout, check=False)


class TranscriptionError(RuntimeError):
    """The recording could not be transcribed."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    text: str
    engine: str
    language: str | None = None
    duration_s: float | None = None
    segments: tuple[Segment, ...] = field(default_factory=tuple)

    def with_timestamps(self) -> str:
        """The transcript as ``[mm:ss] text`` lines when segments exist."""
        if not self.segments:
            return self.text
        return "\n".join(f"[{_mmss(s.start)}] {s.text.strip()}" for s in self.segments if s.text)


def _mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class Transcriber(Protocol):
    name: str

    def available(self) -> bool: ...

    def transcribe(self, path: Path, language: str | None = None) -> Transcript: ...


class UnavailableTranscriber:
    """Default: says loudly what is missing (Grundsatz 4)."""

    name = "unavailable"

    def available(self) -> bool:
        return False

    def transcribe(self, path: Path, language: str | None = None) -> Transcript:
        raise TranscriptionError(
            f"{Path(path).name}: no transcription engine configured; install whisper.cpp"
            " (whisper-cli + model) or the optional extra 'transcription'"
            " (see docs/DEPLOYMENT.md)"
        )


# -- ffmpeg ----------------------------------------------------------------


class Ffmpeg:
    """ffmpeg/ffprobe on this machine, through subprocesses."""

    def __init__(
        self,
        binary: str = "",
        *,
        runner: Runner | None = None,
    ) -> None:
        self.binary = binary or shutil.which("ffmpeg") or "ffmpeg"
        self.probe = (
            str(Path(self.binary).with_name("ffprobe")) if binary else shutil.which("ffprobe")
        ) or "ffprobe"
        self._run = runner or _default_run

    def available(self) -> bool:
        try:
            return self._run([self.binary, "-version"], 20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def duration(self, path: Path) -> float | None:
        try:
            done = self._run(
                [
                    self.probe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                60,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        try:
            return float(done.stdout.decode().strip())
        except ValueError:
            return None

    def extract_audio(self, path: Path, target: Path) -> Path:
        """The audio track as 16 kHz mono WAV - what every Whisper wants."""
        done = self._run(
            [
                self.binary,
                "-y",
                "-v",
                "error",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                str(target),
            ],
            3600,
        )
        if done.returncode != 0 or not target.is_file():
            err = done.stderr.decode(errors="replace").strip()
            raise TranscriptionError(f"{path.name}: cannot extract audio: {err or 'ffmpeg failed'}")
        return target

    def key_frames(self, path: Path, workdir: Path, max_frames: int = MAX_FRAMES) -> list[Path]:
        """Scene-change frames as JPEG files (threshold
        0.15, fallback one frame per 10 s, thinned to ``max_frames``)."""
        pattern = workdir / "frame-%04d.jpg"
        done = self._run(
            [
                self.binary,
                "-y",
                "-v",
                "error",
                "-i",
                str(path),
                "-vf",
                f"select='gt(scene,{SCENE_THRESHOLD})',scale={FRAME_WIDTH}:-2",
                "-vsync",
                "vfr",
                str(pattern),
            ],
            3600,
        )
        frames = sorted(workdir.glob("frame-*.jpg")) if done.returncode == 0 else []
        if not frames:
            done = self._run(
                [
                    self.binary,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-vf",
                    # The first frame, then one every ten seconds.
                    "select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,10)',"
                    f"scale={FRAME_WIDTH}:-2",
                    "-vsync",
                    "vfr",
                    str(pattern),
                ],
                3600,
            )
            frames = sorted(workdir.glob("frame-*.jpg")) if done.returncode == 0 else []
        if len(frames) > max_frames:
            step = len(frames) / max_frames
            frames = [frames[int(i * step)] for i in range(max_frames)]
        return frames


# -- whisper.cpp -----------------------------------------------------------


class WhisperCppTranscriber:
    """``whisper-cli`` (whisper.cpp) with a local GGML model file."""

    name = "whisper.cpp"

    def __init__(
        self,
        binary: str = "",
        model_path: str = "",
        *,
        ffmpeg: Ffmpeg | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.binary = binary or shutil.which("whisper-cli") or shutil.which("whisper-cpp") or ""
        self.model_path = model_path
        self.ffmpeg = ffmpeg
        self._run = runner or _default_run

    def available(self) -> bool:
        return bool(self.binary) and bool(self.model_path) and Path(self.model_path).is_file()

    def transcribe(self, path: Path, language: str | None = None) -> Transcript:
        if not self.available():
            raise TranscriptionError("whisper.cpp: binary or model file missing")
        path = Path(path)
        with tempfile.TemporaryDirectory(prefix="aitm-whisper-") as tmp:
            work = Path(tmp)
            wav = path
            if path.suffix.lower() != ".wav":
                if self.ffmpeg is None:
                    raise TranscriptionError(f"{path.name}: ffmpeg needed to convert to WAV")
                wav = self.ffmpeg.extract_audio(path, work / "audio.wav")
            out = work / "out"
            args = [
                self.binary,
                "-m",
                self.model_path,
                "-f",
                str(wav),
                "-oj",
                "-of",
                str(out),
                "-np",
                "-l",
                language or "auto",
            ]
            done = self._run(args, 6 * 3600)
            if done.returncode != 0:
                err = done.stderr.decode(errors="replace").strip()
                raise TranscriptionError(f"{path.name}: whisper.cpp failed: {err[-500:]}")
            result_file = out.with_suffix(".json")
            if not result_file.is_file():
                raise TranscriptionError(f"{path.name}: whisper.cpp wrote no result")
            data = json.loads(result_file.read_text(encoding="utf-8"))
        segments = tuple(
            Segment(
                start=float(s.get("offsets", {}).get("from", 0)) / 1000.0,
                end=float(s.get("offsets", {}).get("to", 0)) / 1000.0,
                text=str(s.get("text", "")).strip(),
            )
            for s in data.get("transcription", [])
        )
        text = " ".join(s.text for s in segments if s.text).strip()
        lang = data.get("result", {}).get("language")
        return Transcript(
            text=text,
            engine=f"whisper.cpp:{Path(self.model_path).stem}",
            language=lang,
            duration_s=segments[-1].end if segments else None,
            segments=segments,
        )


# -- faster-whisper --------------------------------------------------------


class FasterWhisperTranscriber:
    """faster-whisper (CTranslate2) in-process; the optional extra
    ``transcription``. The model is loaded on first use and kept."""

    name = "faster-whisper"

    def __init__(
        self,
        model: str = "small",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        download_root: str | None = None,
        loader: Callable | None = None,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self._loader = loader
        self._model = None

    @staticmethod
    def installed() -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def available(self) -> bool:
        return self._loader is not None or self.installed()

    def _load(self):
        if self._model is None:
            if self._loader is not None:
                self._model = self._loader(self.model_name)
            else:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                    download_root=self.download_root,
                )
        return self._model

    def transcribe(self, path: Path, language: str | None = None) -> Transcript:
        if not self.available():
            raise TranscriptionError("faster-whisper is not installed (extra 'transcription')")
        model = self._load()
        try:
            raw_segments, info = model.transcribe(str(path), language=language, vad_filter=True)
            segments = tuple(
                Segment(start=float(s.start), end=float(s.end), text=str(s.text).strip())
                for s in raw_segments
            )
        except Exception as exc:  # the library raises assorted internals
            raise TranscriptionError(f"{Path(path).name}: transcription failed: {exc}") from exc
        text = " ".join(s.text for s in segments if s.text).strip()
        return Transcript(
            text=text,
            engine=f"faster-whisper:{self.model_name}",
            language=getattr(info, "language", None),
            duration_s=getattr(info, "duration", None),
            segments=segments,
        )


RemoteCall = Callable[..., dict]


class HttpTranscriber:
    """The household's own transcription container (``deploy/transcribe``)
    or any OpenAI-compatible speech endpoint the operator points at -
    backend ``http`` of ADR-0030. The recording goes to that one address
    and nowhere else; what it is allowed to carry is the operator's
    decision when configuring the address (a service in the household
    keeps the data class, a cloud service does not)."""

    name = "http"

    def __init__(
        self,
        url: str,
        token: str | None = None,
        model: str | None = None,
        call: RemoteCall | None = None,
        probe: Callable[[str], bool] | None = None,
    ) -> None:
        from . import connectors

        self.url = url.rstrip("/")
        self.token = token
        self.model = model or None
        self._call = call or connectors.transcribe_remote
        self._probe = probe or connectors.transcription_service_ok

    def available(self) -> bool:
        return bool(self.url) and self._probe(self.url)

    def transcribe(self, path: Path, language: str | None = None) -> Transcript:
        from .connectors import ConnectorError

        try:
            body = self._call(self.url, self.token, Path(path), language=language, model=self.model)
        except ConnectorError as exc:
            raise TranscriptionError(f"{Path(path).name}: {exc}") from exc
        segments = tuple(
            Segment(
                start=float(s.get("start", 0.0)),
                end=float(s.get("end", 0.0)),
                text=str(s.get("text", "")).strip(),
            )
            for s in body.get("segments") or []
            if isinstance(s, dict)
        )
        text = str(body.get("text") or " ".join(s.text for s in segments)).strip()
        duration = body.get("duration")
        return Transcript(
            text=text, engine=f"http:{self.model or 'default'}", language=body.get("language"),
            duration_s=float(duration) if isinstance(duration, int | float) else None,
            segments=segments,
        )


# -- video -----------------------------------------------------------------

FrameDescriber = Callable[[bytes, str], tuple[str, float | None]]
"""(image bytes, media type) → (description, confidence); the runtime
hands in a vision model call when the data class permits, else None."""


@dataclass(frozen=True)
class VideoText:
    transcript: Transcript
    frames: tuple[tuple[str, str], ...]  # (timestamp mm:ss, description)

    @property
    def text(self) -> str:
        parts = [self.transcript.with_timestamps()]
        if self.frames:
            parts.append("\n".join(f"[Bild {ts}] {d}" for ts, d in self.frames if d))
        return "\n\n".join(p for p in parts if p).strip()


def transcribe_video(
    path: Path,
    transcriber: Transcriber,
    ffmpeg: Ffmpeg,
    describe: FrameDescriber | None = None,
    *,
    language: str | None = None,
) -> VideoText:
    """Audio track → transcript; key frames → descriptions when a describer
    is permitted. A recording without an audio stream yields an empty
    transcript, not an error - the frames may still carry the content."""
    path = Path(path)
    with tempfile.TemporaryDirectory(prefix="aitm-video-") as tmp:
        work = Path(tmp)
        try:
            wav = ffmpeg.extract_audio(path, work / "audio.wav")
            transcript = transcriber.transcribe(wav, language=language)
        except TranscriptionError as exc:
            if describe is None:
                raise
            log.info("video %s: no usable audio (%s), frames only", path.name, exc)
            transcript = Transcript(text="", engine=transcriber.name)
        frames: list[tuple[str, str]] = []
        if describe is not None:
            duration = ffmpeg.duration(path)
            files = ffmpeg.key_frames(path, work)
            for i, frame in enumerate(files):
                # Frame files carry no timestamp; spread them over the duration.
                ts = _mmss(duration * i / max(1, len(files))) if duration else f"#{i + 1}"
                description, _ = describe(frame.read_bytes(), "image/jpeg")
                frames.append((ts, description.strip()))
    return VideoText(transcript=transcript, frames=tuple(frames))
