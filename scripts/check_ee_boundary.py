#!/usr/bin/env python3
"""License boundary (ADR-0009, ADR-0043): nothing outside the ee/ folder
may import the commercial package, which must stay optional - the open
core runs without it. Exit 1 with the offending lines."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "node_modules", "dist", "__pycache__", "_to_delete", "ee"}
PATTERN = re.compile(r"^\s*(?:from\s+(?:aitmation_ee|ee)\b|import\s+(?:aitmation_ee|ee)\b)")
SELF = Path(__file__).resolve()
TS_PATTERN = re.compile(r"""from\s+['"](?:@aitmation/ee|[./]*ee)(?:/|['"])""")


def main() -> int:
    bad: list[str] = []
    for file in ROOT.rglob("*"):
        if not file.is_file() or any(part in SKIP for part in file.relative_to(ROOT).parts):
            continue
        if file.suffix not in {".py", ".ts", ".tsx", ".go"} or file == SELF:
            continue
        pattern = PATTERN if file.suffix == ".py" else TS_PATTERN
        text = file.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                bad.append(f"{file.relative_to(ROOT)}:{number}: {line.strip()}")
    if bad:
        print("open code imports from ee/ (license boundary, ADR-0009):")
        print("\n".join(f"  {b}" for b in bad))
        return 1
    print("ee boundary ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
