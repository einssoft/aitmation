#!/usr/bin/env bash
# Build one release bundle: launcher binary + uv + core sources + apps +
# built UI + start scripts (ADR-0003). Run from the repository root:
#
#   scripts/bundle.sh <version> <goos> <goarch> [out-dir]
#
# e.g. scripts/bundle.sh 0.1.0 windows amd64 dist/
#
# Needs: go, node/npm (or an existing ui/dist), curl, zip/tar, uv. The
# bundle carries its own Python (ADR-0051): the python-build-standalone
# interpreter for the target plus every locked package installed into its
# site-packages, resolved for the target platform on the build machine
# (uv pip install --python-platform). First start needs no network; the
# launcher prefers <bundle>/python over creating a venv (bootstrap.go).
# BUNDLE_PYTHON=0 leaves it out (old behaviour: uv fetches on first start).
# Signing (ADR-0003) is a TODO; a SHA-256 sum is written next to the archive.
set -euo pipefail

VERSION="${1:?version}"
GOOS="${2:?goos}"
GOARCH="${3:?goarch}"
OUT="$(mkdir -p "${4:-dist}" && cd "${4:-dist}" && pwd)"
UV_VERSION="${UV_VERSION:-0.8.17}"
# python-build-standalone: release tag and CPython version (install_only_stripped)
PBS_RELEASE="${PBS_RELEASE:-20260718}"
PY_VERSION="${PY_VERSION:-3.12.13}"
BUNDLE_PYTHON="${BUNDLE_PYTHON:-1}"
# Extras that go into the bundle (comma-separated).
EXTRAS="${EXTRAS:-mcp,knowledge}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="aitmation-${VERSION}-${GOOS}-${GOARCH}"
STAGE="$(mktemp -d)/${NAME}"
mkdir -p "${STAGE}"

echo "== launcher (${GOOS}/${GOARCH})"
EXT=""; [ "${GOOS}" = "windows" ] && EXT=".exe"
( cd "${ROOT}/launcher" && CGO_ENABLED=0 GOOS="${GOOS}" GOARCH="${GOARCH}" \
    go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o "${STAGE}/aitmation-launcher${EXT}" . )

echo "== uv ${UV_VERSION}"
case "${GOOS}-${GOARCH}" in
  windows-amd64) TRIPLE="x86_64-pc-windows-msvc";   ARCHIVE="zip" ;;
  windows-arm64) TRIPLE="aarch64-pc-windows-msvc";  ARCHIVE="zip" ;;
  darwin-arm64)  TRIPLE="aarch64-apple-darwin";     ARCHIVE="tar.gz" ;;
  # darwin-amd64 (Intel Macs): no wheels for cryptography >= 50 and onnxruntime - not built (ADR-0051)
  linux-amd64)   TRIPLE="x86_64-unknown-linux-gnu"; ARCHIVE="tar.gz" ;;
  linux-arm64)   TRIPLE="aarch64-unknown-linux-gnu"; ARCHIVE="tar.gz" ;;
  *) echo "unsupported target ${GOOS}-${GOARCH}" >&2; exit 2 ;;
esac
UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${TRIPLE}.${ARCHIVE}"
TMP="$(mktemp -d)"
curl -fsSL "${UV_URL}" -o "${TMP}/uv.${ARCHIVE}"
if [ "${ARCHIVE}" = "zip" ]; then
  unzip -q "${TMP}/uv.zip" -d "${TMP}/uv"
  cp "${TMP}/uv/uv.exe" "${STAGE}/uv.exe"
else
  tar -xzf "${TMP}/uv.tar.gz" -C "${TMP}"
  cp "${TMP}/uv-${TRIPLE}/uv" "${STAGE}/uv"
  chmod +x "${STAGE}/uv"
fi

if [ "${BUNDLE_PYTHON}" = "1" ]; then
  echo "== python ${PY_VERSION} (${TRIPLE}) + packages"
  PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/cpython-${PY_VERSION}+${PBS_RELEASE}-${TRIPLE}-install_only_stripped.tar.gz"
  curl -fsSL "${PBS_URL}" -o "${TMP}/python.tar.gz"
  tar -xzf "${TMP}/python.tar.gz" -C "${STAGE}"          # unpacks to ${STAGE}/python
  if [ "${GOOS}" = "windows" ]; then
    SITE="${STAGE}/python/Lib/site-packages"
  else
    SITE="${STAGE}/python/lib/python${PY_VERSION%.*}/site-packages"
  fi
  mkdir -p "${SITE}"
  EXTRA_ARGS=(); for e in ${EXTRAS//,/ }; do EXTRA_ARGS+=(--extra "$e"); done
  ( cd "${ROOT}/core" && uv export --frozen --no-dev "${EXTRA_ARGS[@]}" --no-emit-project -o "${TMP}/requirements.txt" -q )
  # macOS wheels of onnxruntime need 14.0; uv reads the deployment target for the platform check
  MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-14.0}" \
  uv pip install --python-platform "${TRIPLE}" --python-version "${PY_VERSION%.*}" --only-binary :all: \
      --target "${SITE}" -r "${TMP}/requirements.txt" -q
  echo "   extras: ${EXTRAS}"
  rm -rf "${SITE}/bin" "${SITE}/Scripts"                  # entry points of the target platform, not needed
  find "${STAGE}/python" -name "__pycache__" -type d -prune -exec rm -rf {} +
  echo "${PY_VERSION}+${PBS_RELEASE}" > "${STAGE}/python/VERSION"
fi

echo "== core, apps, ui"
mkdir -p "${STAGE}/core"
for item in pyproject.toml uv.lock .python-version README.md src templates mailroom; do
  cp -R "${ROOT}/core/${item}" "${STAGE}/core/"
done
find "${STAGE}/core" -name "__pycache__" -type d -prune -exec rm -rf {} +
cp -R "${ROOT}/apps" "${STAGE}/apps"
if [ ! -f "${ROOT}/ui/dist/index.html" ]; then
  ( cd "${ROOT}/ui" && npm ci --no-audit --no-fund && npm run build )
fi
mkdir -p "${STAGE}/ui"
cp -R "${ROOT}/ui/dist" "${STAGE}/ui/dist"

echo "== start scripts"
cp "${ROOT}/deploy/bundle/README.md" "${STAGE}/README.md"
if [ "${GOOS}" = "windows" ]; then
  cp "${ROOT}/deploy/bundle/start.cmd" "${STAGE}/start.cmd"
else
  cp "${ROOT}/deploy/bundle/start.sh" "${STAGE}/start.sh"
  chmod +x "${STAGE}/start.sh"
fi
echo "${VERSION}" > "${STAGE}/VERSION"

echo "== archive"
( cd "$(dirname "${STAGE}")" && if [ "${GOOS}" = "windows" ]; then
    zip -qr "${OUT}/${NAME}.zip" "${NAME}"; echo "${OUT}/${NAME}.zip"
  else
    tar -czf "${OUT}/${NAME}.tar.gz" "${NAME}"; echo "${OUT}/${NAME}.tar.gz"
  fi )
( cd "${OUT}" && sha256sum "${NAME}".* > "${NAME}.sha256" )
rm -rf "$(dirname "${STAGE}")" "${TMP}"
