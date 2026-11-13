#!/usr/bin/env bash
# Start the core with the built UI and the example solutions on
# http://127.0.0.1:8477/app/ (dev data directory `_dev` next to the repo).
# Run `scripts/check.sh` first once, so ui/dist exists. Options are passed
# on; PORT=8500 scripts/start.sh changes the port. Run it, do not source it.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  echo "start.sh: run it as 'bash scripts/start.sh'." >&2
  return 2 2>/dev/null
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ ! -d "$HERE/../ui/dist" ]; then
  echo "ui/dist is missing - run 'bash scripts/check.sh' once (builds the UI), or continue API-only."
fi
exec bash "$HERE/check.sh" --start --skip-tests "$@"
