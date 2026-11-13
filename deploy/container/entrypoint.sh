#!/bin/sh
# Container start (ADR-0046 §5): mirror the shipped base solutions into the
# data volume so the shop can install next to them, then run the core.
# A solution the operator removed from the data directory comes back on
# the next start - the image is the source of the base solutions.
set -eu
DATA="${AITMATION_DATA_DIR:-/data}"
mkdir -p "$DATA/apps"
for bundle in /app/apps/*/; do
  name="$(basename "$bundle")"
  rm -rf "$DATA/apps/$name"
  cp -R "$bundle" "$DATA/apps/$name"
done
exec python -m aitmation_core "$@"
