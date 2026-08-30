# WeightsDB Adoption Checklist — for FreeWeight Phase 12

FreeWeight's `freeweight/infrastructure/db/{engine,session,types,upsert,migration,backup,errors}.py`
were written "as if they were WeightsDB's own" modules (ADR-0011), specifically so this phase is
mostly deletion. This checklist is written from the actual extraction (LoadCoach Phase 1) — what
moved, what changed shape, and the one gotcha that isn't obvious from a diff.

## 1. What to delete outright

These have no callers outside `freeweight/infrastructure/db/` itself and no historical migration
file references them by import path — delete the file and repoint its call sites at `weightsdb`:

| Delete | Replace with |
|---|---|
| `infrastructure/db/engine.py` | `from weightsdb import create_engine_for` |
| `infrastructure/db/session.py` | `from weightsdb import session_factory, session_scope, transaction` |
| `infrastructure/db/upsert.py` | `from weightsdb import upsert` |
| `infrastructure/db/migration.py` | `from weightsdb import MigrationRunner, MigrationOutcome, ParityResult` |
| `infrastructure/db/backup.py` | `from weightsdb import backup, restore, checkpoint, database_size_bytes, integrity_check, pg_restore_command` and `from weightsdb.backup import sqlite_path, backup_revision, prune_backups, reclaimable_bytes, BackupResult, RestoreResult, IntegrityResult` |
| `infrastructure/db/errors.py` | `from weightsdb import DatabaseError, DatabaseUnavailable, MigrationFailed, MigrationRequired, SchemaAhead, StorageBusy, StorageFull` |

`infrastructure/db/base.py` (`Base`, the naming convention, `utcnow`) is **not** deleted —
WeightsDB owns no table and never will (spec §10), so the declarative base stays FreeWeight's own,
exactly as LoadCoach built its own separately rather than sharing one.

## 2. The one gotcha: `types.py` cannot simply be deleted

Every existing migration revision file under `infrastructure/db/migrations/versions/*.py`
instantiates column types by their historical import path, e.g.:

```python
import freeweight.infrastructure.db.types

sa.Column("created_at", freeweight.infrastructure.db.types.UtcDateTime(), nullable=False)
```

Verified for FreeWeight at the time of writing: all seven revisions, `0001_initial_schema`
through `0007_capability_evidence`, carry an `import freeweight.infrastructure.db.types` — none is
exempt. Re-run `grep -rn "db\.types" src/freeweight/infrastructure/db/migrations/versions/` before
acting on this section, since a revision added after this date would not be covered by that check.

Deleting `types.py` outright breaks every one of those files at import time (Alembic loads the
full revision history to walk it, not just the target). Two options, in order of preference:

* **Preferred — leave a re-export shim.** Replace the *body* of `types.py` with:
  ```python
  """Deprecated: use weightsdb directly in new code. Kept so existing migration revisions
  (which reference this module by its historical import path) keep working unchanged."""

  from weightsdb import PortableJSON, UtcDateTime, measurement_columns, ulid_primary_key

  __all__ = ["PortableJSON", "UtcDateTime", "measurement_columns", "ulid_primary_key"]
  ```
  New code imports from `weightsdb` directly; old migration files keep working with zero edits,
  and migration history — which the suite treats as an immutable record — stays untouched.
* **Alternative — rewrite every historical revision file's import.** Mechanical
  (`freeweight.infrastructure.db.types.X` → `weightsdb.X` in every `versions/*.py`), but it edits
  migration history, which the project otherwise never does. Only worth it if the shim itself is
  judged not worth keeping around long-term; not recommended for this phase.

`ulid_primary_key`'s default (`baseaicore.new_id`) and `measurement_columns`' generated column
names are unchanged, so this is genuinely a like-for-like swap either way — the shim is about
import paths, not behaviour.

## 3. Behaviour changes since the code was copied out (read before assuming a silent swap)

