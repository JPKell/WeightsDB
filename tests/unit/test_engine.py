"""Tests for weightsdb.engine."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError as SAOperationalError
from sqlalchemy.pool import QueuePool

from weightsdb.engine import READ_ONLY_EXECUTION_OPTION, _raise_if_busy, create_engine_for
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


def test_sqlite_secure_delete_is_on_for_every_connection() -> None:
    """A retention scrub must mean the same thing on every machine.

    ``secure_delete`` defaults to whatever the host's SQLite build chose, so whether deleted
    content is actually overwritten on disk would otherwise vary by machine (the M4 handoff's
    WeightsDB 0.2.1 item). Observed on a live connection, not trusted from the connect string.
    """
    with temporary_sqlite() as engine:
        with engine.connect() as connection:
            secure_delete = connection.execute(text("PRAGMA secure_delete")).scalar_one()
    assert secure_delete == 1


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
            secure_delete = connection.execute(text("PRAGMA secure_delete")).scalar_one()
    assert foreign_keys == 1
    assert journal_mode == "wal"
    assert secure_delete == 1


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


class _RecordingCursor:
    """A DBAPI cursor that records the statements the connect listener issues."""

    def __init__(self, log: list[tuple[str, object]]) -> None:
        self._log = log

    def execute(self, statement: str, parameters: object = None) -> None:
        self._log.append((statement, parameters))

    def close(self) -> None:
        return None


class _RecordingConnection:
    """The minimum DBAPI surface weightsdb's own connect listener touches."""

    def __init__(self) -> None:
        self.log: list[tuple[str, object]] = []
        self.isolation_level: object = "unset"

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.log)


def _weightsdb_connect_listener(engine: Engine) -> Callable[[object, object], None]:
    """Return the ``connect`` listener weightsdb itself registered on ``engine``.

    The dialect registers listeners of its own that would try to talk to a real server; this
    picks out ours so the statements it issues can be asserted without one.
    """
    for listener in engine.pool.dispatch.connect:
        if getattr(listener, "__module__", None) == "weightsdb.engine":
            return cast("Callable[[object, object], None]", listener)
    raise AssertionError("weightsdb registered no connect listener on this engine")


def test_postgresql_pool_size_is_passed_through() -> None:
    """`pool_size` is a PostgreSQL-only knob — SQLite's pool does not take one."""
    engine = create_engine_for("postgresql+psycopg://u:p@h:5432/db", pool_size=7)
    assert cast("QueuePool", engine.pool).size() == 7


def test_postgresql_connect_listener_sets_timeouts_and_application_name() -> None:
    """The values go through `set_config`, not `SET ... = %s`, which is a syntax error at "$1"."""
    engine = create_engine_for(
        "postgresql+psycopg://u:p@h:5432/db",
        statement_timeout_ms=30_000,
        application_name="weightsdb-tests",
    )
    connection = _RecordingConnection()
    _weightsdb_connect_listener(engine)(connection, None)

    assert connection.log == [
        ("SELECT set_config('statement_timeout', %s, false)", ("30000",)),
        ("SELECT set_config('lock_timeout', %s, false)", ("30000",)),
        ("SELECT set_config('application_name', %s, false)", ("weightsdb-tests",)),
    ]


def test_postgresql_connect_listener_issues_nothing_when_nothing_is_configured() -> None:
    """An unconfigured engine must not silently impose a timeout the caller never asked for."""
    engine = create_engine_for("postgresql+psycopg://u:p@h:5432/db")
    connection = _RecordingConnection()
    _weightsdb_connect_listener(engine)(connection, None)
    assert connection.log == []


def test_sqlite_connect_listener_hands_transaction_control_to_sqlalchemy() -> None:
    """`isolation_level = None` is what stops pysqlite opening a transaction of its own."""
    engine = create_engine_for("sqlite://")
    connection = _RecordingConnection()
    _weightsdb_connect_listener(engine)(connection, None)

    assert connection.isolation_level is None
    assert [statement for statement, _ in connection.log] == [
        "PRAGMA foreign_keys=ON",
        "PRAGMA journal_mode=WAL",
        "PRAGMA busy_timeout=5000",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA secure_delete=ON",
    ]


def test_a_non_busy_operational_error_at_begin_is_not_miscast_as_contention() -> None:
    """`_raise_if_busy` re-raises anything that is not SQLITE_BUSY, unchanged."""
    original = SAOperationalError("BEGIN IMMEDIATE", None, Exception("syntax error"))
    with pytest.raises(SAOperationalError) as caught:
        _raise_if_busy(original, busy_timeout_ms=5000)
    assert caught.value is original


class _BeginConnection:
    """The minimum connection surface weightsdb's own "begin" listener touches."""

    def __init__(
        self, options: dict[str, object], *, fails_with: tuple[str, BaseException] | None = None
    ) -> None:
        self._options = options
        self._fails_with = fails_with
        self.log: list[str] = []

    def get_execution_options(self) -> dict[str, object]:
        return self._options

    def exec_driver_sql(self, statement: str) -> None:
        self.log.append(statement)
        if self._fails_with is not None and statement == self._fails_with[0]:
            raise SAOperationalError(statement, None, self._fails_with[1])


def _weightsdb_begin_listener(engine: Engine) -> Callable[[object], None]:
    """Return the ``begin`` listener weightsdb itself registered on ``engine``."""
    for listener in engine.dispatch.begin:
        if getattr(listener, "__module__", None) == "weightsdb.engine":
            return cast("Callable[[object], None]", listener)
    raise AssertionError("weightsdb registered no begin listener on this engine")


def test_a_read_only_transaction_that_loses_the_race_also_raises_storage_busy() -> None:
    """A deferred BEGIN contends far less than BEGIN IMMEDIATE, but it can still lose.

    When it does, the reader gets the same typed StorageBusy the writer path gives (spec §13) —
    driving the listener directly because provoking a busy *deferred* BEGIN against a real file
    would mean holding an exclusive lock from another process.
    """
    engine = create_engine_for("sqlite://", sqlite_busy_timeout_ms=250)
    locked = sqlite3.OperationalError("database is locked")
    locked.sqlite_errorcode = sqlite3.SQLITE_BUSY
    connection = _BeginConnection({READ_ONLY_EXECUTION_OPTION: True}, fails_with=("BEGIN", locked))

    with pytest.raises(StorageBusy) as excinfo:
        _weightsdb_begin_listener(engine)(connection)

    assert excinfo.value.details["busy_timeout_ms"] == 250
    assert connection.log == ["PRAGMA query_only=ON", "BEGIN"]


def test_a_read_only_transaction_that_is_not_busy_re_raises_unchanged() -> None:
    """`query_only` is still set first, so the reader cannot write even if BEGIN then fails."""
    engine = create_engine_for("sqlite://")
    connection = _BeginConnection(
        {READ_ONLY_EXECUTION_OPTION: True},
        fails_with=("BEGIN", Exception("disk I/O error")),
    )

    with pytest.raises(SAOperationalError):
        _weightsdb_begin_listener(engine)(connection)

    assert connection.log == ["PRAGMA query_only=ON", "BEGIN"]
