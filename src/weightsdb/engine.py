"""weightsdb.engine — dialect-correct engine construction.

Database standards §2: SQLite gets ``foreign_keys=ON``, ``journal_mode=WAL``, ``busy_timeout``,
``synchronous=NORMAL``, ``secure_delete=ON``, applied per connection so a pool reconnect never
silently loses them;
PostgreSQL gets ``statement_timeout``, ``lock_timeout`` and ``application_name``. Only these two
dialects are supported (§2) — a third requires an ADR, not a code change here.

Moved from FreeWeight's ``infrastructure.db.engine``, which was written "as if it were WeightsDB's
own" module for exactly this move (ADR-0011). Generalized in one respect the application version
did not need: SQLite lock contention beyond ``busy_timeout`` now raises the typed
:class:`~weightsdb.errors.StorageBusy` (spec §13) instead of a raw
``sqlalchemy.exc.OperationalError`` — FreeWeight had one consumer and no test for that translation;
WeightsDB's contract requires it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError as SAOperationalError

from weightsdb.errors import DatabaseError, StorageBusy

__all__ = ["READ_ONLY_EXECUTION_OPTION", "create_engine_for"]

READ_ONLY_EXECUTION_OPTION = "weightsdb_read_only"
"""Execution option marking a transaction as read-only.

