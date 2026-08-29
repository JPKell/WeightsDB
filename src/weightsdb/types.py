"""weightsdb.types — portable column types, the ULID primary key, and the one sanctioned upsert.

The type behaviour here is a **major** compatibility contract (spec §19): changing how
``UtcDateTime`` or ``PortableJSON`` serialize would require a data migration in every consumer.
Moved from FreeWeight's ``infrastructure.db.types`` and ``infrastructure.db.upsert``, both written
"as if they were WeightsDB's own" modules for exactly this move (ADR-0011); behaviour is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from baseaicore import ValidationError, new_id
from sqlalchemy import JSON, DateTime, Float, String
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from sqlalchemy.engine import Dialect
    from sqlalchemy.orm import DeclarativeBase, Session
    from sqlalchemy.types import TypeEngine

__all__ = ["PortableJSON", "UtcDateTime", "measurement_columns", "ulid_primary_key", "upsert"]


class UtcDateTime(TypeDecorator[datetime]):
    """Stores a timezone-aware UTC instant portably across SQLite and PostgreSQL.

    SQLite has no reliable native storage for a timezone-aware value — its ``DATETIME`` affinity
    stores whatever ``isoformat()`` produces as text, and the default parser does not round-trip a
    UTC-offset suffix cleanly. This type sidesteps that dialect trap entirely rather than working
    around it: every bound value is converted to UTC and stored **naive**, and every value read
    back has UTC tzinfo reattached. The instant stored is identical either way; only the
    representation on disk changes, and the same naive-UTC-underneath approach behaves identically
    on PostgreSQL, so there is exactly one code path for both dialects.

    A naive input is rejected rather than assumed to already be UTC (spec §11.3) — silently
    guessing here is exactly the kind of ambiguity that breaks a timestamp comparison months later,
    on whichever row happened to be written by the one caller that forgot a timezone.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Convert an aware datetime to naive UTC for storage, or raise on a naive input."""
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValidationError(
                "UtcDateTime requires a timezone-aware datetime; got a naive value "
                f"{value!r}. Use baseaicore.utc_now() or attach a timezone explicitly.",
                details={"value": repr(value)},
            )
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Reattach UTC tzinfo to the naive value read back from either dialect."""
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class PortableJSON(TypeDecorator[Any]):
    """JSON storage that becomes ``JSONB`` on PostgreSQL and plain ``JSON`` elsewhere.

    SQLAlchemy's generic ``JSON`` type already round-trips nested structures and unicode correctly
    on SQLite; the only dialect-specific behaviour needed is upgrading to ``JSONB`` on PostgreSQL
    for indexing and containment-query support later in the suite's life.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Return ``JSONB`` on PostgreSQL, else the generic ``JSON`` implementation."""
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


def ulid_primary_key() -> Mapped[str]:
    """Return a ``CHAR(26)`` ULID primary key column, defaulted from :func:`baseaicore.new_id`.

    Time-sortable, safe to expose in URLs and logs, and needs no cross-process sequence
    coordination (database standards §3). The default is a plain callable rather than a
    server-side default so it works identically on SQLite and PostgreSQL without a
    dialect-specific ``DEFAULT`` expression.
    """
    return mapped_column(String(26), primary_key=True, default=new_id)


def measurement_columns(name: str) -> tuple[Mapped[float | None], Mapped[str | None]]:
    """Return the ``(<name>, <name>_unavailable_reason)`` column pair for a ``Measurement`` field.

    ``NULL`` alone never means "not measurable" (ADR-0016); a table storing a value that may be
    ``baseaicore.measurement.UNSUPPORTED`` uses this pair rather than a single nullable column, so
    "not yet measured" and "not measurable here, and here is why" stay distinguishable. The names
    are fixed, not suggested: a metadata scan finds no unpaired measurement column across every
    consumer's tables.

    Args:
        name: The measurement's base name; the reason column is ``f"{name}_unavailable_reason"``.

    Returns:
        A ``(value_column, reason_column)`` pair. The caller assigns them to two class attributes
        named ``name`` and ``f"{name}_unavailable_reason"`` respectively — this function does not
        (and cannot) name the attributes itself, only build the columns that belong under those
        names.
    """
    value_column: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason_column: Mapped[str | None] = mapped_column(String, nullable=True)
    return value_column, reason_column


def upsert(
    session: Session,
    model: type[DeclarativeBase],
    values: dict[str, Any],
    *,
    index_elements: list[str],
    no_update: frozenset[str] = frozenset(),
) -> None:
    """Insert ``values`` as a new row, or update the existing row conflicting on ``index_elements``.

    The dialect-correct ``INSERT ... ON CONFLICT (index_elements) DO UPDATE SET ...`` for a plain
    (non-partial) unique index or constraint — the suite's one sanctioned upsert (ADR-0006).
    Select-then-insert is a race under both dialects, and a hand-written ``ON CONFLICT`` is how a
    dialect-specific variant gets in; this is the single place that emits one.

    The ``UPDATE`` branch touches every column named in ``values`` **except** those listed in
    ``no_update`` — which exists for exactly one kind of column: one that must be set on the
    initial insert from the same clock the caller is already using (so a test can assert its
    value), but must never move on a later sighting, such as a ``first_seen_at``. Passing it as a
    plain keyword to ``Column.default`` would make it non-deterministic in tests; omitting it from
    ``values`` entirely would make it impossible to set to anything but wall-clock time on the very
    first insert. ``no_update`` is how both are true at once. Any column present in neither
    ``values`` nor ``index_elements`` still receives its normal ``Column.default`` on the initial
    insert, exactly as a plain ``INSERT`` would.

    Not usable against a **partial** unique index (one with a ``WHERE`` clause) — PostgreSQL and
    SQLite both require the same ``WHERE`` predicate to be repeated on the conflict target for that
    case, which this function does not thread through; a caller with that shape needs explicit
    update-then-insert logic instead. This function is for the ordinary single-natural-key case.

    Args:
        session: The caller's active session; never opened here.
        model: The mapped class to upsert against.
        values: Column values for the insert; also the conflict ``SET`` clause, minus
            ``no_update``. Must not be empty.
        index_elements: The column names forming the unique index or constraint to conflict on.
        no_update: Columns present in ``values`` (for the insert) that must never appear in the
            conflict ``SET`` clause.

    Raises:
        ValueError: ``values`` is empty, or ``session`` is bound to a dialect other than SQLite or
            PostgreSQL (database standards §2 — only these two are supported).
    """
    if not values:
        raise ValueError("upsert() requires at least one column in `values`.")

    dialect_name = session.get_bind().dialect.name
    if dialect_name == "sqlite":
        sqlite_insert = sqlite.insert(model).values(**values)
        session.execute(
            sqlite_insert.on_conflict_do_update(
                index_elements=index_elements,
                set_={key: sqlite_insert.excluded[key] for key in values if key not in no_update},
            )
        )
    elif dialect_name == "postgresql":
        postgresql_insert = postgresql.insert(model).values(**values)
        session.execute(
            postgresql_insert.on_conflict_do_update(
                index_elements=index_elements,
                set_={
                    key: postgresql_insert.excluded[key] for key in values if key not in no_update
                },
            )
        )
    else:
        raise ValueError(
            f"upsert() supports sqlite and postgresql only; got dialect {dialect_name!r}."
        )
