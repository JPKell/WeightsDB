"""Contract: weightsdb's published surface, in both directions.

Spec §7 fixes the public API and spec §20 fixes what this package must *not* carry. Both are
promises to nine other repositories, so both are asserted here rather than left to review: a name
silently added to ``__all__`` is as much a contract change as one silently removed.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import weightsdb

pytestmark = pytest.mark.contract

# Spec §7's Public API block, transcribed. Editing this list is a deliberate contract change and
# belongs in CHANGELOG.md under the version that makes it.
_PUBLISHED_NAMES = frozenset(
    {
        "BackupResult",
        "DatabaseError",
        "DatabaseHealth",
        "DatabaseUnavailable",
        "IntegrityResult",
        "MigrationFailed",
        "MigrationOutcome",
        "MigrationRequired",
        "MigrationRunner",
        "ParityResult",
        "PortableJSON",
        "RestoreResult",
        "SchemaAhead",
        "StorageBusy",
        "StorageFull",
        "UtcDateTime",
        "__version__",
        "backup",
        "checkpoint",
        "create_engine_for",
        "database_health",
        "database_size_bytes",
        "integrity_check",
        "is_network_filesystem",
        "measurement_columns",
        "pg_restore_command",
        "redact_url",
        "restore",
        "session_factory",
        "session_scope",
        "transaction",
        "ulid_primary_key",
        "upsert",
    }
)


def test_the_published_surface_is_exactly_what_the_spec_names() -> None:
    """Both directions: nothing removed, and nothing added without a contract change."""
    assert set(weightsdb.__all__) == _PUBLISHED_NAMES


def test_every_published_name_actually_resolves() -> None:
    """An `__all__` entry with no attribute behind it breaks `from weightsdb import *` silently."""
    missing = [name for name in weightsdb.__all__ if not hasattr(weightsdb, name)]
    assert missing == []


def test_published_names_are_sorted() -> None:
    """Sorted `__all__` is what makes an addition or removal legible in a diff."""
    assert list(weightsdb.__all__) == sorted(weightsdb.__all__)


def test_the_package_ships_a_pep_561_marker() -> None:
    """Without it a consumer's `mypy --strict` reports every weightsdb import as untyped."""
    marker = Path(inspect.getfile(weightsdb)).parent / "py.typed"
    assert marker.is_file()


def test_the_package_declares_no_schema_of_its_own() -> None:
    """Database standards §1: WeightsDB owns no table and defines no shared declarative base.

    A `MetaData`, `DeclarativeBase` or `Table` appearing here would make every consumer share a
    schema, which is the coupling this package exists to avoid.
    """
    from sqlalchemy import MetaData, Table
    from sqlalchemy.orm import DeclarativeBase

    forbidden = (MetaData, Table)
    offenders = [
        name
        for name in weightsdb.__all__
        if isinstance(getattr(weightsdb, name), forbidden)
        or (
            inspect.isclass(getattr(weightsdb, name))
            and issubclass(getattr(weightsdb, name), DeclarativeBase)
        )
    ]
    assert offenders == []


def test_the_version_is_a_plain_release_string() -> None:
    """Packaging standards: the version is read from `__about__.py`, and the wheel carries it."""
    assert weightsdb.__version__.count(".") == 2
    assert all(part.isdigit() for part in weightsdb.__version__.split("."))
