#!/bin/sh
# Transcription on a Mac with Apple Silicon: mlx-whisper on the Mac's GPU,
# same API as the container (POST /v1/audio/transcriptions, test page /).
# Needs uv and ffmpeg (brew install uv ffmpeg).
#
# Settings live in ~/.aitmation/transcribe.env (KEY=value per line, kept
# across restarts). The first start creates it with a random token:
#   TRANSCRIBE_TOKEN  bearer token every caller must send
#   HOST              127.0.0.1 (this Mac only) or the Tailscale address
#                     (e.g. 100.95.20.54: reachable in the tailnet only)
#   PORT              8000
#   WHISPER_MODEL     default model (large-v3-turbo)
#   WHISPER_MODELS    what a request may choose (model=…)
set -e
cd "$(dirname "$0")"
CONFIG="${AITMATION_TRANSCRIBE_ENV:-$HOME/.aitmation/transcribe.env}"
if [ ! -f "$CONFIG" ]; then
  mkdir -p "$(dirname "$CONFIG")"
  umask 077
  cat > "$CONFIG" <<CONF
# aitmation-transcribe on this Mac - read by run-mac.sh at every start
TRANSCRIBE_TOKEN=$(openssl rand -hex 32)
HOST=127.0.0.1
PORT=8000
WHISPER_MODEL=large-v3-turbo
WHISPER_MODELS=large-v3-turbo,medium,small,base
CONF
  echo "created $CONFIG (with a new token) - edit HOST there to reach it via Tailscale"
fi
chmod 600 "$CONFIG"
set -a
. "$CONFIG"
set +a
export WHISPER_BACKEND=mlx
echo "aitmation-transcribe on http://${HOST:-127.0.0.1}:${PORT:-8000} (token from $CONFIG)"
exec uv run --python 3.12 --with mlx-whisper --with "fastapi>=0.115" --with "uvicorn[standard]>=0.30" \
  --with "python-multipart>=0.0.9" uvicorn server:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
