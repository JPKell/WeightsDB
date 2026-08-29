"""weightsdb.session — session factory, unit-of-work scope, and explicit transaction control.

SQLite's transaction-start behaviour (``BEGIN IMMEDIATE``, so lock contention fails fast rather than
at commit) is dialect-specific plumbing configured once on the engine by :mod:`weightsdb.engine`;
nothing here branches on dialect.

Generalized from FreeWeight's ``infrastructure.db.session`` (ADR-0011): the application version
folded read-only declaration into ``session_scope`` itself via a ``read_only`` flag.  Two consumers
made that the wrong shape to keep — a caller wants to declare read-only-ness for one sub-block of a
unit of work, not the whole session — so it is split here into :func:`session_scope` (commit,
rollback, close: the session's lifecycle) and :func:`transaction` (declares how the transaction
that lifecycle wraps begins).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from weightsdb.engine import READ_ONLY_EXECUTION_OPTION
from weightsdb.errors import DatabaseError

__all__ = ["session_factory", "session_scope", "transaction"]


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a session factory bound to ``engine``.

    ``expire_on_commit=False``: a repository's return value is a detached, plain-data snapshot
    (coding standards §4 — ORM objects never leave the repository layer), and a caller reading an
    attribute off it after commit must not trigger a lazy load on a session that may already be
    closed.

    Args:
        engine: The engine to bind every session this factory produces to.

    Returns:
        A ``sessionmaker`` producing sessions against ``engine``.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Run one unit of work: commit on success, roll back on any exception, always close.

    "Any exception" includes ``KeyboardInterrupt`` and ``SystemExit`` — a ``Ctrl-C`` mid-write must
    leave the database in its pre-write state, not a half-committed one, so the ``except`` below is
    deliberately ``BaseException`` rather than ``Exception``.

    Args:
        factory: A session factory from :func:`session_factory`.

    Yields:
        A session open for exactly this unit of work. Wrap it in :func:`transaction` to declare
        read-only intent for SQLite's benefit; a plain write is the default and needs no wrapping.
    """
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def transaction(session: Session, *, immediate: bool = True) -> Iterator[Session]:
    """Declare how ``session``'s transaction begins.

    On SQLite, ``immediate=True`` (the default) uses ``BEGIN IMMEDIATE`` so lock contention fails
    fast, at the start of the transaction, rather than at commit time (:mod:`weightsdb.engine`'s own
    docstring explains why that matters). ``immediate=False`` uses a deferred ``BEGIN`` and enforces
    it: an attempted write inside raises rather than silently taking the write lock. Inert on
    PostgreSQL, where ordinary MVCC already lets readers and writers proceed without blocking each
    other.

    This only *declares* the transaction's start semantics — commit, rollback and close remain
    whichever enclosing :func:`session_scope` is responsible for them. It does not itself commit or
    roll back, so it composes as a sub-block of one:

    .. code-block:: python

        with session_scope(factory) as session, transaction(session, immediate=False):
            ...  # read-only work

    Args:
        session: An open session whose connection has not yet begun a transaction — normally one
            freshly obtained from :func:`session_scope`, before any query has run on it.
        immediate: ``True`` for ``BEGIN IMMEDIATE`` (a writer); ``False`` for a deferred ``BEGIN``
            (a reader).

    Yields:
        The same session, with its transaction-start semantics declared.

    Raises:
        DatabaseError: ``session`` already has an active transaction — database standards §6
            forbids nested transactions, and entering this twice on one session is exactly that.
    """
    if session.in_transaction():
        raise DatabaseError(
            "transaction() cannot nest: this session already has an active transaction "
            "(database standards §6 — use a savepoint deliberately if partial rollback is needed)."
        )
    if immediate:
        session.connection()
    else:
        read_only_options: dict[str, bool] = {READ_ONLY_EXECUTION_OPTION: True}
        session.connection(execution_options=read_only_options)
    yield session
