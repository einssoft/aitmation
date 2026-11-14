"""Shared host: many instances in one process.

One installation can serve many tenants without a container each. The
core stays what it is - a ``Runtime`` works on one data directory and
knows nothing of tenants - and this thin host keeps one runtime per
tenant, each on a directory of its own::

    <root>/tenants/<slug>/tenant.toml   what the host knows (title, role of
                                         the directory, booked solutions)
    <root>/tenants/<slug>/data/         the instance: database, files,
                                         knowledge, secrets, own projects
    <root>/models/                      the embedding model, once for all
    <root>/removed/                     retired tenants (never deleted here)

Requests for ``/m/<slug>/…`` go to that tenant's application - the same
address scheme the launcher uses for a household, so the shell needs
nothing new. Separation is physical (one database file per tenant): a
backup, an export or a move to an installation of its own is a copy of
one directory.

What sharing a process demands, and where it is enforced:

- **Secrets.** The process environment is the operator's. Instances read
  their secrets from their own file only (``secrets.ignore_environment``);
  the operator lends exactly the model declared by ``LLM_*`` (address
  fixed) and the transcription token.
- **Switches.** Local MCP servers and private hosts stay off, configured
  paths stay inside the instance (``confine_paths``); the host sets these
  through the settings' environment, which wins over the instance's file.
- **Identity.** One sign-in for several tenants: the token carries a role
  per instance (``auth.Principal.roles``) and every instance accepts only
  a role granted for itself. Who may sign in is not the host's business -
  an identity provider (the platform) mints the short hand-over token.
- **Time.** One clock for all: the host ticks its loaded instances, a few
  at a time, and puts quiet ones to sleep until someone asks for them.
- **Paths.** A slug becomes a directory in one function (`Host.directory`).

``python -m aitmation_core host`` runs it. Environment:
``AITMATION_HOST_ROOT`` (default ``<AITMATION_DATA_DIR>``),
``AITMATION_AUTH_SECRET`` (sessions; required), ``AITMATION_HOST_KEY``
(the operator's key for ``/host/…``; required), ``AITMATION_LOGIN_URL``
(where people sign in), ``AITMATION_SOLUTIONS_DIR`` (the library of
solutions tenants can be given), ``AITMATION_HOST_BASE_SOLUTIONS``
(comma list every new tenant starts with), ``AITMATION_HOST_IDLE_SECONDS``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import re
import secrets as py_secrets
import shutil
import sqlite3
import tarfile
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, auth
from . import secrets as core_secrets
from .config import Settings, load_settings
from .runtime import Runtime
from .web import (
    SESSION_SECONDS,
    SSO_LINK_SECONDS,
    create_app,
    default_mail_source,
    default_model_client,
)

log = logging.getLogger("aitmation.host")

SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
TYPES = ("company", "private")
PROXY_HEADER = b"x-aitmation-proxy"

# Operator settings every instance shares (read from the host's
# environment as AITMATION_<NAME>): services and policies, never anything
# that names a place of one tenant.
SHARED_SETTINGS = (
    "templates_dir",
    "ui_dir",
    "poll_interval_seconds",
    "model_cheap",
    "model_strong",
    "model_confidence_threshold",
    "log_retention_days",
    "tesseract_path",
    "ocr_languages",
    "ocr_vision_escalation",
    "guardrails_pre",
    "guardrails_post",
    "max_model_calls_per_case",
    "max_read_calls_per_case",
    "knowledge_crawl_delay_seconds",
    "mail_max_attachment_mb",
    "catalog_url",
    "trusted_levels",
    "whisper_backend",
    "whisper_model",
    "whisper_url",
    "whisper_cpp_path",
    "whisper_cpp_model",
    "ffmpeg_path",
    "video_frames",
    "embeddings",
    "embedding_model",
)
LENT_SECRETS = ("whisper_token",)


class HostError(Exception):
    """A request the host cannot serve; ``status`` is the HTTP answer."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Tenant:
    slug: str
    title: str = ""
    type: str = "company"
    language: str = "de"
    enabled: bool = True
    solutions: tuple[str, ...] = ()
    # CHF per month of the operator's model; 0 = no limit set here.
    budget_chf: float = 0.0
    created_at: str = ""

    def view(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title or self.slug,
            "type": self.type,
            "language": self.language,
            "enabled": self.enabled,
            "solutions": list(self.solutions),
            "budget_chf": self.budget_chf,
            "created_at": self.created_at,
        }


