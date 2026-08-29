"""Tests for weightsdb.session."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, text
from sqlalchemy.exc import OperationalError

from weightsdb.errors import DatabaseError
from weightsdb.session import session_factory, session_scope, transaction
from weightsdb.testing import temporary_sqlite

_metadata = MetaData()
_widgets = Table("widgets", _metadata, Column("id", Integer, primary_key=True))


def test_session_scope_commits_on_success() -> None:
    with temporary_sqlite() as engine:
        _metadata.create_all(engine)
        factory = session_factory(engine)
        with session_scope(factory) as session:
            session.execute(_widgets.insert().values(id=1))
        with session_scope(factory) as session:
            count = session.execute(text("SELECT COUNT(*) FROM widgets")).scalar_one()
    assert count == 1


def test_session_scope_rolls_back_on_exception() -> None:
    with temporary_sqlite() as engine:
        _metadata.create_all(engine)
        factory = session_factory(engine)
        with pytest.raises(ValueError):
            with session_scope(factory) as session:
                session.execute(_widgets.insert().values(id=2))
                raise ValueError("boom")
        with session_scope(factory) as session:
            count = session.execute(text("SELECT COUNT(*) FROM widgets")).scalar_one()
    assert count == 0


def test_session_scope_rolls_back_on_keyboard_interrupt() -> None:
    with temporary_sqlite() as engine:
        _metadata.create_all(engine)
        factory = session_factory(engine)
        with pytest.raises(KeyboardInterrupt):
            with session_scope(factory) as session:
                session.execute(_widgets.insert().values(id=3))
                raise KeyboardInterrupt
        with session_scope(factory) as session:
            count = session.execute(text("SELECT COUNT(*) FROM widgets")).scalar_one()
    assert count == 0


def test_session_scope_always_closes() -> None:
    with temporary_sqlite() as engine:
        _metadata.create_all(engine)
        factory = session_factory(engine)
        opened = factory()
        try:
            with session_scope(factory) as session:
                assert session is not opened
        finally:
            opened.close()
        with session_scope(factory) as session:
            assert not session.in_transaction()


def test_transaction_read_only_enforces_no_write() -> None:
    """A write attempted inside transaction(immediate=False) is refused, not silently taken."""
    with temporary_sqlite() as engine:
        _metadata.create_all(engine)
        factory = session_factory(engine)
        with session_scope(factory) as session, transaction(session, immediate=False):
            with pytest.raises(OperationalError):
                session.execute(_widgets.insert().values(id=4))
                session.flush()


def test_transaction_immediate_allows_write() -> None:
    with temporary_sqlite() as engine:
        _metadata.create_all(engine)
        factory = session_factory(engine)
        with session_scope(factory) as session, transaction(session, immediate=True):
            session.execute(_widgets.insert().values(id=5))
        with session_scope(factory) as session:
            count = session.execute(text("SELECT COUNT(*) FROM widgets")).scalar_one()
    assert count == 1


def test_transaction_rejects_nesting() -> None:
    with temporary_sqlite() as engine:
        _metadata.create_all(engine)
        factory = session_factory(engine)
        with session_scope(factory) as session, transaction(session):
            session.execute(text("SELECT 1"))
            with pytest.raises(DatabaseError):
                with transaction(session):
                    pass
