"""Tests for weightsdb.testing — fixtures shipped to consumers as public API (spec §7)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from weightsdb.testing import temporary_postgres, temporary_sqlite

# Port 1 is reserved and never listening, so this refuses immediately rather than hanging on a
# connect timeout — and it stays unreachable on a machine that does run PostgreSQL locally.
_UNREACHABLE_URL = "postgresql+psycopg://weightsdb:weightsdb@127.0.0.1:1/weightsdb_test"


def test_temporary_sqlite_is_a_real_file_that_is_removed_on_exit() -> None:
    """A file, not `:memory:` — backup, restore and checkpoint all need one to operate on."""
    with temporary_sqlite() as engine:
        database = engine.url.database
        assert database is not None
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.rollback()
        assert Path(database).is_file()
    assert not Path(database).exists()


def test_temporary_postgres_skips_when_no_server_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEIGHTSDB_POSTGRES_URL", _UNREACHABLE_URL)
    monkeypatch.delenv("WEIGHTSDB_REQUIRE_POSTGRES", raising=False)
    with pytest.raises(pytest.skip.Exception, match="no PostgreSQL server available"):
        with temporary_postgres():
            pass  # pragma: no cover — the context manager raises on entry


def test_temporary_postgres_fails_loudly_when_a_server_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silently skipped dialect is an untested dialect: CI sets this to turn the skip into red."""
    monkeypatch.setenv("WEIGHTSDB_POSTGRES_URL", _UNREACHABLE_URL)
    monkeypatch.setenv("WEIGHTSDB_REQUIRE_POSTGRES", "1")
    with pytest.raises(pytest.fail.Exception, match="WEIGHTSDB_REQUIRE_POSTGRES=1"):
        with temporary_postgres():
            pass  # pragma: no cover — the context manager raises on entry
