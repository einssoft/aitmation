#!/usr/bin/env python3
"""Browser actuator for client tools (ADR-0041 §4): fulfils image tasks by
driving a web generator (Grok, ChatGPT, Gemini, ...) in a real browser.

It is the automated sibling of ``actuator.py``: same queue, same delivery
call, only the part between "show the prompt" and "hand in the file" is
done by Playwright. The service is described in a small TOML file - URL and
CSS selectors - so a changed page layout is a config edit, not a code
change. The browser profile is persistent (``profile_dir``): log in once by
hand with ``--login``, the session is reused afterwards. Nothing here knows
credentials.

    python scripts/actuator_browser.py --service grok --login      # log in once
    python scripts/actuator_browser.py --service grok --once       # one task
    python scripts/actuator_browser.py --service grok --watch      # keep going

The tool stays behind the human gate (ADR-0007): the process only reaches
the client task after the text was approved, and the generated file goes
back through a second gate before anything is published.

Requires Python 3.11+ with ``pip install playwright`` and a Chromium
(``playwright install chromium`` or ``--browser-path``). Without a system
Python, run it through the core's environment:
``cd core && uv run --with playwright python ../scripts/actuator_browser.py …``
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from actuator import call, show

DEFAULT_CONFIG = Path(__file__).resolve().parent / "actuator-browser.toml"


def load_service(config: Path, name: str) -> dict:
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    services = data.get("services", {})
    if name not in services:
        raise SystemExit(f"service {name!r} not in {config} (have: {', '.join(services) or '-'})")
    spec = dict(data.get("defaults", {}))
    spec.update(services[name])
    for key in ("url", "prompt_selector", "result_selector"):
        if key not in spec:
            raise SystemExit(f"service {name!r}: missing {key!r}")
    return spec


def prompt_text(task: dict) -> str:
    """Everything the generator should see: the prompt first, then the
    other bound inputs as labelled lines."""
    inputs = task.get("params", {}).get("inputs") or {}
    parts: list[str] = []
    if "prompt" in inputs:
        parts.append(str(inputs["prompt"]))
    for key, value in inputs.items():
        if key == "prompt":
            continue
        text = "\n".join(map(str, value)) if isinstance(value, list) else str(value)
        parts.append(f"{key}: {text}")
    return "\n\n".join(parts)


def generate(spec: dict, text: str, profile_dir: Path, *, browser_path: str | None,
             headless: bool) -> tuple[str, bytes]:
    """Open the service, submit the prompt, wait for a new image and return
    (file name, bytes). The image is fetched from its ``src``; when that is
    not possible (blob/cross-origin) the element is screenshotted."""
    from playwright.sync_api import sync_playwright

    timeout_ms = int(spec.get("wait_seconds", 120)) * 1000
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir), headless=headless, executable_path=browser_path,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(spec["url"], wait_until="domcontentloaded")
            page.wait_for_selector(spec["prompt_selector"], timeout=timeout_ms)
            before = page.locator(spec["result_selector"]).count()
            box = page.locator(spec["prompt_selector"]).first
            box.click()
            box.fill(text)
            submit = spec.get("submit", "Enter")
            if submit == "Enter":
                box.press("Enter")
            else:
                page.locator(submit).first.click()
            # A new result is one more element than before the submit.
            deadline = time.monotonic() + timeout_ms / 1000
            while page.locator(spec["result_selector"]).count() <= before:
                if time.monotonic() > deadline:
                    raise SystemExit("no new image appeared in time")
                page.wait_for_timeout(1000)
            if settle := int(spec.get("settle_seconds", 3)):
                page.wait_for_timeout(settle * 1000)
            image = page.locator(spec["result_selector"]).nth(-1)
            src = image.get_attribute("src") or ""
            data: bytes | None = None
            tag = (image.evaluate("e => e.tagName") or "").lower()
            name = "generated.mp4" if tag in ("video", "source") else "generated.png"
            if src.startswith("data:"):
                header, _, payload = src.partition(",")
                data = base64.b64decode(payload)
                if "jpeg" in header or "jpg" in header:
                    name = "generated.jpg"
            elif src.startswith("http"):
                try:
                    response = page.request.get(src, timeout=timeout_ms)
                    if response.ok:
                        data = response.body()
                        kind = response.headers.get("content-type", "")
                        if "jpeg" in kind:
                            name = "generated.jpg"
                        elif "webp" in kind:
                            name = "generated.webp"
                        elif "webm" in kind:
                            name = "generated.webm"
                except Exception:  # fall back to a screenshot
                    data = None
            if data is None:
                data = image.screenshot(type="png")
            return name, data
        finally:
            context.close()


def deliver(base: str, token: str | None, task_id: str, name: str, data: bytes) -> None:
    body = {
        "file": {"name": name, "content_base64": base64.b64encode(data).decode()},
        "data": {},
        "actor": "actuator-browser",
    }
    out = call(base, f"/api/v1/client-tasks/{task_id}/fulfil", token, body)
    print(f"delivered {name} ({len(data)} bytes) for {task_id}: {out}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", default="http://127.0.0.1:8477")
    parser.add_argument("--token")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--service", required=True)
    parser.add_argument("--profile", type=Path, help="browser profile folder (default from config)")
    parser.add_argument("--browser-path", help="Chromium executable (default: Playwright's)")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--login", action="store_true", help="open the service and wait for Enter")
    parser.add_argument("--once", action="store_true", help="fulfil one open task and exit")
    parser.add_argument("--watch", action="store_true", help="keep polling")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--kind", default="image", help="only tasks of this kind")
    args = parser.parse_args()

    spec = load_service(args.config, args.service)
    profile = args.profile or Path(spec.get("profile_dir", f"_actuator/{args.service}"))
    profile = profile if profile.is_absolute() else Path.cwd() / profile
    profile.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")

    if args.login:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(profile), headless=False, executable_path=args.browser_path
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(spec["url"])
            input("log in in the browser window, then press Enter here ... ")
            context.close()
        print(f"session stored in {profile}")
        return 0

    if not (args.once or args.watch):
        show(call(base, "/api/v1/client-tasks", args.token))
        return 0

    while True:
        tasks = [
            t for t in call(base, "/api/v1/client-tasks", args.token)
            if (t.get("params", {}).get("kind") or "image") == args.kind
            and t.get("status", "open") == "open"
        ]
        for task in tasks:
            show([task])
            # Claim first: a second actuator or a person in the inbox then
            # sees the task as taken instead of working on it too.
            try:
                call(base, f"/client-tasks/{task['id']}/claim", args.token,
                     {"actor": "actuator-browser"})
            except SystemExit as exc:
                print(f"skipped (already taken): {exc}")
                continue
            try:
                name, data = generate(
                    spec, prompt_text(task), profile,
                    browser_path=args.browser_path, headless=args.headless,
                )
                deliver(base, args.token, task["id"], name, data)
            except (SystemExit, Exception) as exc:  # keep watching
                print(f"failed for {task['id']}: {exc}")
                # Hand the failure back so the step takes its error path
                # instead of waiting for a file that never comes.
                try:
                    call(base, f"/client-tasks/{task['id']}/result", args.token,
                         {"ok": False, "result": {"error": str(exc)[:300]},
                          "actor": "actuator-browser"})
                except SystemExit as inner:
                    print(f"could not report the failure: {inner}")
                if args.once:
                    return 1
                continue
            if args.once:
                return 0
        if args.once:
            print("no open task of kind", args.kind)
            return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
