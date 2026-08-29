"""weightsdb — shared SQLAlchemy + Alembic plumbing: engines, sessions, pragmas, migrations, backup.

No application table, no shared schema (database standards §1). See
``docs/packages/weightsdb/spec.md`` for the full contract.
"""

from __future__ import annotations

from weightsdb.__about__ import __version__
from weightsdb.backup import (
    BackupResult,
    IntegrityResult,
    RestoreResult,
    backup,
    checkpoint,
    database_size_bytes,
    integrity_check,
    pg_restore_command,
    restore,
)
from weightsdb.engine import create_engine_for
from weightsdb.errors import (
    DatabaseError,
    DatabaseUnavailable,
    MigrationFailed,
    MigrationRequired,
    SchemaAhead,
    StorageBusy,
    StorageFull,
)
from weightsdb.migrations import MigrationOutcome, MigrationRunner, ParityResult
from weightsdb.redaction import redact_url
from weightsdb.session import session_factory, session_scope, transaction
from weightsdb.types import PortableJSON, UtcDateTime, measurement_columns, ulid_primary_key, upsert

__all__ = [
    "BackupResult",
    "DatabaseError",
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
    "database_size_bytes",
    "integrity_check",
    "measurement_columns",
    "pg_restore_command",
    "redact_url",
    "restore",
    "session_factory",
    "session_scope",
    "transaction",
    "ulid_primary_key",
    "upsert",
]
