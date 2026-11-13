#!/usr/bin/env python3
"""Reference actuator for client tools (ADR-0041 §4).

Polls an instance for open client tasks, shows what each one asks for and
hands the result back - a file (image, video, audio) or data. This is the
plain, human-in-the-loop version; a browser automation (Chrome extension,
Playwright) replaces only the part between "show" and "deliver".

    python scripts/actuator.py --base http://127.0.0.1:8477           # list open tasks
    python scripts/actuator.py --base ... --deliver <task-id> <file>  # hand in a file
    python scripts/actuator.py --base ... --watch <folder>            # a file named
        <task-id>.<ext> dropped into the folder is delivered automatically

Behind the launcher use the member's prefix, e.g. --base https://host/m/<id>,
and pass --token <session-token> (launcher `token` command).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def call(base: str, path: str, token: str | None, body: dict | None = None) -> dict | list:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"{exc.code} on {path}: {detail}") from exc


def open_tasks(base: str, token: str | None) -> list:
    """Tasks nobody holds yet (the inbox also lists claimed ones)."""
    tasks = call(base, "/api/v1/client-tasks", token)
    return [t for t in tasks if t.get("status", "open") == "open"]


def show(tasks: list) -> None:
    if not tasks:
        print("no open client tasks")
    for task in tasks:
        params = task.get("params", {})
        name = params.get("name") or {}
        instruction = params.get("instruction") or {}
        print(f"\n[{task['id']}] {name.get('en') or name.get('de') or params.get('tool')}"
              f"  ({task.get('template_id')} · {task['step_id']} · {params.get('kind', '')})")
        print("  ", instruction.get("en") or instruction.get("de") or "")
        for key, value in (params.get("inputs") or {}).items():
            text = "\n      ".join(value) if isinstance(value, list) else str(value)
            print(f"   {key}: {text}")
        print(f"   expects: {params.get('expects', 'file')}")


def deliver(
    base: str, token: str | None, task_id: str, file: Path, data: dict | None = None
) -> None:
    encoded = base64.b64encode(file.read_bytes()).decode()
    body = {
        "file": {"name": file.name, "content_base64": encoded},
        "data": data or {},
        "actor": "actuator",
    }
    out = call(base, f"/api/v1/client-tasks/{task_id}/fulfil", token, body)
    print(f"delivered {file.name} for {task_id}: {out}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", default="http://127.0.0.1:8477")
    parser.add_argument("--token")
    parser.add_argument("--deliver", nargs=2, metavar=("TASK_ID", "FILE"))
    parser.add_argument("--watch", metavar="FOLDER")
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    if args.deliver:
        deliver(base, args.token, args.deliver[0], Path(args.deliver[1]))
        return 0
    if not args.watch:
        show(open_tasks(base, args.token))
        return 0
    folder = Path(args.watch)
    folder.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    print(f"watching {folder} - drop <task-id>.<ext> files here (Ctrl+C stops)")
    while True:
        tasks = open_tasks(base, args.token)
        for task in tasks:
            if task["id"] not in seen:
                show([task])
                seen.add(task["id"])
            for candidate in folder.glob(f"{task['id']}.*"):
                deliver(base, args.token, task["id"], candidate)
                candidate.rename(candidate.with_suffix(candidate.suffix + ".delivered"))
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