def _toml_string(value: str) -> str:
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + out.replace("\n", " ").replace("\r", " ").replace("\t", " ") + '"'


def _write_tenant(path: Path, tenant: Tenant) -> None:
    lines = [
        "# Managed by the aitmation host. The instance's own data lies in data/.",
        f"title = {_toml_string(tenant.title)}",
        f"type = {_toml_string(tenant.type)}",
        f"language = {_toml_string(tenant.language)}",
        f"enabled = {'true' if tenant.enabled else 'false'}",
        "solutions = [" + ", ".join(_toml_string(s) for s in tenant.solutions) + "]",
        f"budget_chf = {float(tenant.budget_chf)}",
        f"created_at = {_toml_string(tenant.created_at)}",
    ]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_tenant(slug: str, path: Path) -> Tenant:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Tenant(
        slug=slug,
        title=str(data.get("title", "")),
        type=str(data.get("type", "company")),
        language=str(data.get("language", "de")),
        enabled=bool(data.get("enabled", True)),
        solutions=tuple(str(s) for s in data.get("solutions", []) or []),
        budget_chf=float(data.get("budget_chf", 0.0) or 0.0),
        created_at=str(data.get("created_at", "")),
    )


def needs_clock(data_dir: Path) -> bool:
    """Whether a sleeping instance has anything the clock must drive - a
    look into its files without starting it (the counterpart of
    ``Runtime.quiescent``). Unsure means yes."""
    config = data_dir / "config.toml"
    if config.is_file():
        try:
            if tomllib.loads(config.read_text(encoding="utf-8")).get("imap_host"):
                return True
        except (tomllib.TOMLDecodeError, OSError):
            return True
    db = data_dir / "aitmation.db"
    candidates = [db] if db.is_file() else sorted(data_dir.glob("*.db"))
    if not candidates:
        return False
    try:
        conn = sqlite3.connect(f"file:{candidates[0]}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return True
    try:
        for query in (
            "SELECT 1 FROM cases WHERE status = 'running' LIMIT 1",
            "SELECT 1 FROM schedules LIMIT 1",
            "SELECT 1 FROM knowledge_sources LIMIT 1",
            "SELECT 1 FROM client_tasks WHERE status IN ('open', 'claimed') LIMIT 1",
        ):
            if conn.execute(query).fetchone() is not None:
                return True
    except sqlite3.Error:
        return True  # an older or unreadable schema: let the runtime sort it out
    finally:
        conn.close()
    return False


@dataclass
class Loaded:
    tenant: Tenant
    runtime: Runtime
    app: FastAPI
    last_used: float = field(default_factory=time.monotonic)
    ticking: bool = False
    requests: int = 0


class Host:
    def __init__(
        self,
        root: Path,
        *,
        auth_secret: str,
        host_key: str,
        login_url: str = "",
        solutions_dir: Path | None = None,
        base_solutions: tuple[str, ...] = (),
        idle_seconds: float = 900.0,
        poll_seconds: float = 2.0,
        parallel_ticks: int = 4,
        max_tenants: int = 0,
        env: dict[str, str] | None = None,
        model_factory: Callable | None = None,
        mail_factory: Callable | None = None,
    ) -> None:
        if not auth_secret or not host_key:
            raise HostError("the host needs AITMATION_AUTH_SECRET and AITMATION_HOST_KEY")
        self.root = Path(root)
        self.tenants_dir = self.root / "tenants"
        self.removed_dir = self.root / "removed"
        self.models_dir = self.root / "models"
        for d in (self.tenants_dir, self.removed_dir, self.models_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.auth_secret = auth_secret
        self._secret_bytes = (
            bytes.fromhex(auth_secret)
            if len(auth_secret) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in auth_secret)
            else auth_secret.encode()
        )
        self.host_key = host_key
        self.login_url = login_url.strip()
        self.solutions_dir = Path(solutions_dir) if solutions_dir else None
        self.base_solutions = tuple(base_solutions)
        self.idle_seconds = idle_seconds
        self.poll_seconds = poll_seconds
        self.parallel_ticks = max(1, parallel_ticks)
        self.max_tenants = max_tenants
        self.env = dict(os.environ if env is None else env)
        self.model_factory = model_factory or default_model_client
        self.mail_factory = mail_factory or default_mail_source
        self._loaded: dict[str, Loaded] = {}
        self._lock = threading.RLock()
        # The environment is the operator's from here on (module docstring).
        core_secrets.ignore_environment(True, lend=LENT_SECRETS)
        self.app = self._build_root()

    # -- tenants on disk ---------------------------------------------------------

    def directory(self, slug: str) -> Path:
        """The one place where a slug becomes a path."""
        if not isinstance(slug, str) or not SLUG.match(slug):
            raise HostError("slug: lowercase letters, digits and hyphens, 1 to 40", 404)
        path = self.tenants_dir / slug
        if path.resolve().parent != self.tenants_dir.resolve():
            raise HostError("slug: not a tenant directory", 404)
        return path

    def get(self, slug: str) -> Tenant:
        path = self.directory(slug) / "tenant.toml"
        if not path.is_file():
            raise HostError(f"no tenant {slug!r}", 404)
        return _read_tenant(slug, path)

    def tenants(self) -> list[Tenant]:
        out = []
        for path in sorted(self.tenants_dir.iterdir()):
            if SLUG.match(path.name) and (path / "tenant.toml").is_file():
                try:
                    out.append(_read_tenant(path.name, path / "tenant.toml"))
                except (tomllib.TOMLDecodeError, ValueError, OSError) as exc:
                    log.error("tenant %s unreadable: %s", path.name, exc)
        return out

    def library(self) -> list[dict]:
        """Solutions a tenant can be given: the bundles of the library."""
        if self.solutions_dir is None or not self.solutions_dir.is_dir():
            return []
        out = []
        for path in sorted(self.solutions_dir.iterdir()):
            manifest = path / "solution.toml"
            if not manifest.is_file():
                continue
            try:
                data = tomllib.loads(manifest.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, OSError):
                continue
            out.append(
                {
                    "id": path.name,
                    "title": data.get("title", path.name),
                    "version": str(data.get("version", "")),
                    "dependencies": sorted((data.get("dependencies") or {}).keys())
                    if isinstance(data.get("dependencies"), dict)
                    else [str(d) for d in data.get("dependencies") or []],
                }
            )
        return out

    def _with_dependencies(self, wanted: tuple[str, ...]) -> tuple[str, ...]:
        """The wanted solutions plus what they depend on, dependencies first."""
        known = {s["id"]: s for s in self.library()}
        ordered: list[str] = []

        def add(solution_id: str, trail: tuple[str, ...]) -> None:
            if solution_id in ordered:
                return
            if solution_id not in known:
                raise HostError(f"solution {solution_id!r} is not in the library")
            if solution_id in trail:
                raise HostError(f"solution {solution_id!r} depends on itself")
            for dep in known[solution_id]["dependencies"]:
                add(dep, (*trail, solution_id))
            ordered.append(solution_id)

        for solution_id in wanted:
            add(solution_id, ())
        return tuple(ordered)

    def save(self, slug: str, values: dict) -> Tenant:
        """Create a tenant or change what the host knows about one."""
        directory = self.directory(slug)
        path = directory / "tenant.toml"
        with self._lock:
            if path.is_file():
                tenant = _read_tenant(slug, path)
            else:
                if self.max_tenants and len(self.tenants()) >= self.max_tenants:
                    raise HostError("this host is full", 409)
                tenant = Tenant(
                    slug=slug,
                    solutions=self.base_solutions,
                    created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
            unknown = set(values) - {
                "title",
                "type",
                "language",
                "enabled",
                "solutions",
                "budget_chf",
            }
            if unknown:
                raise HostError(f"unknown fields {sorted(unknown)}")
            if "title" in values:
                tenant.title = str(values["title"]).strip()[:120]
            if "type" in values:
                if values["type"] not in TYPES:
                    raise HostError(f"type: one of {', '.join(TYPES)}")
                tenant.type = values["type"]
            if "language" in values:
                if values["language"] not in ("de", "fr", "it", "en"):
                    raise HostError("language: de, fr, it or en")
                tenant.language = values["language"]
            if "enabled" in values:
                tenant.enabled = bool(values["enabled"])
            if "budget_chf" in values:
                budget = float(values["budget_chf"] or 0)
                if budget < 0:
                    raise HostError("budget_chf: not negative")
                tenant.budget_chf = budget
            if "solutions" in values:
                wanted = values["solutions"]
                if not isinstance(wanted, list | tuple) or not all(
                    isinstance(s, str) for s in wanted
                ):
                    raise HostError("solutions: a list of ids")
                tenant.solutions = tuple(wanted)
            tenant.solutions = self._with_dependencies(tenant.solutions)
            (directory / "data").mkdir(parents=True, exist_ok=True)
            _write_tenant(path, tenant)
            self.unload(slug)  # the next request starts it with what is true now
        return tenant

    def retire(self, slug: str) -> Path:
        """Take a tenant out of service. Its directory moves aside - the
        host never deletes a customer's data on its own."""
        directory = self.directory(slug)
        if not (directory / "tenant.toml").is_file():
            raise HostError(f"no tenant {slug!r}", 404)
        with self._lock:
            self.unload(slug)
            target = self.removed_dir / f"{slug}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
            shutil.move(str(directory), str(target))
        return target

    def export(self, slug: str) -> bytes:
        """The tenant as one archive: what an installation of its own
        would start from. The instance sleeps while it is read."""
        directory = self.directory(slug)
        if not (directory / "tenant.toml").is_file():
            raise HostError(f"no tenant {slug!r}", 404)
        with self._lock:
            self.unload(slug)
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                archive.add(directory / "data", arcname="data")
        return buffer.getvalue()

    # -- instances in memory -----------------------------------------------------

    def settings_for(self, tenant: Tenant) -> Settings:
        data = self.directory(tenant.slug) / "data"
        env = {"AITMATION_DATA_DIR": str(data)}
        for name in SHARED_SETTINGS:
            key = f"AITMATION_{name.upper()}"
            if self.env.get(key):
                env[key] = self.env[key]
        env.update(
            {
                "AITMATION_INSTANCE_ID": tenant.slug,
                "AITMATION_INSTANCE_TITLE": tenant.title or tenant.slug,
                "AITMATION_INSTANCE_TYPE": tenant.type,
                "AITMATION_LANGUAGE": tenant.language,
                "AITMATION_EMBEDDING_CACHE_DIR": str(self.models_dir),
                # What sharing a process forbids - the environment wins over
                # anything the instance's own file says.
                "AITMATION_TOOLS_ALLOW_PRIVATE_HOSTS": "false",
                "AITMATION_MCP_ALLOW_STDIO": "false",
                "AITMATION_CONFINE_PATHS": "true",
            }
        )
        if tenant.budget_chf > 0:
            env["AITMATION_MONTHLY_BUDGET_CHF"] = str(tenant.budget_chf)
            env["AITMATION_BUDGET_STOP"] = "true"
        return load_settings(env)

    def _mirror_solutions(self, tenant: Tenant, settings: Settings) -> None:
        """Bring the tenant's booked solutions to the library's state. The
        library is their source; what the tenant built itself is left alone,
        and a solution no longer booked goes away."""
        if self.solutions_dir is None:
            return
        marker = settings.apps_dir / ".managed"
        previous = set(marker.read_text(encoding="utf-8").split()) if marker.is_file() else set()
        for gone in previous - set(tenant.solutions):
            if SLUG.match(gone):
                shutil.rmtree(settings.apps_dir / gone, ignore_errors=True)
        for solution_id in tenant.solutions:
            source = self.solutions_dir / solution_id
            if not SLUG.match(solution_id) or not (source / "solution.toml").is_file():
                log.error("tenant %s: solution %s is not in the library", tenant.slug, solution_id)
                continue
            target = settings.apps_dir / solution_id
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(source, target, symlinks=False)
        marker.write_text("\n".join(tenant.solutions) + "\n", encoding="utf-8")

    def load(self, slug: str) -> Loaded:
        """The running instance of a tenant, started on first use."""
        with self._lock:
            loaded = self._loaded.get(slug)
            if loaded is not None:
                loaded.last_used = time.monotonic()
                return loaded
            tenant = self.get(slug)
            if not tenant.enabled:
                raise HostError(f"tenant {slug!r} is switched off", 404)
            settings = self.settings_for(tenant)
            settings.ensure_layout()
            self._mirror_solutions(tenant, settings)
            runtime = Runtime(
                settings,
                model_client=self.model_factory(settings),
                mail_source=self.mail_factory(settings),
            )
            app = create_app(
                runtime,
                model_factory=self.model_factory,
                mail_factory=self.mail_factory,
                auth_secret=self.auth_secret,
                access_key="",
                login_url=self.login_url,
                scoped_roles=True,
                lifespan_tasks=False,
            )
            loaded = Loaded(tenant=tenant, runtime=runtime, app=app)
            self._loaded[slug] = loaded
            log.info("tenant %s started (%d in memory)", slug, len(self._loaded))
            return loaded

    def unload(self, slug: str) -> bool:
        with self._lock:
            loaded = self._loaded.pop(slug, None)
        if loaded is None:
            return False
        deadline = time.monotonic() + 30
        while (loaded.ticking or loaded.requests) and time.monotonic() < deadline:
            time.sleep(0.05)
        try:
            loaded.runtime.close()
        except Exception:
            log.exception("tenant %s did not close cleanly", slug)
        return True

    def close(self) -> None:
        for slug in list(self._loaded):
            self.unload(slug)

    def loaded(self) -> list[str]:
        with self._lock:
            return sorted(self._loaded)

    # -- time ----------------------------------------------------------------------

    def wake_due(self) -> int:
        """Start the sleeping tenants that have work for the clock."""
        started = 0
        for tenant in self.tenants():
            if not tenant.enabled or tenant.slug in self._loaded:
                continue
            if needs_clock(self.directory(tenant.slug) / "data"):
                try:
                    self.load(tenant.slug)
                    started += 1
                except Exception:
                    log.exception("tenant %s did not start", tenant.slug)
        return started

    def sleep_idle(self) -> int:
        """Close the instances nobody asked for in a while and that have
        nothing the clock must drive."""
        now, asleep = time.monotonic(), 0
        for slug in self.loaded():
            loaded = self._loaded.get(slug)
            if loaded is None or loaded.ticking or loaded.requests:
                continue
            if now - loaded.last_used < self.idle_seconds:
                continue
            try:
                quiet = loaded.runtime.quiescent()
            except Exception:
                quiet = False
            if quiet and self.unload(slug):
                asleep += 1
        return asleep

    def _tick_one(self, loaded: Loaded) -> None:
        try:
            loaded.runtime.tick()
        except Exception as exc:  # one tenant's bad tick is that tenant's alone
            log.exception("tick failed for %s", loaded.tenant.slug)
            with contextlib.suppress(Exception):
                loaded.runtime.diag(
                    "error", "scheduler", "tick_failed", f"{exc.__class__.__name__}: {exc}"
                )
        finally:
            loaded.ticking = False

    async def run_clock(self, stop: asyncio.Event) -> None:
        gate = asyncio.Semaphore(self.parallel_ticks)
        rounds = 0

        async def tick(loaded: Loaded) -> None:
            async with gate:
                await asyncio.to_thread(self._tick_one, loaded)

        pending: set[asyncio.Task] = set()
        await asyncio.to_thread(self.wake_due)
        while not stop.is_set():
            for slug in self.loaded():
                loaded = self._loaded.get(slug)
                if loaded is None or loaded.ticking:
                    continue
                loaded.ticking = True  # a tenant's ticks never overlap
                task = asyncio.create_task(tick(loaded))
                pending.add(task)
                task.add_done_callback(pending.discard)
            rounds += 1
            if rounds % 30 == 0:
                await asyncio.to_thread(self.sleep_idle)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.poll_seconds)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # -- the web ---------------------------------------------------------------------

    def principal_of(self, request: Request) -> auth.Principal | None:
        token = auth.token_from_request(request.headers, request.cookies)
        if token is None:
            return None
        try:
            return auth.verify(self._secret_bytes, token)
        except auth.AuthError:
            return None

    def _operator(self, request: Request) -> None:
        header = request.headers.get("authorization", "")
        given = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not given or not py_secrets.compare_digest(given, self.host_key):
            raise HTTPException(status_code=401, detail="the host key is required")

    def _build_root(self) -> FastAPI:
        host = self

        @contextlib.asynccontextmanager
        async def lifespan(_: FastAPI):
            stop = asyncio.Event()
            clock = asyncio.create_task(host.run_clock(stop))
            try:
                yield
            finally:
                stop.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await clock
                await asyncio.to_thread(host.close)

        app = FastAPI(title="aitmation host", version=__version__, lifespan=lifespan)

        @app.exception_handler(HostError)
        async def host_error(_: Request, exc: HostError) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=exc.status)

        @app.get("/healthz")
        def healthz() -> dict:
            return {"status": "ok", "version": __version__, "host": True}

        @app.get("/api/v1/household")
        def household(request: Request) -> dict:
            """What the shell asks first: who is signed in and which
            instances they may open (the organisation as they see it)."""
            principal = host.principal_of(request)
            members, roles = [], {}
            if principal is not None:
                for tenant in host.tenants():
                    scoped = principal.scoped(tenant.slug)
                    if scoped is None or not tenant.enabled:
                        continue
                    roles[tenant.slug] = scoped.role
                    members.append(
                        {
                            "id": tenant.slug,
                            "title": tenant.title or tenant.slug,
                            "type": tenant.type,
                            "url": f"/m/{tenant.slug}",
                        }
                    )
            return {
                "self": "",
                "members": members,
                "launcher": None,
                "auth_required": True,
                "principal": None
                if principal is None
                else {
                    "sub": principal.sub,
                    "role": "admin" if "admin" in roles.values() else "member",
                    "instances": sorted(roles),
                    "roles": roles,
                    "exp": principal.exp,
                },
                "login_url": host.login_url or None,
            }

        @app.get("/sso")
        def sso(request: Request, token: str = "", next: str = "/app/") -> Response:
            """Hand-over from the identity provider: a token that lives two
            minutes is traded for the session of a working day."""
            try:
                principal = auth.verify(host._secret_bytes, token)
            except auth.AuthError as exc:
                raise HTTPException(401, str(exc)) from exc
            now = int(time.time())
            if principal.exp - now > SSO_LINK_SECONDS:
                raise HTTPException(401, "not a sign-on link")
            if not principal.roles:
                raise HTTPException(403, "this account has no instance on this host")
            session = auth.sign(
                host._secret_bytes,
                auth.Principal(
                    principal.sub,
                    "member",
                    tuple(principal.roles),
                    now + SESSION_SECONDS,
                    principal.roles,
                ),
            )
            target = next if next.startswith("/app") and "//" not in next else "/app/"
            response = RedirectResponse(target, status_code=303)
            secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
            response.set_cookie(
                auth.COOKIE_NAME,
                session,
                max_age=SESSION_SECONDS,
                httponly=True,
                samesite="lax",
                secure=secure,
                path="/",
            )
            return response

        @app.post("/logout")
        @app.get("/logout")
        def logout() -> Response:
            response = RedirectResponse(host.login_url or "/app/", status_code=303)
            response.delete_cookie(auth.COOKIE_NAME, path="/")
            return response

        # -- the operator's side (the platform calls these) -------------------

        @app.get("/host/status")
        def status(request: Request) -> dict:
            host._operator(request)
            tenants = host.tenants()
            return {
                "version": __version__,
                "tenants": len(tenants),
                "enabled": sum(1 for t in tenants if t.enabled),
                "in_memory": host.loaded(),
                "max_tenants": host.max_tenants,
            }

        @app.get("/host/solutions")
        def solutions(request: Request) -> list[dict]:
            host._operator(request)
            return host.library()

        @app.get("/host/tenants")
        def tenant_list(request: Request) -> list[dict]:
            host._operator(request)
            awake = set(host.loaded())
            return [{**t.view(), "in_memory": t.slug in awake} for t in host.tenants()]

        @app.get("/host/tenants/{slug}")
        def tenant_get(slug: str, request: Request) -> dict:
            host._operator(request)
            return {**host.get(slug).view(), "in_memory": slug in host.loaded()}

        @app.put("/host/tenants/{slug}")
        async def tenant_put(slug: str, request: Request) -> dict:
            host._operator(request)
            try:
                body = await request.json()
            except ValueError as exc:
                raise HTTPException(400, "body must be JSON") from exc
            if not isinstance(body, dict):
                raise HTTPException(400, "body must be an object")
            tenant = await asyncio.to_thread(host.save, slug, body)
            return tenant.view()

        @app.delete("/host/tenants/{slug}")
        async def tenant_delete(slug: str, request: Request) -> dict:
            host._operator(request)
            target = await asyncio.to_thread(host.retire, slug)
            return {"retired": slug, "kept_as": target.name}

        @app.get("/host/tenants/{slug}/export")
        async def tenant_export(slug: str, request: Request) -> Response:
            host._operator(request)
            data = await asyncio.to_thread(host.export, slug)
            return Response(
                data,
                media_type="application/gzip",
                headers={"Content-Disposition": f'attachment; filename="{slug}.tgz"'},
            )

        # -- the shell -----------------------------------------------------------

        dist = None
        raw = self.env.get("AITMATION_UI_DIR", "")
        for candidate in ([Path(raw)] if raw else []) + [
            Path(__file__).resolve().parents[3] / "ui" / "dist",
        ]:
            if (candidate / "index.html").is_file():
                dist = candidate
                break
        if dist is not None:
            if (dist / "assets").is_dir():
                app.mount("/app/assets", StaticFiles(directory=dist / "assets"), name="ui-assets")

            @app.get("/app")
            @app.get("/app/{path:path}")
            def ui_shell(path: str = "") -> FileResponse:
                candidate = (dist / path).resolve() if path else None
                if (
                    candidate is not None
                    and dist.resolve() in candidate.parents
                    and candidate.is_file()
                ):
                    return FileResponse(candidate)
                return FileResponse(dist / "index.html")

        @app.get("/")
        def root() -> Response:
            if dist is not None:
                return RedirectResponse("/app/", status_code=307)
            return JSONResponse({"service": "aitmation host", "version": __version__})

        return app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/m/"):
            await self.app(scope, receive, send)
            return
        slug, _, rest = scope["path"][3:].partition("/")
        try:
            loaded = await asyncio.to_thread(self.load, slug)
        except HostError as exc:
            await JSONResponse({"detail": str(exc)}, status_code=exc.status)(scope, receive, send)
            return
        except Exception:
            log.exception("tenant %s did not start", slug)
            await JSONResponse(
                {"detail": "this instance cannot be started right now"}, status_code=503
            )(scope, receive, send)
            return
        prefix = f"/m/{slug}"
        child = dict(scope)
        child["path"] = "/" + rest
        child["raw_path"] = ("/" + rest).encode()
        child["root_path"] = scope.get("root_path", "") + prefix
        child["headers"] = [(k, v) for k, v in scope["headers"] if k != PROXY_HEADER] + [
            (PROXY_HEADER, b"host")
        ]
        child["app"] = loaded.app
        loaded.requests += 1
        try:
            await loaded.app(child, receive, send)
        finally:
            loaded.requests -= 1
            loaded.last_used = time.monotonic()


