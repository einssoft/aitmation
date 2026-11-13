# aitmation-transcribe: faster-whisper behind an OpenAI-compatible endpoint.
# CPU image; for a GPU host swap the base for an nvidia/cuda runtime and
# set WHISPER_DEVICE=cuda. Models are downloaded on first start into
# HF_HOME - mount a volume at /data so that happens once.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# av<16: PyAV 16 dropped open(metadata_errors=…), which faster-whisper 1.2
# still passes - every transcription failed with a TypeError
RUN pip install --no-cache-dir "faster-whisper>=1.1" "av>=11,<16" "fastapi>=0.115" "uvicorn[standard]>=0.30" "python-multipart>=0.0.9"
# Speaker diarization is optional (adds torch + pyannote, ~1.5 GB):
#   docker build --build-arg WITH_DIARIZATION=1 .
ARG WITH_DIARIZATION=0
RUN if [ "$WITH_DIARIZATION" = "1" ]; then \
      pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch>=2.3" "torchaudio>=2.3" \
      && pip install --no-cache-dir "pyannote.audio>=3.3"; \
    fi
RUN useradd -m -u 10001 transcribe && mkdir -p /data && chown transcribe /data
ENV HF_HOME=/data/hf WHISPER_MODEL=large-v3-turbo WHISPER_DEVICE=cpu OMP_NUM_THREADS=4 PORT=8000
COPY server.py /app/server.py
USER transcribe
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=600s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
CMD ["python", "/app/server.py"]
