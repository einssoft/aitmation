"""aitmation-transcribe: a small transcription service for a household.

One container, one job: ``POST /v1/audio/transcriptions`` in the OpenAI
shape (multipart ``file``, optional ``language``, ``response_format``
``json`` | ``verbose_json``) answered by faster-whisper on this machine.
Nothing is stored; the upload lives in a temporary file for the length
of the call. The instance (ADR-0030, backend ``http``) and the assistant
(``STT_PROVIDER=openai`` + ``STT_BASE_URL``) both speak this shape, so
one service serves the whole household - and the household's data stays
in the household.

Environment: ``WHISPER_MODEL`` (default ``large-v3-turbo``),
``WHISPER_MODELS`` (comma list a request may choose from with ``model``;
default only ``WHISPER_MODEL``), ``WHISPER_BEAM`` (default 5; a request
may ask for less with ``beam`` - 1 is about twice as fast, fine for short
dictation), ``WHISPER_BACKEND`` (``faster`` = faster-whisper on CPU/CUDA,
``mlx`` = mlx-whisper on Apple Silicon, uses the GPU of a Mac),
``WHISPER_DEVICE`` (``cpu`` | ``cuda``), ``WHISPER_COMPUTE`` (``int8`` on
CPU, ``float16`` on GPU), ``WHISPER_THREADS`` (CPU threads, default all),
``TRANSCRIBE_TOKEN`` (optional bearer; without it the service trusts its
network), ``HF_HOME`` (model cache, a volume).

Speaker diarization (who spoke when) is optional: image built with
``WITH_DIARIZATION=1`` and ``HF_TOKEN`` set (pyannote's models need the
licence click on Hugging Face once). Then ``diarize=true`` on a
transcription labels every segment with ``speaker`` (``SPEAKER_00`` …),
and ``POST /v1/audio/diarize`` returns the raw turns.
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("transcribe")

MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
MODELS = [m.strip() for m in os.environ.get("WHISPER_MODELS", "").split(",") if m.strip()] or [MODEL_NAME]
if MODEL_NAME not in MODELS:
    MODELS.insert(0, MODEL_NAME)
BACKEND = os.environ.get("WHISPER_BACKEND", "faster")
BEAM = max(1, min(int(os.environ.get("WHISPER_BEAM", "5")), 10))
THREADS = int(os.environ.get("WHISPER_THREADS", "0"))
# mlx-whisper reads converted weights from the mlx-community organisation
MLX_REPOS = {
    "tiny": "mlx-community/whisper-tiny-mlx", "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx", "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx", "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8" if DEVICE == "cpu" else "float16")
TOKEN = os.environ.get("TRANSCRIBE_TOKEN", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
DIARIZE_MODEL = os.environ.get("DIARIZE_MODEL", "pyannote/speaker-diarization-3.1")
MAX_BYTES = int(os.environ.get("TRANSCRIBE_MAX_MB", "200")) * 1024 * 1024
LANGUAGES = {"de", "fr", "it", "en", "rm", "es", "pt", "nl"}

app = FastAPI(title="aitmation-transcribe", docs_url=None, redoc_url=None)
_model = None  # the default model (kept for tests and the health check)
_models: dict = {}
_diarizer = None
_lock = threading.Lock()


def diarization_available() -> bool:
    try:
        import pyannote.audio  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return bool(HF_TOKEN)


def diarizer():
    """pyannote pipeline, loaded once; needs the image built with
    WITH_DIARIZATION=1 and HF_TOKEN."""
    global _diarizer
    with _lock:
        if _diarizer is None:
            import torch
            from pyannote.audio import Pipeline

            started = time.monotonic()
            _diarizer = Pipeline.from_pretrained(DIARIZE_MODEL, use_auth_token=HF_TOKEN)
            if DEVICE == "cuda":
                _diarizer.to(torch.device("cuda"))
            log.info("diarization %s loaded in %.1fs", DIARIZE_MODEL, time.monotonic() - started)
        return _diarizer


def _to_wav(path: str) -> str:
    """pyannote wants a plain WAV; ffmpeg converts anything the browser or
    a phone produced."""
    import subprocess

    out = path + ".16k.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-ac", "1", "-ar", "16000", out], check=True)
    return out


def _waveform(wav: str) -> dict:
    """The 16 kHz mono WAV as the in-memory input pyannote accepts
    everywhere - its own file decoding needs torchcodec, which the CPU
    wheels do not ship reliably."""
    import wave

    import numpy as np
    import torch

    with wave.open(wav, "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": rate}


def _turns(path: str, speakers: int | None) -> list[dict]:
    wav = _to_wav(path)
    try:
        kwargs = {"num_speakers": speakers} if speakers else {}
        annotation = diarizer()(_waveform(wav), **kwargs)
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
    turns = []
    for segment, _track, label in annotation.itertracks(yield_label=True):
        turns.append({"start": round(float(segment.start), 3), "end": round(float(segment.end), 3), "speaker": str(label)})
    return turns


def _label(segments: list[dict], turns: list[dict]) -> None:
    """Each transcript segment gets the speaker who overlaps it most."""
    for seg in segments:
        best, best_overlap = None, 0.0
        for turn in turns:
            overlap = min(seg["end"], turn["end"]) - max(seg["start"], turn["start"])
            if overlap > best_overlap:
                best, best_overlap = turn["speaker"], overlap
        seg["speaker"] = best or ""


def model(name: str | None = None):
    """A loaded faster-whisper model, by name; each is loaded once."""
    global _model
    name = name or MODEL_NAME
    with _lock:
        if name == MODEL_NAME and _model is not None:
            return _model
        if name not in _models:
            from faster_whisper import WhisperModel

            started = time.monotonic()
            _models[name] = WhisperModel(name, device=DEVICE, compute_type=COMPUTE, cpu_threads=THREADS)
            log.info("model %s loaded on %s/%s in %.1fs", name, DEVICE, COMPUTE, time.monotonic() - started)
        if name == MODEL_NAME:
            _model = _models[name]
        return _models[name]


def _mlx(path: str, name: str, language: str | None) -> tuple[list[dict], str | None, float]:
    """Apple Silicon: mlx-whisper on the Mac's GPU (no beam search there,
    greedy decoding)."""
    import mlx_whisper

    repo = MLX_REPOS.get(name, name)
    out = mlx_whisper.transcribe(path, path_or_hf_repo=repo, language=language)
    segments = [{"start": float(s["start"]), "end": float(s["end"]), "text": str(s["text"])}
                for s in out.get("segments") or []]
    duration = segments[-1]["end"] if segments else 0.0
    return segments, out.get("language"), duration


def _authorised(request: Request) -> None:
    if not TOKEN:
        return
    given = request.headers.get("authorization", "")
    if not (given.lower().startswith("bearer ") and secrets.compare_digest(given[7:].strip(), TOKEN)):
        raise HTTPException(401, "bearer token required")

_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>aitmation-transcribe</title><style>body{font:15px system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#1d2733}
h1{font-size:1.2rem}label{display:block;margin:.6rem 0 .2rem}select,input,button{font:inherit;padding:.4rem}button{background:#233e60;color:#fff;border:0;border-radius:6px;padding:.5rem 1rem;margin-top:.8rem;cursor:pointer}
pre{white-space:pre-wrap;background:#f4f6f8;padding:.8rem;border-radius:6px}.muted{color:#667}</style>
<h1>aitmation-transcribe</h1><p class="muted" id="state"></p>
<form id="f"><label>Recording<input type="file" name="file" accept="audio/*,video/*" required></label>
<label>Model<select name="model" id="model"></select></label>
<label>Beam size<select name="beam"><option value="">default</option><option>1</option><option>2</option><option>5</option></select></label>
<label>Language<select name="language"><option value="">detect</option><option selected>de</option><option>fr</option><option>it</option><option>en</option></select></label>
<label>Token (if set)<input name="token" type="password" autocomplete="off"></label>
<button>Transcribe</button></form><pre id="out"></pre>
<script>
fetch('healthz').then(r=>r.json()).then(h=>{document.getElementById('state').textContent=`Backend ${h.backend}, default ${h.model}, beam ${h.beam}, device ${h.device}`;
 document.getElementById('model').innerHTML=h.models.map(m=>`<option ${m===h.model?'selected':''}>${m}</option>`).join('')});
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target),t=f.get('token');f.delete('token');
 for(const k of ['beam','language']) if(!f.get(k)) f.delete(k);
 const o=document.getElementById('out');o.textContent='… running (the first run of a model downloads it)';const s=performance.now();
 const r=await fetch('v1/audio/transcriptions',{method:'POST',body:f,headers:t?{Authorization:'Bearer '+t}:{}});const j=await r.json().catch(()=>({}));
 if(!r.ok){o.textContent='Error '+r.status+': '+(j.detail||'');return}
 const w=(performance.now()-s)/1000,x=j.timing||{};
 o.textContent=`${j.duration}s of audio in ${w.toFixed(1)}s (model ${x.model}, beam ${x.beam}, ${x.speed}× real time)\n\n${j.text}`};
</script></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """A small test page: pick a recording, a model and a beam, see the
    text and how fast it was. The API itself is /v1/audio/transcriptions."""
    return _PAGE


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "model": MODEL_NAME, "models": MODELS, "backend": BACKEND, "beam": BEAM,
            "device": DEVICE, "loaded": _model is not None or bool(_models), "diarization": diarization_available()}


@app.get("/v1/models")
def models(request: Request) -> dict:
    _authorised(request)
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "aitmation"} for m in MODELS]}


def _run(path: str, language: str | None, diarize: bool, speakers: int | None = None,
         name: str | None = None, beam: int | None = None) -> dict:
    name = name or MODEL_NAME
    beam = beam or BEAM
    started = time.monotonic()
    if BACKEND == "mlx":
        raw, detected, duration = _mlx(path, name, language)
    else:
        segs, info = model(name).transcribe(path, language=language, vad_filter=True, beam_size=beam)
        raw = [{"start": float(s.start), "end": float(s.end), "text": s.text} for s in segs]
        detected, duration = getattr(info, "language", None), float(getattr(info, "duration", 0.0) or 0.0)
    out = []
    for s in raw:
        out.append({"id": len(out), "start": round(s["start"], 3), "end": round(s["end"], 3), "text": s["text"].strip()})
    if diarize and out:
        _label(out, _turns(path, speakers))
    seconds = time.monotonic() - started
    return {
        "text": " ".join(s["text"] for s in out if s["text"]).strip(),
        "language": detected,
        "duration": round(duration, 3),
        "segments": out,
        # how fast: the model, the beam, seconds spent, times faster than real time
        "timing": {"model": name, "backend": BACKEND, "beam": beam if BACKEND != "mlx" else 1,
                   "seconds": round(seconds, 2), "speed": round(duration / seconds, 1) if seconds > 0 else None},
    }


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    request: Request,
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    response_format: str = Form(default="json"),
    model_name: str | None = Form(default=None, alias="model"),
    diarize: str = Form(default="false"),
    speakers: int | None = Form(default=None),
    beam: int | None = Form(default=None),
):
    _authorised(request)
    # the model: one of WHISPER_MODELS, anything else (an OpenAI name like
    # "whisper-1" from a client) means the default
    name = model_name if model_name in MODELS else MODEL_NAME
    beam = max(1, min(beam, BEAM)) if beam else None
    lang = (language or "").strip().lower() or None
    if lang and lang not in LANGUAGES:
        lang = None  # let the model detect rather than refuse
    suffix = Path(file.filename or "clip").suffix or ".bin"
    with tempfile.NamedTemporaryFile(prefix="aitm-", suffix=suffix, delete=True) as tmp:
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                raise HTTPException(413, "recording too large")
            tmp.write(chunk)
        tmp.flush()
        if size == 0:
            raise HTTPException(400, "empty upload")
        want_speakers = diarize.strip().lower() in ("true", "1", "yes")
        if want_speakers and not diarization_available():
            raise HTTPException(501, "diarization is not available on this service (WITH_DIARIZATION, HF_TOKEN)")
        started = time.monotonic()
        try:
            result = await run_in_threadpool(_run, tmp.name, lang, want_speakers, speakers, name, beam)
        except Exception as exc:  # noqa: BLE001 - the caller needs the reason, not a 500
            log.exception("transcription failed")
            raise HTTPException(422, f"transcription failed: {exc.__class__.__name__}: {str(exc)[:160]}") from exc
    log.info("%s: %.1fs audio in %.1fs (%s, %s, beam %s)", file.filename, result["duration"],
             time.monotonic() - started, result["language"], name, result["timing"]["beam"])
    if response_format == "text":
        return PlainTextResponse(result["text"])
    if response_format == "verbose_json":
        return JSONResponse({"task": "transcribe", "diarized": want_speakers, **result})
    return JSONResponse({"text": result["text"], "language": result["language"], "duration": result["duration"],
                         "timing": result["timing"]})


@app.post("/v1/audio/diarize")
async def diarize_only(request: Request, file: UploadFile = File(...), speakers: int | None = Form(default=None)):
    """Who spoke when, without the words - for a caller that transcribes
    elsewhere or per track."""
    _authorised(request)
    if not diarization_available():
        raise HTTPException(501, "diarization is not available on this service (WITH_DIARIZATION, HF_TOKEN)")
    suffix = Path(file.filename or "clip").suffix or ".bin"
    with tempfile.NamedTemporaryFile(prefix="aitm-", suffix=suffix, delete=True) as tmp:
        data = await file.read()
        if not data:
            raise HTTPException(400, "empty upload")
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "recording too large")
        tmp.write(data)
        tmp.flush()
        try:
            turns = await run_in_threadpool(_turns, tmp.name, speakers)
        except Exception as exc:  # noqa: BLE001
            log.warning("diarization failed: %s", exc)
            raise HTTPException(422, f"diarization failed: {exc.__class__.__name__}") from exc
    return JSONResponse({"turns": turns, "speakers": sorted({t["speaker"] for t in turns})})


if __name__ == "__main__":
    import uvicorn

    if os.environ.get("TRANSCRIBE_PRELOAD", "1") != "0":
        model()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
