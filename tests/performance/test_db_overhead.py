"""Performance budgets from spec §15. Excluded by default; run with `pytest -m performance`."""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Engine, Integer, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from weightsdb.backup import backup, integrity_check
from weightsdb.engine import create_engine_for
from weightsdb.migrations import MigrationRunner
from weightsdb.session import session_factory, session_scope
from weightsdb.testing import temporary_sqlite

pytestmark = pytest.mark.performance

_SCRIPT_LOCATION = str(Path(__file__).parent.parent / "integration" / "_migration_fixture")


class _Base(DeclarativeBase):
    pass


class _Payload(_Base):
    __tablename__ = "payload"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    blob: Mapped[str] = mapped_column(String)


def test_engine_creation_budget() -> None:
    """spec §15: engine creation <= 50 ms."""
    start = time.perf_counter()
    engine = create_engine_for("sqlite:///:memory:")
    elapsed_ms = (time.perf_counter() - start) * 1000
    engine.dispose()
    assert elapsed_ms <= 50, f"engine creation took {elapsed_ms:.2f} ms"


def test_session_acquisition_budget() -> None:
    """spec §15: session acquisition from the pool <= 1 ms (warm pool, after the first checkout)."""
    with temporary_sqlite() as engine:
        factory = session_factory(engine)
        with session_scope(factory) as session:
            session.connection()  # warm the pool

        start = time.perf_counter()
        with session_scope(factory) as session:
            session.connection()
        elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms <= 1, f"session acquisition took {elapsed_ms:.3f} ms"


def test_pragma_application_budget() -> None:
    """spec §15: pragma application per SQLite connection <= 2 ms."""
    with temporary_sqlite() as engine:
        engine.dispose()  # force the next checkout to re-fire "connect" and reapply pragmas
        start = time.perf_counter()
        with engine.connect() as connection:
            connection.rollback()
        elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms <= 2, f"pragma application took {elapsed_ms:.3f} ms"


def _load_fixture_metadata() -> MetaData:
    sys.path.insert(0, _SCRIPT_LOCATION)
    from models import Base  # type: ignore[import-not-found]

    return Base.metadata  # type: ignore[no-any-return]


def test_check_parity_budget() -> None:
    """spec §15: check_parity <= 2 s."""
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        runner.upgrade(backup=False)
        target_metadata = _load_fixture_metadata()

        start = time.perf_counter()
        runner.check_parity(target_metadata)
        elapsed_s = time.perf_counter() - start
    assert elapsed_s <= 2, f"check_parity took {elapsed_s:.3f} s"


@contextmanager
def _populated_sqlite(*, row_count: int, blob_size_bytes: int) -> Iterator[Engine]:
    with temporary_sqlite() as engine:
        _Base.metadata.create_all(engine)
        factory = session_factory(engine)
        blob = "x" * blob_size_bytes
        batch = 500
        with session_scope(factory) as session:
            for start in range(0, row_count, batch):
                session.add_all(_Payload(blob=blob) for _ in range(min(batch, row_count - start)))
                session.flush()
        yield engine


def test_backup_throughput_budget_1gb(tmp_path: Path) -> None:
    """spec §15: backup of a 1 GB SQLite database <= 30 s."""
    # ~1 GB: 200,000 rows of ~5 KB each.
    with _populated_sqlite(row_count=200_000, blob_size_bytes=5000) as engine:
        destination = tmp_path / "backup.sqlite3"
        start = time.perf_counter()
        backup(engine, destination)
        elapsed_s = time.perf_counter() - start
    size_gb = destination.stat().st_size / (1024**3)
    assert elapsed_s <= 30, f"backup of {size_gb:.2f} GB took {elapsed_s:.1f} s"


def test_integrity_check_budget_1gb() -> None:
    """spec §15: integrity_check on 1 GB <= 60 s."""
    with _populated_sqlite(row_count=200_000, blob_size_bytes=5000) as engine:
        start = time.perf_counter()
        result = integrity_check(engine)
        elapsed_s = time.perf_counter() - start
    assert result.ok
    assert elapsed_s <= 60, f"integrity_check took {elapsed_s:.1f} s"
