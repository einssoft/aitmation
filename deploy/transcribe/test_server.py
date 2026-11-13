"""HTTP layer of the transcription service with a fake Whisper and a fake
diarizer - the real models need a download the CI does not do."""

from __future__ import annotations

import types

from fastapi.testclient import TestClient

import server


class Info:
    language = "de"
    duration = 3.2


class FakeWhisper:
    def transcribe(self, path, language=None, vad_filter=True, beam_size=5):
        seg = types.SimpleNamespace
        return [seg(start=0.0, end=1.5, text=" Guten Tag."), seg(start=1.6, end=3.2, text=" Wie geht es?")], Info()


def test_transcription_json_and_verbose(monkeypatch):
    monkeypatch.setattr(server, "_model", FakeWhisper())
    monkeypatch.setattr(server, "TOKEN", "t")
    client = TestClient(server.app)
    assert client.get("/healthz").json()["ok"] is True
    assert client.post("/v1/audio/transcriptions", files={"file": ("a.webm", b"abc", "audio/webm")}).status_code == 401
    r = client.post("/v1/audio/transcriptions", headers={"authorization": "Bearer t"},
                    files={"file": ("a.webm", b"abc", "audio/webm")}, data={"language": "de"})
    body = r.json()
    assert r.status_code == 200 and {k: body[k] for k in ("text", "language", "duration")} == {
        "text": "Guten Tag. Wie geht es?", "language": "de", "duration": 3.2}
    assert body["timing"]["model"] == "large-v3-turbo" and body["timing"]["beam"] == 5
    r = client.post("/v1/audio/transcriptions", headers={"authorization": "Bearer t"},
                    files={"file": ("a.webm", b"abc", "audio/webm")}, data={"response_format": "verbose_json"})
    body = r.json()
    assert body["segments"][1] == {"id": 1, "start": 1.6, "end": 3.2, "text": "Wie geht es?"} and body["diarized"] is False
    r = client.post("/v1/audio/transcriptions", headers={"authorization": "Bearer t"},
                    files={"file": ("a.webm", b"abc", "audio/webm")}, data={"diarize": "true"})
    assert r.status_code == 501  # not built with diarization here


def test_diarization_labels_segments(monkeypatch):
    monkeypatch.setattr(server, "_model", FakeWhisper())
    monkeypatch.setattr(server, "TOKEN", "")
    monkeypatch.setattr(server, "diarization_available", lambda: True)
    monkeypatch.setattr(server, "_turns", lambda path, speakers: [
        {"start": 0.0, "end": 1.4, "speaker": "SPEAKER_00"}, {"start": 1.4, "end": 3.5, "speaker": "SPEAKER_01"}])
    client = TestClient(server.app)
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.webm", b"abc", "audio/webm")},
                    data={"diarize": "true", "response_format": "verbose_json"})
    assert r.status_code == 200, r.text
    assert [s["speaker"] for s in r.json()["segments"]] == ["SPEAKER_00", "SPEAKER_01"]
    r = client.post("/v1/audio/diarize", files={"file": ("a.webm", b"abc", "audio/webm")})
    assert r.json()["speakers"] == ["SPEAKER_00", "SPEAKER_01"]


def test_model_and_beam_per_request(monkeypatch):
    seen = {}

    class Recording(FakeWhisper):
        def transcribe(self, path, language=None, vad_filter=True, beam_size=5):
            seen["beam"] = beam_size
            return super().transcribe(path, language, vad_filter, beam_size)

    monkeypatch.setattr(server, "TOKEN", "")
    monkeypatch.setattr(server, "MODELS", ["large-v3-turbo", "small"])
    monkeypatch.setattr(server, "_models", {"small": Recording(), "large-v3-turbo": Recording()})
    monkeypatch.setattr(server, "_model", None)
    client = TestClient(server.app)
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.webm", b"abc", "audio/webm")},
                    data={"model": "small", "beam": "1"})
    assert r.json()["timing"]["model"] == "small" and seen["beam"] == 1
    # an unknown name (e.g. "whisper-1" from an OpenAI client) means the default; beam never above the limit
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.webm", b"abc", "audio/webm")},
                    data={"model": "whisper-1", "beam": "9"})
    assert r.json()["timing"]["model"] == "large-v3-turbo" and seen["beam"] == 5
    assert client.get("/v1/models").json()["data"][1]["id"] == "small"


def test_the_root_is_a_test_page():
    page = TestClient(server.app).get("/")
    assert page.status_code == 200 and "v1/audio/transcriptions" in page.text
