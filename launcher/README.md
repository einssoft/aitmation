# launcher

Lifecycle management for an aitmation **household**: reads
`household.toml`, starts one core instance per mandant plus the mailroom
(the instance that routes incoming documents to the right mandant), keeps
them alive (restart with backoff), writes the mailroom's table of
addressees from the household, grants the catch-all mandate when there is
only one mandant, and answers a small HTTP API through which the setup
wizard of the interface adds mandants.

Deterministic and deliberately boring: no model access, no audit trail,
no intelligence.

## Use (development)

```
cd launcher && go build -o aitmation-launcher .
./aitmation-launcher init -data-dir ~/aitmation-data -admin alex -private -company "Company X"
./aitmation-launcher run  -data-dir ~/aitmation-data -core-dir ../core -apps-dir ../apps
```

`init` creates the admin as the first user: password from
`-admin-password` or `AITMATION_ADMIN_PASSWORD`, otherwise one is
generated and shown **once**. From then on the household requires a
sign-in; the interface asks for it by itself. Further people:

```
./aitmation-launcher user add    -data-dir ~/aitmation-data -name anna -instances privat        # member
./aitmation-launcher user add    -data-dir ~/aitmation-data -name urs  -role admin
./aitmation-launcher user passwd -data-dir ~/aitmation-data -name anna
./aitmation-launcher user remove -data-dir ~/aitmation-data -name anna
./aitmation-launcher token       -data-dir ~/aitmation-data -user alex -days 365   # for the MCP channel (AITMATION_TOKEN)
```

Or in the interface under **Administration → Users** (admin). A household
without `users.json` (an older installation) keeps running open; the
launcher says so at start.

Then: status page `http://127.0.0.1:8400/`, interface
`http://127.0.0.1:8400/app/` — **the launcher is the one origin of the
household:** it passes `/app/` and `/api/v1/` to the mailroom and
`/m/<instance-id>/…` to the respective instance (prefix removed, marker
`X-Aitmation-Proxy`, on which the core returns relative member URLs). The
shell still runs directly on `:8420/app/` (development, Vite proxy). The
launcher binds to loopback only; expose it through `tailscale serve` or a
reverse proxy — with sign-in in front, never as a household without users.
Further mandants are created on the setup page of the interface
(`POST /api/household/mandants`); the launcher saves, starts the instance
and rewires the mailroom.

`-core-cmd` and `-core-dir` point to the checkout in development and to
the unpacked release in an installation. Without `-core-cmd` the launcher
takes the Python of the bundle (`<core-dir>/../python`), otherwise the
interpreter of the environment under `<core-dir>/.venv` (after `uv sync`),
otherwise `uv run python -m aitmation_core`.

### Windows (Git Bash or PowerShell)

Go is needed for building only — either `winget install GoLang.Go` (then
reopen the terminal, `go version`) or a ready `aitmation-launcher.exe` in
this directory (ignored by git, `*.exe`). The core needs `uv sync` in
`core/` (extra `mcp` optional), the interface needs either Node
(`cd ui && npm ci && npm run build`) or a ready `ui/dist/`.

```
cd launcher
./aitmation-launcher.exe init -data-dir "$USERPROFILE/aitmation-data" -admin alex -private -company "Company X"
./aitmation-launcher.exe run  -data-dir "$USERPROFILE/aitmation-data" -core-dir ../core -apps-dir ../apps
```

Stop with Ctrl+C; on Windows the cores are terminated hard (no SIGINT to
child processes), which is uncritical for SQLite in WAL mode.

## Release bundle (no terminal)

`scripts/bundle.sh <version> <goos> <goarch>` builds an archive with the
launcher, its own Python including all packages, `uv`, the core, the base
solutions and the interface; `release.yml` does that for all platforms on
a tag. In the unpacked bundle `start.cmd` / `start.sh` starts the launcher
with `-auto-init -open-browser`: if there is no household yet, one is
created (a private mandant, admin = the signed-in OS user, password shown
once); the core runs on the Python in the bundle. Only a bundle without
`python/` lets `uv` set up a `core/.venv` at first start (Python into
`<data>/python`, needs the network once). `internal/bootstrap` takes care
of this; a checkout with an existing `.venv` is not affected.

## Directory layout

```
<DataDir>/
  household.toml           source of truth (admin, ports, mandants)
  users.json               users with password hashes (0600)
  auth-secret              household secret for session tokens (0600)
  python/, uv-cache/       interpreter managed by uv (bundles without their own Python only)
  mailroom/                data directory of the mailroom (addressees.toml from the launcher)
  mandants/<id>/           data directory per mandant (own database, inbox, archive)
  backups/
```

Ports: launcher 8400, mailroom 8420, mandants from 8421.

## Structure

- `internal/household` – load and save the file (atomically), create a
  mandant (slug, next port), checks, render the table of addressees
- `internal/supervise` – one process per instance: start, health probe on
  `/healthz`, restart with exponential backoff, SIGINT-friendly stop
- `internal/api` – status page (framework-free), `/api/household` (GET)
  and `/api/household/mandants` (POST), sign-in, and the reverse proxy
  `proxy.go` (`/app/`, `/api/v1/`, `/m/<id>/`); CORS for loopback only

Dependency: `github.com/BurntSushi/toml` (MIT).

## As a service (macOS)

`scripts/install-macos-service.sh --data-dir ~/aitmation-data` registers
the launcher as a LaunchAgent (`com.aitmation.launcher`, RunAtLoad +
KeepAlive, log `<data>/logs/launcher.log`); `--uninstall` removes it.
Requirements: built launcher, `core/.venv`, `ui/dist`, initialised
household.

## Open

Fetching a release, verifying signature and checksum, update with backup
and rollback, service registration for systemd and Windows services
(macOS: done), code signing — the platform-specific parts are the effort,
not Go.

## Build

```
go build ./...
GOOS=windows GOARCH=amd64 go build -o dist/aitmation-launcher.exe
GOOS=darwin  GOARCH=arm64 go build -o dist/aitmation-launcher-macos
```
