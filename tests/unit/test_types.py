"""Tests for weightsdb.types: UtcDateTime, PortableJSON, ULIDs, measurement pairs, upsert."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from baseaicore import ValidationError
from sqlalchemy import Engine, Integer, String
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from weightsdb.session import session_factory, session_scope
from weightsdb.testing import temporary_postgres, temporary_sqlite
from weightsdb.types import PortableJSON, UtcDateTime, measurement_columns, ulid_primary_key, upsert


class _Base(DeclarativeBase):
    pass


class _Widget(_Base):
    __tablename__ = "widgets"

    id: Mapped[str] = ulid_primary_key()
    name: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    detail_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    score, score_unavailable_reason = measurement_columns("score")
    hit_count: Mapped[int] = mapped_column(Integer, default=0)


@contextmanager
def _engine_for(dialect: str) -> Iterator[Engine]:
    if dialect == "sqlite":
        with temporary_sqlite() as engine:
            yield engine
    else:
        with temporary_postgres() as engine:
            yield engine


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_utc_datetime_round_trips(dialect: str) -> None:
    with _engine_for(dialect) as engine:
        _Base.metadata.create_all(engine)
        factory = session_factory(engine)
        instant = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)
        with session_scope(factory) as session:
            session.add(_Widget(name="a", created_at=instant))
        with session_scope(factory) as session:
            widget = session.query(_Widget).filter_by(name="a").one()
            assert widget.created_at == instant
            assert widget.created_at.tzinfo is not None
            assert widget.created_at.utcoffset() == timedelta(0)


def test_utc_datetime_rejects_naive_input() -> None:
    """Rejected at the type-decorator level, identically on both dialects (spec §11.3)."""
    naive = datetime(2026, 8, 26, 12, 0, 0)  # noqa: DTZ001 — deliberately naive, this is the point
    with pytest.raises(ValidationError, match="naive"):
        UtcDateTime().process_bind_param(naive, sqlite.dialect())


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_portable_json_round_trips_nested_and_unicode(dialect: str) -> None:
    payload: dict[str, object] = {
        "nested": {"list": [1, 2, {"three": "🎉 café"}]},
        "empty": [],
        "n": None,
    }
    with _engine_for(dialect) as engine:
        _Base.metadata.create_all(engine)
        factory = session_factory(engine)
        with session_scope(factory) as session:
            session.add(_Widget(name="c", created_at=datetime.now(UTC), detail_json=payload))
        with session_scope(factory) as session:
            widget = session.query(_Widget).filter_by(name="c").one()
            assert widget.detail_json == payload


def test_ulid_primary_key_defaults() -> None:
    with temporary_sqlite() as engine:
        _Base.metadata.create_all(engine)
        factory = session_factory(engine)
        with session_scope(factory) as session:
            widget = _Widget(name="d", created_at=datetime.now(UTC))
            session.add(widget)
            session.flush()
            assert widget.id is not None
            assert len(widget.id) == 26


def test_measurement_columns_produces_documented_pair() -> None:
    columns = {column.name for column in _Widget.__table__.columns}
    assert "score" in columns
    assert "score_unavailable_reason" in columns


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_upsert_inserts_then_updates(dialect: str) -> None:
    with _engine_for(dialect) as engine:
        _Base.metadata.create_all(engine)
        factory = session_factory(engine)
        with session_scope(factory) as session:
            upsert(
                session,
                _Widget,
                {"name": "e", "created_at": datetime.now(UTC), "hit_count": 1},
                index_elements=["name"],
            )
        with session_scope(factory) as session:
            upsert(
                session,
                _Widget,
                {"name": "e", "created_at": datetime.now(UTC), "hit_count": 2},
                index_elements=["name"],
            )
        with session_scope(factory) as session:
            rows = session.query(_Widget).filter_by(name="e").all()
            assert len(rows) == 1
            assert rows[0].hit_count == 2


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_upsert_no_update_column_set_only_on_insert(dialect: str) -> None:
    first_seen = datetime(2020, 1, 1, tzinfo=UTC)
    later = datetime(2026, 1, 1, tzinfo=UTC)
    with _engine_for(dialect) as engine:
        _Base.metadata.create_all(engine)
        factory = session_factory(engine)
        with session_scope(factory) as session:
            upsert(
                session,
                _Widget,
                {"name": "f", "created_at": first_seen, "hit_count": 1},
                index_elements=["name"],
                no_update=frozenset({"created_at"}),
            )
        with session_scope(factory) as session:
            upsert(
                session,
                _Widget,
                {"name": "f", "created_at": later, "hit_count": 2},
                index_elements=["name"],
                no_update=frozenset({"created_at"}),
            )
        with session_scope(factory) as session:
            row = session.query(_Widget).filter_by(name="f").one()
            assert row.created_at == first_seen
            assert row.hit_count == 2


def test_upsert_empty_values_rejected() -> None:
    with temporary_sqlite() as engine:
        _Base.metadata.create_all(engine)
        factory = session_factory(engine)
        with session_scope(factory) as session:
            with pytest.raises(ValueError, match="at least one column"):
                upsert(session, _Widget, {}, index_elements=["name"])


def test_upsert_concurrent_writers_produce_one_row_no_error() -> None:
    """Two concurrent writers of the same natural key produce one row and no error (spec §18).

    A 5 s busy_timeout (weightsdb's default) makes SQLite serialize the second writer's
    BEGIN IMMEDIATE behind the first rather than fail — so no retry loop is needed here, only a
    generous per-thread join timeout in case that assumption is ever wrong.
    """
    with temporary_sqlite() as engine:
        _Base.metadata.create_all(engine)
        factory = session_factory(engine)
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker(hit_count: int) -> None:
            try:
                barrier.wait(timeout=5)
                with session_scope(factory) as session:
                    upsert(
                        session,
                        _Widget,
                        {
                            "name": "concurrent",
                            "created_at": datetime.now(UTC),
                            "hit_count": hit_count,
                        },
                        index_elements=["name"],
                    )
            except BaseException as exc:  # noqa: BLE001 — captured for the main thread to re-raise
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(count,)) for count in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors
        with session_scope(factory) as session:
            rows = session.query(_Widget).filter_by(name="concurrent").all()
        assert len(rows) == 1
