# WeightsDB — Quickstart

WeightsDB is plumbing, not a framework: an engine, a session, a migration runner, backup/restore
and a health report. It owns no table and reads no configuration file — every value comes from
your own application's settings, passed in as plain arguments.

## Install

```bash
pip install weightsdb
# or, for PostgreSQL support:
pip install "weightsdb[postgres]"
```

WeightsDB ships a PEP 561 `py.typed` marker, so `mypy --strict` in your own project reads its
annotations from the installed wheel — no stub package, and no `ignore_missing_imports` entry.

## 1. Build an engine

```python
from weightsdb import create_engine_for

engine = create_engine_for("sqlite:///./myapp.sqlite3")
# or: create_engine_for("postgresql+psycopg://user:pass@host/db", statement_timeout_ms=30_000)
```

`create_engine_for` applies the suite's dialect-correct settings (WAL, `busy_timeout`,
`foreign_keys=ON` on SQLite; `statement_timeout`/`application_name` on PostgreSQL) on every
connection the pool opens, including after a reconnect. Construction opens nothing — the first
real connection happens on first use.

## 2. Declare your own schema

WeightsDB never defines a declarative base — that would give it application-shaped opinions about
your schema. Build your own, using WeightsDB's portable column types:

```python
from datetime import datetime

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from weightsdb import PortableJSON, UtcDateTime, ulid_primary_key, measurement_columns


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = ulid_primary_key()  # CHAR(26) ULID, auto-generated
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    payload_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    # A measurement that may be unavailable gets the documented (value, reason) pair:
    duration_ms, duration_ms_unavailable_reason = measurement_columns("duration_ms")
```

## 3. Sessions and transactions

```python
from weightsdb import session_factory, session_scope, transaction

factory = session_factory(engine)

# A write — the default; BEGIN IMMEDIATE on SQLite, so lock contention fails fast.
with session_scope(factory) as session:
    session.add(Job(created_at=..., payload_json={"hello": "world"}))

# A read — declares intent so concurrent readers don't queue behind the write lock under WAL.
with session_scope(factory) as session, transaction(session, immediate=False):
    jobs = session.query(Job).all()
```

`session_scope` is the unit of work: commit on success, rollback on any exception (including
`KeyboardInterrupt`), always close. `transaction` only declares how the SQLite transaction begins;
it composes inside a `session_scope` block rather than replacing it.

## 4. The one sanctioned upsert

```python
from weightsdb import upsert

with session_scope(factory) as session:
    upsert(
        session,
        Job,
        {"id": "01ABC...", "created_at": some_datetime, "payload_json": {"n": 1}},
        index_elements=["id"],
    )
```

Never write `INSERT ... ON CONFLICT` by hand and never select-then-insert (a race under both
dialects) — this is the one place a dialect-correct upsert is emitted.

## 5. Migrations

Point Alembic's `env.py` at your own metadata (see `docs/adoption-checklist.md` for a full example
adapted from FreeWeight's), then drive it through `MigrationRunner` — never the bare `alembic` CLI:

```python
from weightsdb import MigrationRunner

runner = MigrationRunner(engine, script_location="myapp/migrations")
outcome = runner.upgrade()  # backs up first on SQLite, restores automatically on failure
print(outcome.from_revision, "->", outcome.to_revision)

parity = runner.check_parity(Base.metadata)
assert parity.matches, parity.diff  # CI: a model changed without a migration
```

## 6. Backup, restore, health

```python
from pathlib import Path
from weightsdb import backup, restore, database_health

result = backup(engine, Path("./backups/manual.sqlite3"))
restore(engine, result.path, confirm=True)  # confirm=True is required; there is no implicit path

report = database_health(engine, runner)
print(report.status, report.degraded_reasons)
```

`database_health()` never raises — a database it cannot reach comes back as
`status="unavailable"`, not an exception — and is meant to be the single source your own
application's `GET /health` and `<app> health` both report the `database` component from.

## 7. Testing your own application against WeightsDB

```python
from weightsdb.testing import temporary_sqlite, temporary_postgres, migration_harness


def test_my_migrations():
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location="myapp/migrations")
        runner.upgrade(backup=False)
        assert runner.check_parity(Base.metadata).matches
```

`temporary_postgres()` skips when no PostgreSQL server is reachable at `WEIGHTSDB_POSTGRES_URL`
(default `postgresql+psycopg://weightsdb:weightsdb@localhost:5432/weightsdb_test`) — set
`WEIGHTSDB_REQUIRE_POSTGRES=1` in CI to turn that skip into a failure instead.

See `docs/packages/weightsdb/spec.md` for the full contract, including error behaviour,
performance budgets and the compatibility guarantees each type makes.
