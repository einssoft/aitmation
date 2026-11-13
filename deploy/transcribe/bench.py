"""Time a transcription service: the same recording with each model and
beam, printed as seconds and "times faster than real time".

    python bench.py aufnahme.m4a                      # http://127.0.0.1:8000
    python bench.py aufnahme.m4a --url https://… --token … --models large-v3-turbo,small --beams 5,1

Standard library only, so it runs anywhere Python 3 does.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import time
import urllib.request
import uuid
from pathlib import Path


def transcribe(url: str, token: str, path: Path, model: str, beam: int, language: str) -> dict:
    boundary = uuid.uuid4().hex
    fields = {"model": model, "beam": str(beam), "language": language, "response_format": "json"}
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()
    )
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
             f"Content-Type: {kind}\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(url.rstrip("/") + "/v1/audio/transcriptions", data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=3600) as response:
        out = json.loads(response.read())
    out["wall"] = time.monotonic() - started
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="")
    parser.add_argument("--models", default="large-v3-turbo,small")
    parser.add_argument("--beams", default="5,1")
    parser.add_argument("--language", default="de")
    args = parser.parse_args()
    print(f"{'model':<16} {'beam':>4} {'audio s':>8} {'wall s':>7} {'x real':>7}  text")
    for model in args.models.split(","):
        for beam in [int(b) for b in args.beams.split(",")]:
            transcribe(args.url, args.token, args.file, model, beam, args.language)  # warm: load the model
            out = transcribe(args.url, args.token, args.file, model, beam, args.language)
            used = out.get("timing", {})
            speed = out["duration"] / out["wall"] if out["wall"] else 0
            print(f"{used.get('model', model):<16} {used.get('beam', beam):>4} {out['duration']:>8.1f} "
                  f"{out['wall']:>7.1f} {speed:>7.1f}  {out['text'][:60]}")


if __name__ == "__main__":
    main()
