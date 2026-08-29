"""Tests for weightsdb.engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from weightsdb.engine import create_engine_for
from weightsdb.errors import DatabaseError, StorageBusy
from weightsdb.testing import temporary_postgres, temporary_sqlite


def test_sqlite_pragmas_applied_on_fresh_connection() -> None:
    with temporary_sqlite() as engine:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.rollback()
            pragmas = {
                "foreign_keys": connection.execute(text("PRAGMA foreign_keys")).scalar_one(),
                "journal_mode": connection.execute(text("PRAGMA journal_mode")).scalar_one(),
            }
    assert pragmas["foreign_keys"] == 1
    assert pragmas["journal_mode"] == "wal"


def test_sqlite_pragmas_applied_after_forced_reconnect() -> None:
    """A pool recycle must not silently drop the pragmas (spec §7, §11.1)."""
    with temporary_sqlite() as engine:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.rollback()
        # Force every pooled DBAPI connection to be discarded, so the next checkout opens a new
        # one and re-fires the "connect" event.
        engine.dispose()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.rollback()
            foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
            journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
    assert foreign_keys == 1
    assert journal_mode == "wal"


def test_sqlite_busy_timeout_raises_storage_busy() -> None:
    """Contention on BEGIN IMMEDIATE beyond busy_timeout raises the typed StorageBusy."""
    with temporary_sqlite() as engine:
        with engine.connect() as connection:
            connection.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
            connection.commit()

        holder = engine.connect()
        holder.execute(text("INSERT INTO t (id) VALUES (1)"))  # opens as a writer (BEGIN IMMEDIATE)

        contender = create_engine_for(str(engine.url), sqlite_busy_timeout_ms=100)
        try:
            with pytest.raises(StorageBusy) as excinfo:
                with contender.connect() as second:
                    second.execute(text("INSERT INTO t (id) VALUES (2)"))
                    second.commit()
            assert excinfo.value.details["busy_timeout_ms"] == 100
        finally:
            holder.rollback()
            holder.close()
            contender.dispose()


def test_unsupported_dialect_rejected() -> None:
    with pytest.raises(DatabaseError):
        create_engine_for("mysql://user:pass@localhost/db")


def test_sqlite_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "test.sqlite3"
    engine = create_engine_for(f"sqlite:///{target}")
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.rollback()
    finally:
        engine.dispose()
    assert target.parent.is_dir()


def test_postgresql_settings_applied() -> None:
    """statement_timeout, lock_timeout and application_name land on every connection."""
    with temporary_postgres() as probe:
        url = str(probe.url)
    engine = create_engine_for(url, statement_timeout_ms=54321, application_name="weightsdb-test")
    try:
        with engine.connect() as connection:
            statement_timeout = connection.execute(text("SHOW statement_timeout")).scalar_one()
            lock_timeout = connection.execute(text("SHOW lock_timeout")).scalar_one()
            application_name = connection.execute(text("SHOW application_name")).scalar_one()
            connection.rollback()
    finally:
        engine.dispose()
    assert "54321" in statement_timeout
    assert "54321" in lock_timeout
    assert application_name == "weightsdb-test"


def test_postgresql_settings_reapplied_after_forced_reconnect() -> None:
    with temporary_postgres() as probe:
        url = str(probe.url)
    engine = create_engine_for(url, statement_timeout_ms=54321, application_name="weightsdb-test")
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.rollback()
        engine.dispose()
        with engine.connect() as connection:
            application_name = connection.execute(text("SHOW application_name")).scalar_one()
            connection.rollback()
    finally:
        engine.dispose()
    assert application_name == "weightsdb-test"