* **`session_scope` lost its `read_only` parameter.** WeightsDB's version is
  `session_scope(factory) -> Iterator[Session]` — commit/rollback/close only. Read-only intent is
  now declared separately with `transaction(session, immediate=False)`, composed *inside* a
  `session_scope` block:
  ```python
  # before
  with session_scope(factory, read_only=True) as session:
      ...
  # after
  with session_scope(factory) as session, transaction(session, immediate=False):
      ...
  ```
  Grep FreeWeight's codebase for `read_only=True` and `read_only=False` — every call site needs
  this rewrite. `read_only=False` call sites can just drop the argument (that's `session_scope`'s
  new default behaviour).
* **SQLite lock contention now raises `StorageBusy`, not a raw `OperationalError`.** FreeWeight's
  original `_on_begin` listener let `sqlite3`'s busy error propagate unwrapped; WeightsDB's version
  translates `SQLITE_BUSY`/`SQLITE_BUSY_SNAPSHOT` into the typed `weightsdb.StorageBusy` (spec
  §13's contract, which FreeWeight's single-consumer version never had to satisfy). If any test or
  error handler catches `sqlalchemy.exc.OperationalError` specifically around a lock-contention
  path, it needs to catch `StorageBusy` instead (or in addition).
* **The read-only execution-option name changed** from `"freeweight_read_only"` to
  `"weightsdb_read_only"` (`weightsdb.engine.READ_ONLY_EXECUTION_OPTION`). Irrelevant unless
  something referenced the raw string rather than the constant — grep to be sure.
* **`migration.py` → `migrations.py`** (plural) is WeightsDB's internal module name; import from
  the package root (`from weightsdb import MigrationRunner`), not the submodule path, and this
  doesn't matter.

## 4. Tests that must still pass, unchanged

Run FreeWeight's full gate after the swap:

```bash
cd ~/ai/suite/FreeWeight && .venv/bin/ruff format --check . && .venv/bin/ruff check . \
  && .venv/bin/mypy src tests && .venv/bin/lint-imports \
  && .venv/bin/python -m pytest -q -m "not live and not performance"
```

Particular attention to:

* `tests/integration/test_migrations.py` — fresh/stepwise/idempotent/downgrade, parity, failure +
  restore. The acid test: `MigrationRunner(engine, script_location=..., version_table=...)
  .check_parity(Base.metadata)` must still report `matches=True` against the **existing** revision
  history — if it doesn't, a type's storage format drifted, which is the one thing spec §19 calls
  a major-version break.
* `tests/integration/test_backup_restore.py` — round-trip, corrupt-backup refusal, disk-full.
* Anything asserting the exact exception type on SQLite lock contention (§3 above).
* Anything calling `session_scope(..., read_only=...)` directly (§3 above) — this is a compile-time
  `TypeError` (unexpected keyword argument), not a silent behaviour change, so it fails loudly.
* `.importlinter`'s existing contracts should need no edits — WeightsDB was already an external
  dependency from FreeWeight's `.importlinter`'s point of view before this phase; only which module
  provides it changes.

## 5. Verification

1. `weightsdb`'s own `MetaData` is still empty and FreeWeight declares no new table as a result of
   this phase — `check_parity` above is the proof.
2. FreeWeight's `pip show weightsdb` resolves to `>=0.2,<0.3` (FreeWeight's existing pin already
   accepts this — nothing to widen).
3. `freeweight db status` and `GET /api/v1/health`'s `database` component still report identically
   to before the swap (byte-for-byte on the JSON shape) — `database_health_component()` can now be
   rewritten to build entirely from `weightsdb.database_health()`, the same simplification LoadCoach
   made at its own Phase 1 (see `loadcoach/services/database.py::database_health_component`), but
   that rewrite is optional for this phase — FreeWeight's own `get_status()`/`DatabaseStatus` still
   has a reason to exist (it reports FreeWeight's own table row counts, which
   `database_health()` cannot know about).