Set it on a connection (:meth:`Connection.execution_options`) — as
:func:`~weightsdb.session.transaction` does — to select a deferred ``BEGIN`` instead of
``BEGIN IMMEDIATE`` on SQLite, enforced with ``PRAGMA query_only``; on PostgreSQL it is inert, since
ordinary MVCC already lets readers and writers proceed without blocking each other.
"""

_SUPPORTED_DIALECTS = frozenset({"sqlite", "postgresql"})


def create_engine_for(
    url: str,
    *,
    echo: bool = False,
    pool_size: int | None = None,
    statement_timeout_ms: int | None = None,
    sqlite_busy_timeout_ms: int = 5000,
    application_name: str | None = None,
) -> Engine:
    """Build an engine with dialect-correct settings applied to every connection.

    Every setting below is applied by an event listener on the returned engine's connection pool,
    not once at construction time — the pool can and does open new DBAPI connections after the
    first (on recycle, after a disposal, after a dropped network connection to PostgreSQL), and a
    setting that only took effect on the first connection would silently stop applying on the
    second (the specific failure mode spec §7's "pragmas lost after a pool reconnect" names).

    SQLite additionally gets ``BEGIN IMMEDIATE`` transaction semantics: pysqlite's own implicit
    transaction handling is disabled (``isolation_level = None`` on the raw connection) and
    replaced with an explicit ``BEGIN IMMEDIATE`` issued by SQLAlchemy's ``"begin"`` hook, so lock
    contention under SQLite's single-writer model fails fast — at the start of a transaction,
    within ``sqlite_busy_timeout_ms`` — rather than silently at commit time, which is when
    pysqlite's default deferred-transaction behaviour would otherwise surface it.

    That matters because the deferred failure is not merely late, it is **unrecoverable**: a
    transaction that reads, then tries to write after another connection has committed, gets
    ``SQLITE_BUSY_SNAPSHOT``, which ``busy_timeout`` does not apply to. It fails instantly, no
    amount of waiting helps, and the only escape is to roll back and redo work already done.
    ``BEGIN IMMEDIATE`` converts that into an ordinary retryable wait at the start, before any work
    exists to lose.

    The cost is that it declares *every* transaction a writer, and WAL's whole point is that
    readers never contend — so a transaction that only reads must say so, via
    :data:`READ_ONLY_EXECUTION_OPTION` (:func:`~weightsdb.session.transaction`), and gets a
    deferred ``BEGIN`` instead. Without that, two concurrent readers would queue behind each other
    on the single write lock for up to ``sqlite_busy_timeout_ms`` and then fail.

    Args:
        url: A ``sqlite:///`` or ``postgresql(+driver)://`` URL. Only these two dialects are
            supported (database standards §2).
        echo: Log every emitted SQL statement. Development use only.
        pool_size: PostgreSQL connection pool size. Ignored for SQLite, whose pool classes do not
            take this argument.
        statement_timeout_ms: PostgreSQL ``statement_timeout`` (and, identically, ``lock_timeout``,
            since a statement that cannot even acquire its lock should not wait longer than the
            statement itself is allowed to run). ``None`` leaves both at the server default.
            Ignored for SQLite, which has no equivalent server-side setting —
            ``sqlite_busy_timeout_ms`` is SQLite's analogue.
        sqlite_busy_timeout_ms: SQLite's ``PRAGMA busy_timeout``. Ignored for PostgreSQL.
        application_name: PostgreSQL ``application_name``, surfaced in ``pg_stat_activity``.
            Ignored for SQLite.

    Returns:
        A configured :class:`~sqlalchemy.Engine`. Construction is cheap — no connection is opened
        until first use — so this comfortably meets the ≤ 50 ms creation budget (spec §15).

    Raises:
        DatabaseError: ``url``'s dialect is neither ``sqlite`` nor ``postgresql``.
    """
    dialect = make_url(url).get_backend_name()
    if dialect not in _SUPPORTED_DIALECTS:
        raise DatabaseError(
            f"Unsupported dialect {dialect!r}; only sqlite and postgresql are supported "
            "(database standards §2). Adding a third dialect requires an ADR.",
            details={"dialect": dialect},
        )

    engine_kwargs: dict[str, Any] = {"echo": echo}
    if dialect == "postgresql" and pool_size is not None:
        engine_kwargs["pool_size"] = pool_size

    engine = create_engine(url, **engine_kwargs)

    if dialect == "sqlite":
        _ensure_sqlite_directory_exists(url)
        _configure_sqlite(engine, busy_timeout_ms=sqlite_busy_timeout_ms)
    else:
        _configure_postgresql(
            engine, statement_timeout_ms=statement_timeout_ms, application_name=application_name
        )

    return engine


def _ensure_sqlite_directory_exists(url: str) -> None:
    """Create the SQLite file's parent directory, so a fresh install has somewhere to write.

    A no-op for ``:memory:`` and for an already-existing directory.
    """
    # `URL.database`, not a hand-rolled parse: the sqlite dialect owns the rule for how many
    # leading slashes separate "sqlite://" from an absolute path, and getting it wrong turns
    # "sqlite:////tmp/x" into the relative "tmp/x" against whatever the cwd happens to be.
    database = make_url(url).database
    if not database or database == ":memory:":
        return
    Path(database).parent.mkdir(parents=True, exist_ok=True)


def _raise_if_busy(exc: SAOperationalError, *, busy_timeout_ms: int) -> None:
    """Translate a SQLite busy/snapshot-busy failure into :class:`StorageBusy`.

    Re-raises anything else unchanged, so a genuine schema or syntax error at ``BEGIN`` time — not
    that one is expected — is never miscast as contention.
    """
    orig = exc.orig
    code = getattr(orig, "sqlite_errorcode", None)
    if code in (sqlite3.SQLITE_BUSY, getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", -1)):
        raise StorageBusy(
            f"SQLite database is locked (busy_timeout={busy_timeout_ms}ms exceeded).",
            details={"busy_timeout_ms": busy_timeout_ms},
        ) from exc
    raise exc


def _configure_sqlite(engine: Engine, *, busy_timeout_ms: int) -> None:
    """Wire pragmas and ``BEGIN IMMEDIATE`` onto every connection this engine opens."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        # Hand transaction control to SQLAlchemy entirely: pysqlite's own implicit-BEGIN behaviour
        # and our explicit "begin" hook below would otherwise fight over who opens the
        # transaction, producing "cannot start a transaction within a transaction".
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            # secure_delete defaults to whatever the host's SQLite build chose, so whether a
            # deleted row's content is actually overwritten on disk would otherwise vary by
            # machine — and a consumer's retention scrub is a promise about the disk, not about
            # the schema. Set explicitly so the scrub means the same thing everywhere.
            cursor.execute("PRAGMA secure_delete=ON")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Any) -> None:
        # A connection explicitly switched to AUTOCOMMIT (e.g. VACUUM, which cannot run inside any
        # transaction on SQLite) still fires this event — SQLAlchemy's "begin" hook does not know
        # what AUTOCOMMIT means, only this dialect-specific listener does. Forcing a literal
        # BEGIN IMMEDIATE onto such a connection is exactly the failure that opts it out here.
        options = connection.get_execution_options()
        if options.get("isolation_level") == "AUTOCOMMIT":
            connection.exec_driver_sql("PRAGMA query_only=OFF")
            return
        if options.get(READ_ONLY_EXECUTION_OPTION):
            # Plain BEGIN — deferred. A transaction that only reads never has to upgrade to the
            # write lock, so the snapshot hazard IMMEDIATE exists to avoid cannot arise, and
            # declaring it a writer would throw away WAL's concurrent reads for nothing.
            connection.exec_driver_sql("PRAGMA query_only=ON")
            try:
                connection.exec_driver_sql("BEGIN")
            except SAOperationalError as exc:
                _raise_if_busy(exc, busy_timeout_ms=busy_timeout_ms)
            return
        # `query_only` is set explicitly on *both* paths, every transaction, rather than being
        # cleaned up after the read-only one. A reset that only runs on the way out is a reset
        # that does not run when the way out is an exception, and the failure mode — a pooled
        # connection stuck read-only, rejecting writes for the rest of its life — is both silent
        # and very hard to trace back to here.
        connection.exec_driver_sql("PRAGMA query_only=OFF")
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        except SAOperationalError as exc:
            _raise_if_busy(exc, busy_timeout_ms=busy_timeout_ms)


def _configure_postgresql(
    engine: Engine, *, statement_timeout_ms: int | None, application_name: str | None
) -> None:
    """Apply ``statement_timeout``/``lock_timeout``/``application_name`` on every connection."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            # `set_config(...)`, not `SET ... = %s`. PostgreSQL's SET is a utility statement whose
            # value is parsed as a literal, so a bind parameter in it is a syntax error at "$1" —
            # every connection carrying an application_name failed outright. set_config is the
            # function form and takes ordinary parameters, which also keeps the value off the
            # statement string entirely.
            if statement_timeout_ms is not None:
                timeout = str(int(statement_timeout_ms))
                cursor.execute("SELECT set_config('statement_timeout', %s, false)", (timeout,))
                cursor.execute("SELECT set_config('lock_timeout', %s, false)", (timeout,))
            if application_name is not None:
                cursor.execute(
                    "SELECT set_config('application_name', %s, false)", (application_name,)
                )
        finally:
            cursor.close()