def host_from_env(env: dict[str, str] | None = None) -> Host:
    env = dict(os.environ if env is None else env)
    root = (
        env.get("AITMATION_HOST_ROOT")
        or env.get("AITMATION_DATA_DIR")
        or str(Path.home() / ".aitmation-host")
    )
    base = tuple(
        s.strip() for s in env.get("AITMATION_HOST_BASE_SOLUTIONS", "").split(",") if s.strip()
    )
    return Host(
        Path(root),
        auth_secret=env.get("AITMATION_AUTH_SECRET", ""),
        host_key=env.get("AITMATION_HOST_KEY", ""),
        login_url=env.get("AITMATION_LOGIN_URL", ""),
        solutions_dir=Path(env["AITMATION_SOLUTIONS_DIR"])
        if env.get("AITMATION_SOLUTIONS_DIR")
        else None,
        base_solutions=base,
        idle_seconds=float(env.get("AITMATION_HOST_IDLE_SECONDS", "900")),
        poll_seconds=float(env.get("AITMATION_POLL_INTERVAL_SECONDS", "2")),
        parallel_ticks=int(env.get("AITMATION_HOST_PARALLEL_TICKS", "4")),
        max_tenants=int(env.get("AITMATION_HOST_MAX_TENANTS", "0")),
        env=env,
    )


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        host = host_from_env()
    except HostError as exc:
        print(f"aitmation host: {exc}")
        return 2
    uvicorn.run(
        host,
        host=os.environ.get("AITMATION_HOST", "127.0.0.1"),
        port=int(os.environ.get("AITMATION_PORT", "8420")),
        log_level="info",
    )
    return 0
