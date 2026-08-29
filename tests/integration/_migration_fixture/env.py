"""Alembic environment for a test-only migration history, exercising weightsdb.migrations.

Mirrors the shape every real consumer's own ``env.py`` uses: always run through
:class:`weightsdb.migrations.MigrationRunner`, never through the bare ``alembic`` CLI —
``config.attributes["connection"]`` is always populated by the runner with an already-open
connection from a weightsdb-configured engine.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import context

# This fixture is not an installed package (unlike a real consumer's own models module, which
# always is), so its directory is not on sys.path merely because weightsdb imported it — add it
# explicitly, exactly once, before importing its sibling `models.py`.
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import models as _models  # type: ignore[import-not-found] # noqa: F401,E402 — sys.path trick above
from models import Base  # noqa: E402

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    """Run migrations against the connection the caller placed in ``config.attributes``."""
    connection = config.attributes["connection"]
    version_table = config.attributes.get("version_table", "alembic_version")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=version_table,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
