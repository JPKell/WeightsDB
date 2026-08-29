"""Tests for weightsdb.backup: backup, restore, rotation, integrity, disk-full handling."""

from __future__ import annotations

import os
import shlex
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from weightsdb.backup import (
    backup,
    backup_revision,
    database_size_bytes,
    integrity_check,
    pg_restore_command,
    prune_backups,
    restore,
)
from weightsdb.engine import create_engine_for
from weightsdb.errors import DatabaseError, StorageFull
from weightsdb.testing import temporary_postgres, temporary_sqlite


def _seed(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(text("INSERT INTO t (name) VALUES ('a')"))
        connection.commit()


def test_backup_and_restore_round_trip(tmp_path: Path) -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        destination = tmp_path / "backup.sqlite3"
        result = backup(engine, destination)
        assert result.path == destination
        assert result.dialect == "sqlite"
        assert destination.is_file()

        with engine.connect() as connection:
            connection.execute(text("INSERT INTO t (name) VALUES ('b')"))
            connection.commit()

        restore_result = restore(engine, destination, confirm=True)
        assert restore_result.path is not None
        with engine.connect() as connection:
            names = [row[0] for row in connection.execute(text("SELECT name FROM t"))]
        assert names == ["a"]


def test_restore_requires_confirm() -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        with pytest.raises(DatabaseError, match="confirm"):
            restore(engine, Path("/nonexistent"), confirm=False)


def test_backup_of_database_with_open_write_transaction_succeeds(tmp_path: Path) -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        writer = engine.connect()
        writer.execute(text("INSERT INTO t (name) VALUES ('mid-transaction')"))
        # Not committed yet — backup must still succeed and be internally consistent.
        destination = tmp_path / "live-backup.sqlite3"
        result = backup(engine, destination)
        writer.rollback()
        writer.close()

        check = sqlite3.connect(destination)
        try:
            row = check.execute("PRAGMA integrity_check").fetchone()
        finally:
            check.close()
        assert row[0] == "ok"
        assert result.path.is_file()


def test_restore_of_corrupt_backup_refused_before_touching_live_database(tmp_path: Path) -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        corrupt = tmp_path / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a sqlite database")

        with pytest.raises(DatabaseError, match="integrity check"):
            restore(engine, corrupt, confirm=True)

        with engine.connect() as connection:
            names = [row[0] for row in connection.execute(text("SELECT name FROM t"))]
        assert names == ["a"]


def test_restore_refuses_backup_at_unknown_revision(tmp_path: Path) -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        destination = tmp_path / "backup.sqlite3"
        backup(engine, destination)
        with sqlite3.connect(destination) as connection:
            connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            connection.execute("INSERT INTO alembic_version VALUES ('9999-unknown')")
            connection.commit()

        with pytest.raises(DatabaseError, match="unknown|does not contain"):
            restore(engine, destination, confirm=True, known_revisions=frozenset({"0001"}))


def test_disk_full_during_backup_removes_partial_file_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        destination = tmp_path / "backup.sqlite3"

        class _FullDiskConnection:
            def backup(self, _target: object) -> None:
                raise sqlite3.OperationalError("database or disk is full")

            def close(self) -> None:
                pass

        original_connect = sqlite3.connect

        def _fake_connect(path: Any, *args: Any, **kwargs: Any) -> Any:
            if str(path) == str(engine.url.database):
                return _FullDiskConnection()
            return original_connect(path, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", _fake_connect)
        with pytest.raises(StorageFull):
            backup(engine, destination)
    assert not destination.exists()


def test_prune_backups_keeps_only_the_newest(tmp_path: Path) -> None:
    import time

    paths = []
    for index in range(5):
        path = tmp_path / f"pre-migration-000{index}.sqlite3"
        path.write_bytes(b"x")
        paths.append(path)
        time.sleep(0.01)

    pruned = prune_backups(tmp_path, prefix="pre-migration-", keep=2)
    assert len(pruned) == 3
    assert pruned == tuple(paths[:3])
    remaining = sorted(tmp_path.iterdir())
    assert remaining == sorted(paths[3:])


def test_prune_backups_never_touches_operator_named_files(tmp_path: Path) -> None:
    named = tmp_path / "my-own-backup.sqlite3"
    named.write_bytes(b"x")
    auto = tmp_path / "pre-migration-0001.sqlite3"
    auto.write_bytes(b"x")

    prune_backups(tmp_path, prefix="pre-migration-", keep=0)
    assert named.exists()
    assert not auto.exists()


def test_backup_revision_reads_version_table(tmp_path: Path) -> None:
    destination = tmp_path / "versioned.sqlite3"
    with sqlite3.connect(destination) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('0002')")
        connection.commit()
    assert backup_revision(destination) == "0002"


def test_backup_revision_none_when_no_version_table(tmp_path: Path) -> None:
    destination = tmp_path / "unversioned.sqlite3"
    with sqlite3.connect(destination) as connection:
        connection.execute("CREATE TABLE t (id INTEGER)")
        connection.commit()
    assert backup_revision(destination) is None


def test_integrity_check_reports_ok() -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        result = integrity_check(engine)
        assert result.ok
        assert result.detail == "ok"


def test_database_size_bytes_nonzero_after_seed() -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        assert database_size_bytes(engine) > 0


def test_restore_refuses_on_postgresql_and_names_the_command(tmp_path: Path) -> None:
    """No automatic PostgreSQL restore (spec §11.4): refuses and names pg_restore.

    No server needed — the check happens before any connection is opened.
    """
    engine = create_engine_for("postgresql+psycopg://user:pass@localhost/db")
    try:
        with pytest.raises(DatabaseError, match="pg_restore"):
            restore(engine, tmp_path / "whatever.dump", confirm=True)
    finally:
        engine.dispose()


def test_pg_restore_command_names_host_port_user_and_database() -> None:
    engine = create_engine_for("postgresql+psycopg://alice:secret@dbhost:5433/loadcoach")
    try:
        command = pg_restore_command(engine, Path("/backups/loadcoach.dump"))
    finally:
        engine.dispose()
    assert "dbhost" in command
    assert "5433" in command
    assert "alice" in command
    assert "loadcoach" in command
    assert "/backups/loadcoach.dump" in command
    assert "secret" not in command


def test_backup_and_restore_round_trip_postgresql(tmp_path: Path) -> None:
    with temporary_postgres() as engine:
        _seed(engine)
        destination = tmp_path / "backup.dump"
        result = backup(engine, destination)
        assert result.dialect == "postgresql"
        assert destination.is_file()

        with engine.connect() as connection:
            connection.execute(text("INSERT INTO t (name) VALUES ('b')"))
            connection.commit()

        # restore() itself refuses on PostgreSQL (spec §11.4); the backup file is exercised via
        # pg_restore directly, proving the archive produced by backup() is a real, usable dump.
        pg_restore_available = shutil.which("pg_restore") is not None
        if not pg_restore_available:
            pytest.skip("pg_restore is not installed; backup() output cannot be verified")
        restore_command = pg_restore_command(engine, destination)
        subprocess.run(  # noqa: S603
            [*shlex.split(restore_command)],
            check=True,
            capture_output=True,
            env={**os.environ, "PGPASSWORD": engine.url.password or ""},
        )
        with engine.connect() as connection:
            names = sorted(row[0] for row in connection.execute(text("SELECT name FROM t")))
        assert names == ["a"]
