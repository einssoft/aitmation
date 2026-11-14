#!/bin/sh
# Container start (ADR-0046 §5): mirror the shipped base solutions into the
# data volume so the shop can install next to them, then run the core.
# A solution the operator removed from the data directory comes back on
# the next start - the image is the source of the base solutions.
set -eu
DATA="${AITMATION_DATA_DIR:-/data}"
if [ "${1:-}" = "host" ]; then
  # Shared host: tenants get their solutions from the image's library.
  export AITMATION_HOST_ROOT="${AITMATION_HOST_ROOT:-$DATA}"
  export AITMATION_SOLUTIONS_DIR="${AITMATION_SOLUTIONS_DIR:-/app/apps}"
  exec python -m aitmation_core "$@"
fi
mkdir -p "$DATA/apps"
: > "$DATA/apps/.managed"   # what the image placed here - not the instance's own projects
for bundle in /app/apps/*/; do
  name="$(basename "$bundle")"
  rm -rf "$DATA/apps/$name"
  cp -R "$bundle" "$DATA/apps/$name"
  echo "$name" >> "$DATA/apps/.managed"
done
exec python -m aitmation_core "$@"
