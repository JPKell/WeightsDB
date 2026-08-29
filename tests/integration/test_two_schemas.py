"""Two independent schemas coexist against one WeightsDB, with zero coupling (spec §7, §20.1)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from weightsdb.session import session_factory, session_scope
from weightsdb.testing import temporary_sqlite
from weightsdb.types import UtcDateTime, ulid_primary_key


class _AliceBase(DeclarativeBase):
    """Stands in for one application's own declarative base — e.g. FreeWeight's."""


class _BobBase(DeclarativeBase):
    """Stands in for a second, unrelated application's own declarative base — e.g. LoadCoach's."""


class _AliceModel(_AliceBase):
    __tablename__ = "alice_models"

    id: Mapped[str] = ulid_primary_key()
    canonical_id: Mapped[str] = mapped_column(String, unique=True)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class _BobJob(_BobBase):
    __tablename__ = "bob_jobs"

    id: Mapped[str] = ulid_primary_key()
    priority: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


def test_two_independent_schemas_coexist_in_one_database() -> None:
    with temporary_sqlite() as engine:
        _AliceBase.metadata.create_all(engine)
        _BobBase.metadata.create_all(engine)

        factory = session_factory(engine)
        now = datetime.now(UTC)
        with session_scope(factory) as session:
            session.add(_AliceModel(canonical_id="ollama/qwen:9b", first_seen_at=now))
            session.add(_BobJob(priority=5, queued_at=now))

        with session_scope(factory) as session:
            models = session.query(_AliceModel).all()
            jobs = session.query(_BobJob).all()
        assert len(models) == 1
        assert len(jobs) == 1

    # Neither schema's metadata knows about the other's tables.
    assert "bob_jobs" not in _AliceBase.metadata.tables
    assert "alice_models" not in _BobBase.metadata.tables


def test_weightsdb_declares_no_table() -> None:
    """weightsdb's own metadata is empty — asserted (spec §20.2): it owns no table and no row."""
    import weightsdb

    assert not hasattr(weightsdb, "Base")
    assert not hasattr(weightsdb, "metadata")
    for name in weightsdb.__all__:
        assert "Base" not in name
        assert name not in ("MetaData", "Table")
