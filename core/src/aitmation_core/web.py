"""Web layer: the shell, the JSON API, the client-task queue - and nothing else.

Web UI and background work share one process (ADR-0011): the lifespan
starts an asyncio task that calls :meth:`Runtime.tick` periodically; the
blocking tick runs in a worker thread so the event loop stays responsive.

Everything a person sees is the React shell under ``/app`` (ADR-0019),
fed by the JSON API in :mod:`api`. The former HTML pages (approval inbox,
``/setup``, ``/admin``) were the transitional administration; since
2026-09-03 their last jobs - channels, addressees, keys, reload - live in
the shell, so they are gone. Configuration changes travel in JSON bodies,
never in URLs, and are applied to the running runtime immediately.

Authentication (ADR-0025) is a middleware here: with a household secret,
everything but the public paths needs a valid token; the principal
becomes the audited actor.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import secrets
import time
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, auth
from .api import build_router, ui_dist_dir
from .apps import AppError
from .config import Settings
from .doctypes import DoctypeError
from .engine import EngineError
from .mail import ImapSource, MailSource
from .model import ModelClient
from .providers import build_model_router
from .runtime import Runtime
from .secrets import resolve_secret
from .template import TemplateError

log = logging.getLogger("aitmation.web")

# -- default wiring (also used by __main__) --------------------------------


def default_model_client(settings: Settings) -> ModelClient | None:
    """Model router over the registry's released entries (models.toml or the
    built-in default); only released models are ever called - releasing a
    model is a human decision. Provider wiring and
    secrets live in :mod:`providers`."""
    return build_model_router(settings)


def default_mail_source(settings: Settings, mail_config: dict | None = None) -> MailSource | None:
    cfg = mail_config or {
        "imap_host": settings.imap_host,
        "imap_user": settings.imap_user,
        "imap_folder": settings.imap_folder,
    }
    password = resolve_secret(settings.data_dir, "imap_password")
    if not (cfg.get("imap_host") and cfg.get("imap_user") and password):
        return None
    return ImapSource(
        cfg["imap_host"], cfg["imap_user"], password, folder=cfg.get("imap_folder") or "INBOX"
    )


# Paths that stay reachable without a token when authentication is on:
# liveness, the shell itself (it shows the login), the household map the
# shell needs to find the launcher's login endpoint, and the web-form
# channel (it carries its own token).
PUBLIC_PATHS = ("/", "/healthz", "/app", "/api/v1/household", "/api/v1/intake", "/api/v1/gateway")
# Public for the middleware only: the gateway checks its own bearer token
# or a signed-in person itself (ADR-0047), so a session is still noted.
GATEWAY_PREFIX = "/api/v1/gateway"


def _is_hex(value: str) -> bool:
    return len(value) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in value)


# A sign-on link from the platform lives this long; the session it opens, a working day.
SSO_LINK_SECONDS = 120
SESSION_SECONDS = 12 * 3600


def _is_shell(path: str) -> bool:
    """What a browser needs to find out that it has to sign in."""
    return path in ("/", "/app", "/api/v1/household") or path.startswith("/app/")


def _unauthorized(reason: str) -> JSONResponse:
    return JSONResponse({"detail": reason, "login": "launcher"}, status_code=401)


def _forbidden(who: str) -> JSONResponse:
    return JSONResponse({"detail": f"{who} has no access to this instance"}, status_code=403)


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PUBLIC_PATHS)


async def _json_body(request: Request) -> dict:
    raw = (await request.body()).decode("utf-8") or "{}"
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="body must be JSON") from exc
    return body if isinstance(body, dict) else {}


def create_app(
    runtime: Runtime,
    *,
    model_factory=None,
    mail_factory=None,
    auth_secret: str | None = None,
    intake_token: str | None = None,
) -> FastAPI:
    settings = runtime.settings
    if auth_secret is None:
        auth_secret = os.environ.get("AITMATION_AUTH_SECRET") or None
    model_factory = model_factory or default_model_client
    mail_factory = mail_factory or default_mail_source

    def rebuild_clients() -> None:
        runtime.set_model_client(model_factory(settings))
        runtime.set_mail_source(mail_factory(settings, runtime.mail_config))

    # The JSON API (models, secrets, channels) changes what the clients are
    # built from; it asks the runtime to rebuild through this hook.
    runtime.reconfigure_hooks.append(rebuild_clients)
    runtime.mail_factory = mail_factory  # connection tests build a throwaway source

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        stop = asyncio.Event()

        async def scheduler() -> None:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(runtime.tick)
                except Exception as exc:  # one bad tick must not end the scheduler
                    log.exception("tick failed")
                    runtime.diag(
                        "error", "scheduler", "tick_failed", f"{exc.__class__.__name__}: {exc}"
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=runtime.settings.poll_interval_seconds
                    )

        task = asyncio.create_task(scheduler())
        # Search index and embedding model ready before the first question.
        warm = asyncio.create_task(asyncio.to_thread(runtime.warm_knowledge))
        try:
            yield
        finally:
            stop.set()
            # uvicorn re-raises the captured SIGINT after shutdown; the loop
            # then cancels us mid-await. That is the normal end, not an error.
            with contextlib.suppress(asyncio.CancelledError):
                await task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await warm
            runtime.close()

    app = FastAPI(title="aitmation core", version=__version__, lifespan=lifespan)
    app.state.auth_secret = auth_secret
    app.state.auth_required = bool(auth_secret)
    # Web-form channel token: an explicit value (tests, launcher) wins; else
    # the endpoint resolves it per request from the environment or the
    # secrets file, so the UI can issue one without a restart.
    app.state.intake_token = intake_token
    # The household shell (ui/) talks to every instance on this machine
    # (ADR-0024); allow loopback origins only - nothing else is a peer.
    # Credentials are allowed so the household's session cookie travels.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    app.include_router(build_router(runtime))

    # Two doors, either of which lets a request in:
    #  - a session token (ADR-0025): a person, signed in at the launcher of
    #    a household or - for a hosted instance - at the platform, which
    #    hands the browser over through /sso below;
    #  - the access key (ADR-0046 §2, ``AITMATION_ACCESS_KEY="name:password"``):
    #    one shared lock for a container without launcher - Basic auth for a
    #    browser, ``Authorization: Bearer <password>`` for programs (the
    #    assistant, the gateway's callers). Not a user model.
    access_key = os.environ.get("AITMATION_ACCESS_KEY", "").strip()
    key_user, _, key_password = access_key.partition(":")
    has_key = bool(access_key and key_password)
    secret_bytes = (
        (bytes.fromhex(auth_secret) if _is_hex(auth_secret) else auth_secret.encode())
        if auth_secret else b""
    )
    app.state.login_url = os.environ.get("AITMATION_LOGIN_URL", "").strip()

    def _access_ok(header: str) -> bool:
        if not has_key:
            return False
        if header.lower().startswith("basic "):
            try:
                given = base64.b64decode(header[6:].strip()).decode()
            except (ValueError, UnicodeDecodeError):
                return False
            return secrets.compare_digest(given, f"{key_user}:{key_password}")
        if header.lower().startswith("bearer "):
            return secrets.compare_digest(header[7:].strip(), key_password)
        return False

    def _session(request: Request) -> auth.Principal | None:
        """The signed-in person of this request, if the token is valid and
        may see this instance."""
        if not secret_bytes:
            return None
        token = auth.token_from_request(request.headers, request.cookies)
        if token is None:
            return None
        try:
            principal = auth.verify(secret_bytes, token)
        except auth.AuthError:
            return None
        if not principal.may_access(settings.instance_id, settings.instance_type):
            return None
        return principal

    app.state.session_of = _session

    if has_key or secret_bytes:

        @app.middleware("http")
        async def require_door(request: Request, call_next):
            path = request.url.path
            if path in ("/healthz", "/sso") or request.method == "OPTIONS":
                return await call_next(request)
            if _access_ok(request.headers.get("authorization", "")):
                request.state.access_key = True
                return await call_next(request)
            principal = _session(request)
            if principal is not None:
                request.state.principal = principal
                return await call_next(request)
            if has_key and not secret_bytes:
                return Response(
                    "aitmation: access key required", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="aitmation"'},
                )
            # With sign-in configured the public paths stay open. Behind an
            # access key only the shell does, and only when a platform
            # login is known to send the browser to - token-guarded
            # endpoints (intake, gateway) stay behind the key.
            if _is_public(path) and not has_key:
                return await call_next(request)
            if has_key and app.state.login_url and _is_shell(path):
                return await call_next(request)
            if has_key and not app.state.login_url:
                return Response(
                    "aitmation: access key required", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="aitmation"'},
                )
            token = auth.token_from_request(request.headers, request.cookies)
            if token is None:
                return _unauthorized("not signed in")
            try:
                principal = auth.verify(secret_bytes, token)
            except auth.AuthError as exc:
                return _unauthorized(str(exc))
            return _forbidden(principal.sub)

    @app.get("/sso")
    def sso(request: Request, token: str = "", next: str = "/app/") -> Response:
        """Hand-over from the platform's sign-in (hosted instance): a
        short-lived token signed with this instance's secret is traded for
        the session cookie. The link works once per minute it lives; what
        ends up in the cookie is a fresh token for the same person."""
        if not secret_bytes:
            raise HTTPException(404, "sign-in is not configured for this instance")
        try:
            principal = auth.verify(secret_bytes, token)
        except auth.AuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        now = int(time.time())
        if principal.exp - now > SSO_LINK_SECONDS:
            raise HTTPException(401, "not a sign-on link")
        if not principal.may_access(settings.instance_id, settings.instance_type):
            raise HTTPException(403, "this account has no access to this instance")
        session = auth.sign(
            secret_bytes,
            auth.Principal(principal.sub, principal.role, principal.instances,
                           now + SESSION_SECONDS),
        )
        target = next if next.startswith("/app") and "//" not in next else "/app/"
        response = RedirectResponse(target, status_code=303)
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(auth.COOKIE_NAME, session, max_age=SESSION_SECONDS, httponly=True,
                            samesite="lax", secure=secure, path="/")
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    # Built React shell, when present (ADR-0019): /app and /app/<route>.
    dist = ui_dist_dir(settings)
    if dist is not None:
        app.mount("/app/assets", StaticFiles(directory=dist / "assets"), name="ui-assets")

        @app.get("/app")
        @app.get("/app/{path:path}")
        def ui_shell(path: str = "") -> FileResponse:
            candidate = (dist / path) if path else None
            if candidate is not None and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    @app.get("/")
    def root():
        if dist is not None:
            return RedirectResponse("/app/", status_code=307)
        return {
            "name": "aitmation core",
            "version": __version__,
            "api": "/api/v1",
            "ui": "not built - run `npm run build` in ui/ or set AITMATION_UI_DIR",
        }

    # -- client task queue (K5): polled by external actuators --------------

    @app.get("/client-tasks")
    def client_tasks() -> list[dict]:
        return runtime.open_client_tasks()

    @app.post("/client-tasks/{task_id}/claim")
    async def claim_client_task(task_id: str, request: Request) -> dict:
        # Actuators are small scripts: accept the actor as JSON, as a form
        # field or as a query parameter.
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            body = await _json_body(request)
        elif content_type.startswith("application/x-www-form-urlencoded"):
            raw = (await request.body()).decode("utf-8", errors="replace")
            body = {k: v[0] for k, v in parse_qs(raw).items()}
        else:
            body = {}
        actor = str(body.get("actor") or request.query_params.get("actor") or "client")
        try:
            return runtime.claim_client_task(task_id, actor_id=actor)
        except EngineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/client-tasks/{task_id}/result")
    async def client_task_result(task_id: str, request: Request) -> dict:
        body = await _json_body(request)
        try:
            runtime.submit_client_task_result(
                task_id,
                ok=bool(body.get("ok", False)),
                result=body.get("result"),
                actor_id=str(body.get("actor", "client")),
            )
        except EngineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "recorded"}

    @app.get("/cases/{case_id}/audit")
    def audit(case_id: str) -> list[dict]:
        rows = runtime.conn.execute(
            "SELECT seq, at, actor_type, actor_id, event, details, app_version, model_ref"
            " FROM audit_log WHERE case_id = ? ORDER BY seq",
            (case_id,),
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="unknown case")
        return [
            {**dict(r), "details": json.loads(r["details"]) if r["details"] else None} for r in rows
        ]

    @app.post("/admin/reload")
    def admin_reload() -> dict:
        """Re-scan templates, document types, records and the registry
        (the launcher calls this after writing addressees.toml)."""
        try:
            definitions = runtime.reload_templates()
        except (TemplateError, DoctypeError, AppError) as exc:
            log.error("template reload failed: %s", exc)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, "templates": sorted(definitions)}

    return app
