"""Database layer: pragmas, migrations, append-only audit, backup."""

import sqlite3

import pytest

from aitmation_core import db


def test_wal_mode_on_file_database(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrations_apply_once(conn):
    # conn fixture is already migrated; a second run is a no-op.
    assert db.migrate(conn) == []
    assert 1 in db.applied_versions(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    assert "0001_initial.sql" in names


def test_audit_log_is_append_only(conn):
    conn.execute(
        "INSERT INTO cases (id, template_id, template_version, status,"
        " created_at, updated_at) VALUES ('c1', 't', 1, 'running', 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO audit_log (case_id, seq, at, actor_type, actor_id, event,"
        " app_version) VALUES ('c1', 1, 'x', 'system', 's', 'e', '0')"
    )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit_log SET event = 'tampered'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM audit_log")


def test_backup_snapshot_is_consistent(tmp_path):
    conn = db.connect(tmp_path / "live.db")
    try:
        db.migrate(conn)
        conn.execute(
            "INSERT INTO cases (id, template_id, template_version, status,"
            " created_at, updated_at) VALUES ('c1', 't', 1, 'running', 'x', 'x')"
        )
        snap = db.backup(conn, tmp_path / "snap.db")
    finally:
        conn.close()
    copy = sqlite3.connect(snap)
    try:
        assert copy.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1
    finally:
        copy.close()
