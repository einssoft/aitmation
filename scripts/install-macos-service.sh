#!/usr/bin/env bash
# Register the aitmation launcher as a macOS LaunchAgent (ADR-0032 §5), so the
# household starts with the user session and is restarted when it dies.
#
#   scripts/install-macos-service.sh [--data-dir DIR] [--repo DIR] [--uninstall]
#
# Defaults: data dir ~/aitmation-data, repo = the checkout this script lives in.
# Prerequisites (once): `cd core && uv sync`, `cd ui && npm ci && npm run build`,
# `cd launcher && go build -o aitmation-launcher .`, and an initialised household
# (`aitmation-launcher init -data-dir ~/aitmation-data -admin … -private -company …`).
#
# A LaunchAgent, not a LaunchDaemon: the household lives in the user's context
# (~/aitmation-data, keychain, Homebrew PATH). Enable automatic login for that
# user so the agent comes up after a reboot without anyone at the keyboard.
set -euo pipefail

LABEL="com.aitmation.launcher"
DATA_DIR="${HOME}/aitmation-data"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --repo) REPO="$(cd "$2" && pwd)"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "this script targets macOS (launchd); on Linux use systemd" >&2
  exit 1
fi

AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST="${AGENTS_DIR}/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

if [[ "$UNINSTALL" == 1 ]]; then
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed ${LABEL} (data in ${DATA_DIR} untouched)"
  exit 0
fi

LAUNCHER="${REPO}/launcher/aitmation-launcher"
CORE_DIR="${REPO}/core"
APPS_DIR="${REPO}/apps"
missing=0
[[ -x "$LAUNCHER" ]] || { echo "missing: ${LAUNCHER}  → cd launcher && go build -o aitmation-launcher ." >&2; missing=1; }
[[ -d "${CORE_DIR}/.venv" ]] || { echo "missing: ${CORE_DIR}/.venv  → cd core && uv sync" >&2; missing=1; }
[[ -f "${REPO}/ui/dist/index.html" ]] || { echo "missing: ui/dist  → cd ui && npm ci && npm run build" >&2; missing=1; }
[[ -f "${DATA_DIR}/household.toml" ]] || { echo "missing: ${DATA_DIR}/household.toml  → ${LAUNCHER} init -data-dir ${DATA_DIR} -admin <name> -private -company \"…\"" >&2; missing=1; }
[[ "$missing" == 0 ]] || exit 1

mkdir -p "$AGENTS_DIR" "${DATA_DIR}/logs"

# Homebrew (arm64 and Intel) first: tesseract, ffmpeg, whisper-cli, uv, ollama.
SERVICE_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${LAUNCHER}</string>
    <string>run</string>
    <string>-data-dir</string><string>${DATA_DIR}</string>
    <string>-core-dir</string><string>${CORE_DIR}</string>
    <string>-apps-dir</string><string>${APPS_DIR}</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO}/launcher</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${SERVICE_PATH}</string>
    <key>HOME</key><string>${HOME}</string>
    <key>LANG</key><string>de_CH.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${DATA_DIR}/logs/launcher.log</string>
  <key>StandardErrorPath</key><string>${DATA_DIR}/logs/launcher.log</string>
</dict>
</plist>
EOF

launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "${DOMAIN}/${LABEL}"

echo "installed ${LABEL}"
echo "  plist:   ${PLIST}"
echo "  log:     ${DATA_DIR}/logs/launcher.log"
echo "  status:  launchctl print ${DOMAIN}/${LABEL} | grep -E 'state|pid'"
echo "  shell:   http://127.0.0.1:8400/app/   (via Tailscale: tailscale serve --bg --https=443 http://127.0.0.1:8400)"
echo "  stop:    launchctl bootout ${DOMAIN}/${LABEL}    remove: $0 --uninstall"
