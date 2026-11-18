"""Fail when an installed Python package carries a copyleft licence.

The project is Apache 2.0 and ships its dependencies in bundles and
images, so strong copyleft (GPL, AGPL, LGPL, SSPL, EUPL) must not enter
the dependency tree. Run inside the project environment:

    uv run python ../scripts/check_licenses.py

MPL 2.0 is accepted: it binds only the files of the package itself.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata

DENIED = re.compile(
    r"\b(AGPL|LGPL|GPL|SSPL|EUPL)\b|GNU (Affero |Lesser |Library )?General Public", re.IGNORECASE
)
# package -> why it is accepted although its metadata matches
ACCEPTED: dict[str, str] = {}


def licence_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    parts = [meta.get("License-Expression") or "", (meta.get("License") or "").splitlines()[0:1]]
    text = " ".join([parts[0], *parts[1]])
    classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    text += " " + " ".join(classifiers)
    return text.strip()


def main() -> int:
    bad = []
    for dist in metadata.distributions():
        name = dist.metadata.get("Name") or "?"
        licence = licence_of(dist)
        if DENIED.search(licence) and name.lower() not in ACCEPTED:
            bad.append(f"{name} {dist.version}: {licence[:120]}")
    for line in sorted(bad):
        print(f"copyleft licence: {line}", file=sys.stderr)
    print(f"licences: {'FAILED' if bad else 'ok'} ({len(bad)} finding(s))")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
