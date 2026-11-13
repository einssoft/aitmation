# aitmation – one instance as one container (ADR-0011, ADR-0046 §5).
#
# Built once by GitHub Actions (.github/workflows/image.yml) as a
# multi-arch image and pushed to GHCR; Coolify and docker compose only
# pull (docs/DEPLOYMENT.md, Variante A). Two stages: the React shell is
# built with Node, the core runs on Python 3.12 with uv and the locked
# dependencies (ADR-0003: reproducible from the lockfile).
#
# Local build:  docker build -t aitmation:dev .
# Local run:    docker run -p 8080:8080 -v aitmation_data:/data aitmation:dev

# -- 1. the shell ----------------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# -- 2. the core -----------------------------------------------------------
FROM python:3.12-slim-bookworm AS app

# OCR for scans and photos (ADR-0028) and ffmpeg for recordings (ADR-0030)
# are system tools next to the core; wget serves the health check.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-ita tesseract-ocr-eng \
      ffmpeg wget ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 aitmation

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/app/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app/core
# Dependencies first (cached between source changes), the sources after.
# The optional extra "knowledge" (fastembed, ONNX) adds local semantic
# search; build with --build-arg EXTRAS="" for the lexical-only image.
ARG EXTRAS="--extra knowledge"
COPY core/pyproject.toml core/uv.lock core/README.md ./
RUN uv sync --frozen --no-dev --no-install-project ${EXTRAS}
COPY core/ ./
RUN uv sync --frozen --no-dev ${EXTRAS}

COPY apps /app/apps
COPY --from=ui /ui/dist /app/ui/dist
COPY deploy/container/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && mkdir -p /data && chown aitmation:aitmation /data

ENV PATH="/app/venv/bin:${PATH}" \
    AITMATION_DATA_DIR=/data \
    AITMATION_HOST=0.0.0.0 \
    AITMATION_PORT=8080 \
    AITMATION_TEMPLATES_DIR=/app/core/templates \
    AITMATION_UI_DIR=/app/ui/dist \
    AITMATION_LANGUAGE=de \
    PYTHONUNBUFFERED=1

USER aitmation
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
