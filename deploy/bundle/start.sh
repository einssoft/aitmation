#!/usr/bin/env bash
# Start aitmation (macOS/Linux). Python and all packages ship in the bundle
# (no network needed); at the first start a household with one private
# mandant is created and the password is shown once.
# Data lives under ~/.aitmation
cd "$(dirname "$0")"
exec ./aitmation-launcher run -core-dir "$PWD/core" -apps-dir "$PWD/apps" -auto-init -open-browser
