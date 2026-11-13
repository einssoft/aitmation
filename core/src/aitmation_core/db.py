"""Database layer: connection setup, migrations, online backup.

SQLite is the default in every deployment mode (ADR-0005). The connection
is configured for the single-process, moderately concurrent workload of
ADR-0011: WAL journal mode so readers never block the writer, a busy
timeout instead of immediate "database is locked" errors, and enforced
foreign keys.

Migrations are plain numbered SQL files applied at application start
(never by the supervisor - see docs/DEPLOYMENT.md, consequence 6).
Every migration must obey the two-release rule of ADR-0013: the previous
application version must still run against the migrated schema.

Backup: in WAL mode a naive file copy of the ``.db`` file silently misses
changes that still live in the ``-wal`` file. :func:`backup` therefore uses
``VACUUM INTO``, which produces a consistent single-file snapshot while the
database is in use.
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

MIGRATION_FILE_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and if necessary create) the database with our pragmas applied.

    ``path`` may be ``":memory:"`` for tests.
    """
    # check_same_thread=False: the web layer calls in from worker threads;
    # writes are serialized by the runtime lock plus SQLite's own locking.
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    # NORMAL is durable enough in WAL mode and much faster than FULL;
    # the WAL file is synced at checkpoints.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _bundled_migrations() -> list[tuple[int, str, str]]:
    """Return (version, name, sql) for every migration shipped in the package."""
    out: list[tuple[int, str, str]] = []
    root = resources.files("aitmation_core").joinpath("migrations")
    for entry in root.iterdir():
        m = MIGRATION_FILE_PATTERN.match(entry.name)
        if not m:
            continue
        out.append((int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    out.sort(key=lambda t: t[0])
    return out


def _statements(sql: str) -> list[str]:
    """Split a migration script into complete statements.

    ``executescript`` is unusable here: it issues an implicit COMMIT and
    leaves autocommit mode, which breaks the per-migration transaction.
    ``sqlite3.complete_statement`` understands strings and trigger bodies
    (BEGIN ... END), so a naive split on ";" is not needed.
    """
    out: list[str] = []
    buf = ""
    for line in sql.splitlines():
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("--")):
            continue
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return set()
    return {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply all pending migrations in order. Returns the versions applied.

    Each migration runs in its own transaction together with its
    ``schema_migrations`` record: it is either fully applied and recorded,
    or not at all.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    done = applied_versions(conn)
    applied: list[int] = []
    for version, name, sql in _bundled_migrations():
        if version in done:
            continue
        conn.execute("BEGIN")
        try:
            for statement in _statements(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at)"
                " VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (version, name),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


def backup(conn: sqlite3.Connection, dest: str | Path) -> Path:
    """Write a consistent single-file snapshot of the live database.

    Uses ``VACUUM INTO`` so the snapshot includes everything, including
    changes not yet checkpointed from the WAL file. The destination must
    not exist yet (SQLite refuses to overwrite).
    """
    dest = Path(dest)
    conn.execute("VACUUM INTO ?", (str(dest),))
    return dest
