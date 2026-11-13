#!/usr/bin/env bash
# Build and test the whole repository (macOS/Linux) - the same gates as CI:
# core (ruff, pytest, solution checks), ui (npm ci, build), launcher (go vet,
# go test when Go is installed). `--start` then runs the core with the built
# UI and the example solutions in a dev data directory (`_dev` next to the
# repo), so ~/.aitmation stays untouched.
#
#   scripts/check.sh                 # everything
#   scripts/check.sh --skip-ui       # core (and launcher) only
#   scripts/check.sh --start         # checks, then start on :8477
#   scripts/check.sh --start --skip-tests
#
# Run it, do not source it: `. scripts/check.sh` would end your shell on
# the first `exit`.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  echo "check.sh: run it as 'bash scripts/check.sh' - sourcing it would close your shell." >&2
  return 2 2>/dev/null
fi
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEV_DIR="${DEV_DIR:-$(dirname "$REPO")/_dev}"
PORT="${PORT:-8477}"
SKIP_UI=0; SKIP_LAUNCHER=0; SKIP_TESTS=0; START=0
for arg in "$@"; do
  case "$arg" in
    --skip-ui) SKIP_UI=1 ;;
    --skip-launcher) SKIP_LAUNCHER=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --start) START=1 ;;
    *) echo "unknown option $arg"; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$1"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "$1 not found - $2"; exit 1; }; }

cd "$REPO"
if [ "$SKIP_TESTS" = 0 ]; then
  need uv "install uv: https://docs.astral.sh/uv/"
  (
    cd core
    step "core: uv sync";        uv sync --extra mcp
    step "core: ruff check";     uv run ruff check .
    step "core: license boundary ee/"; uv run python ../scripts/check_ee_boundary.py
    step "core: pytest";         uv run pytest -q
    step "core: validate bundles (format 1)"
    bundles=$(find ../apps -mindepth 2 -maxdepth 2 -name app.toml -exec dirname {} \;)
    if [ -n "$bundles" ]; then uv run python -m aitmation_core validate-app $bundles; else echo "    (none)"; fi
    step "core: validate solutions (format 2)"
    uv run python -m aitmation_core validate-solution \
      $(find ../apps ../docs/examples -mindepth 2 -maxdepth 2 -name solution.toml -exec dirname {} \;)
    step "core: proving runs"
    for s in $(find ../apps ../docs/examples -mindepth 2 -maxdepth 2 -name solution.toml -exec dirname {} \; | sort); do
      uv run python -m aitmation_core test "$s"
    done
  )
  if [ "$SKIP_UI" = 0 ]; then
    need npm "install Node.js 22+"
    ( cd ui; step "ui: npm ci"; npm ci --no-audit --no-fund; step "ui: build"; npm run build )
  fi
  if [ "$SKIP_LAUNCHER" = 0 ]; then
    if command -v go >/dev/null 2>&1; then
      ( cd launcher
        step "launcher: gofmt"; test -z "$(gofmt -l .)"
        step "launcher: go vet"; go vet ./...
        step "launcher: go test"; go test ./... )
    else
      step "launcher: skipped (go not installed)"
    fi
  fi
  printf '\nAll checks passed.\n'
fi

if [ "$START" = 1 ]; then
  mkdir -p "$DEV_DIR/apps"
  for src in $(cd "$REPO" && find apps docs/examples -mindepth 2 -maxdepth 2 -name solution.toml -exec dirname {} \; | sort); do
    rm -rf "$DEV_DIR/apps/$(basename "$src")"
    cp -R "$REPO/$src" "$DEV_DIR/apps/"
  done
  export AITMATION_DATA_DIR="$DEV_DIR"
  export AITMATION_TEMPLATES_DIR="$REPO/core/templates"
  export AITMATION_APPS_DIR="$DEV_DIR/apps"
  export AITMATION_PORT="$PORT"
  if [ -d "$REPO/ui/dist" ]; then export AITMATION_UI_DIR="$REPO/ui/dist"; else echo "ui/dist missing - API only"; fi
  step "starting core on http://127.0.0.1:$PORT/app/  (data: $DEV_DIR)"
  echo "    Workshop -> Solutions is in the navigation for admins. Ctrl+C stops."
  cd core && exec uv run python -m aitmation_core
fi
