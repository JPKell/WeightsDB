"""weightsdb.testing — fixtures for a consumer's own test suite, shipped as supported API.

Not a test module itself: importable by any application's tests, and part of this package's public
contract (spec §7) rather than an internal helper that happens to be reachable.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from weightsdb.engine import create_engine_for
from weightsdb.migrations import MigrationRunner

if TYPE_CHECKING:
    from sqlalchemy import Engine, MetaData

__all__ = ["MigrationHarness", "migration_harness", "temporary_postgres", "temporary_sqlite"]

_DEFAULT_POSTGRES_URL = "postgresql+psycopg://weightsdb:weightsdb@localhost:5432/weightsdb_test"


@contextmanager
def temporary_sqlite() -> Iterator[Engine]:
    """Yield an engine on a fresh, empty SQLite database file, disposed and deleted on exit.

    A real file rather than ``:memory:``: backup, restore and the WAL checkpoint all operate on a
    file on disk, and an in-memory database would silently exempt every one of those from a test
    that believes it is exercising them.
    """
    with tempfile.TemporaryDirectory(prefix="weightsdb-") as directory:
        url = f"sqlite:///{Path(directory) / 'test.sqlite3'}"
        engine = create_engine_for(url)
        try:
            yield engine
        finally:
            engine.dispose()


@contextmanager
def temporary_postgres() -> Iterator[Engine]:
    """Yield an engine on a freshly reset PostgreSQL schema, disposed on exit.

    Skips (``pytest.skip``) when no server is reachable at ``WEIGHTSDB_POSTGRES_URL`` (default
    ``postgresql+psycopg://weightsdb:weightsdb@localhost:5432/weightsdb_test``), **except** when
    ``WEIGHTSDB_REQUIRE_POSTGRES=1``, which turns the skip into a failure: a silently skipped
    dialect is an untested dialect, and the both-dialects promise (spec §7) is only as good as its
    enforcement.

    The server is reused across calls, so this resets to empty by dropping and recreating the
    ``public`` schema — including any ``alembic_version`` table a previous test left behind —
    rather than assuming a pristine database.
    """
    url = os.environ.get("WEIGHTSDB_POSTGRES_URL", _DEFAULT_POSTGRES_URL)
    require = os.environ.get("WEIGHTSDB_REQUIRE_POSTGRES") == "1"
    try:
        probe = create_engine_for(url)
        try:
            with probe.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            probe.dispose()
    except Exception as exc:  # noqa: BLE001 — any failure means "no usable server", by design
        if require:
            pytest.fail(f"WEIGHTSDB_REQUIRE_POSTGRES=1 but {url} is unreachable: {exc}")
        pytest.skip(f"no PostgreSQL server available at {url}: {exc}")

    reset_engine = create_engine_for(url)
    try:
        with reset_engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        reset_engine.dispose()

    engine = create_engine_for(url)
    try:
        yield engine
    finally:
        engine.dispose()


@dataclass(slots=True)
class MigrationHarness:
    """One consumer's migration script location and metadata, ready for its own tests.

    Bundles the two pieces of information every migration test needs so an application's test
    module does not repeat ``MigrationRunner(engine, script_location=...)`` boilerplate in every
    test function; :meth:`sqlite` and :meth:`postgres` each combine a fresh :func:`temporary_sqlite`
    or :func:`temporary_postgres` engine with a :class:`~weightsdb.migrations.MigrationRunner` bound
    to it.

    Attributes:
        script_location: Filesystem path to the consumer's ``migrations/`` directory.
        metadata: The consumer's own declarative ``MetaData``, for ``check_parity``.
    """

    script_location: str
    metadata: MetaData

    @contextmanager
    def sqlite(self) -> Iterator[MigrationRunner]:
        """A :class:`~weightsdb.migrations.MigrationRunner` on a fresh temporary SQLite database."""
        with temporary_sqlite() as engine:
            yield MigrationRunner(engine, script_location=self.script_location)

    @contextmanager
    def postgres(self) -> Iterator[MigrationRunner]:
        """A :class:`~weightsdb.migrations.MigrationRunner` on a freshly reset PostgreSQL DB."""
        with temporary_postgres() as engine:
            yield MigrationRunner(engine, script_location=self.script_location)


def migration_harness(script_location: str, metadata: MetaData) -> MigrationHarness:
    """Build a :class:`MigrationHarness` for one consumer's migration history.

    Args:
        script_location: Filesystem path to the consumer's ``migrations/`` directory (containing
            ``env.py`` and ``versions/``).
        metadata: The consumer's own declarative ``MetaData`` — never this package's, which is
            always empty (spec §7, §20).
    """
    return MigrationHarness(script_location=script_location, metadata=metadata)
