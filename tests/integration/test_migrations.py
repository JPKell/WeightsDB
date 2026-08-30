"""Tests for weightsdb.migrations.MigrationRunner, against the local migration fixture."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from _migration_fixture.models import Base
from sqlalchemy import text

from weightsdb.backup import checkpoint
from weightsdb.errors import MigrationFailed
from weightsdb.migrations import MigrationRunner
from weightsdb.testing import migration_harness, temporary_postgres, temporary_sqlite

_SCRIPT_LOCATION = str(Path(__file__).parent / "_migration_fixture")


def test_fresh_database_migrates_to_head_sqlite() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        assert runner.current() is None
        outcome = runner.upgrade(backup=False)
        assert outcome.from_revision is None
        assert outcome.to_revision == "0002"
        assert runner.is_at_head()
        with engine.connect() as connection:
            columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(widgets)")).fetchall()
            }
        assert columns == {"id", "name", "note"}


def test_fresh_database_migrates_to_head_postgres() -> None:
    with temporary_postgres() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        outcome = runner.upgrade(backup=False)
        assert outcome.to_revision == "0002"
        assert runner.is_at_head()


def test_stepwise_migration_applies_each_revision_in_order() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        first = runner.upgrade("0001", backup=False)
        assert first.to_revision == "0001"
        with engine.connect() as connection:
            columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(widgets)")).fetchall()
            }
        assert columns == {"id", "name"}
        second = runner.upgrade("0002", backup=False)
        assert second.from_revision == "0001"
        assert second.to_revision == "0002"


def test_upgrade_head_twice_is_idempotent_no_op() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        runner.upgrade(backup=False)
        second = runner.upgrade(backup=False)
        assert second.backed_up is False
        assert second.from_revision == second.to_revision == "0002"


def test_downgrade_removes_the_added_column() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        runner.upgrade(backup=False)
        outcome = runner.downgrade("0001")
        assert outcome.to_revision == "0001"
        with engine.connect() as connection:
            columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(widgets)")).fetchall()
            }
        assert columns == {"id", "name"}


def test_check_parity_matches_after_upgrade() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        runner.upgrade(backup=False)
        result = runner.check_parity(Base.metadata)
        assert result.matches
        assert result.diff == ""


def test_check_parity_detects_a_drifted_model() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        runner.upgrade(backup=False)

        drifted = sa.MetaData()
        sa.Table(
            "widgets",
            drifted,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("note", sa.String(), nullable=True),
            sa.Column("extra_column_no_migration_adds", sa.String(), nullable=True),
        )
        result = runner.check_parity(drifted)
        assert not result.matches
        assert "extra_column_no_migration_adds" in result.diff


def test_failed_migration_on_sqlite_restores_backup_byte_identical(tmp_path: Path) -> None:
    broken_dir = tmp_path / "broken_migrations"
    versions_dir = broken_dir / "versions"
    versions_dir.mkdir(parents=True)
    (broken_dir / "env.py").write_text((Path(_SCRIPT_LOCATION) / "env.py").read_text())
    (broken_dir / "models.py").write_text((Path(_SCRIPT_LOCATION) / "models.py").read_text())
    (versions_dir / "0001_create_widgets.py").write_text(
        (Path(_SCRIPT_LOCATION) / "versions" / "0001_create_widgets.py").read_text()
    )
    (versions_dir / "0002_broken.py").write_text(
        '"""broken\n\nRevision ID: 0002\nRevises: 0001\n"""\n'
        "from __future__ import annotations\n\n"
        'revision: str = "0002"\n'
        'down_revision: str | None = "0001"\n'
        "branch_labels = None\n"
        "depends_on = None\n\n"
        "def upgrade() -> None:\n"
        "    raise RuntimeError('deliberate failure')\n\n"
        "def downgrade() -> None:\n"
        "    pass\n"
    )

    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=str(broken_dir))
        runner.upgrade("0001", backup=False)

        with engine.connect() as connection:
            connection.execute(text("INSERT INTO widgets (name) VALUES ('keepme')"))
            connection.commit()

        with pytest.raises(MigrationFailed) as excinfo:
            runner.upgrade("0002")
        assert excinfo.value.details["restored"] is True

        # "Byte-identical" (spec §11.4) means: the live file after restore is exactly the backup
        # that was taken — restore() is a verbatim shutil.copyfile of it. It does *not* mean the
        # live file's raw bytes match some earlier snapshot's: SQLite's own backup API produces a
        # file whose header change-counter is independent of the source's (a target-side commit,
        # not a mirror of the source's counter), so comparing against a pre-backup snapshot instead
        # of the backup itself would assert something SQLite does not promise.
        backup_path = Path(str(excinfo.value.details["backup_path"]))
        checkpoint(engine)
        restored_bytes = Path(engine.url.database or "").read_bytes()
        assert restored_bytes == backup_path.read_bytes()
        assert runner.current() == "0001"
        with engine.connect() as connection:
            names = [row[0] for row in connection.execute(text("SELECT name FROM widgets"))]
        assert names == ["keepme"]


def test_failed_migration_on_postgres_reports_revision_and_refuses(tmp_path: Path) -> None:
    broken_dir = tmp_path / "broken_migrations_pg"
    versions_dir = broken_dir / "versions"
    versions_dir.mkdir(parents=True)
    (broken_dir / "env.py").write_text((Path(_SCRIPT_LOCATION) / "env.py").read_text())
    (broken_dir / "models.py").write_text((Path(_SCRIPT_LOCATION) / "models.py").read_text())
    (versions_dir / "0001_create_widgets.py").write_text(
        (Path(_SCRIPT_LOCATION) / "versions" / "0001_create_widgets.py").read_text()
    )
    (versions_dir / "0002_broken.py").write_text(
        '"""broken\n\nRevision ID: 0002\nRevises: 0001\n"""\n'
        "from __future__ import annotations\n\n"
        'revision: str = "0002"\n'
        'down_revision: str | None = "0001"\n'
        "branch_labels = None\n"
        "depends_on = None\n\n"
        "def upgrade() -> None:\n"
        "    raise RuntimeError('deliberate failure')\n\n"
        "def downgrade() -> None:\n"
        "    pass\n"
    )

    with temporary_postgres() as engine:
        runner = MigrationRunner(engine, script_location=str(broken_dir))
        runner.upgrade("0001", backup=False)

        with pytest.raises(MigrationFailed) as excinfo:
            runner.upgrade("0002")
        details = excinfo.value.details
        assert details["restored"] is False
        assert details["reached_revision"] == "0001"
        assert details["restore_command"] is None
        assert runner.current() == "0001"


def test_migration_harness_drives_a_full_upgrade_on_sqlite() -> None:
    """The helper spec §7 names, exercised by a real consumer for the first time (LC5).

    `migration_harness` bundles a script location and metadata so a consumer's migration tests
    do not repeat the `MigrationRunner(engine, script_location=...)` construction per test.
    """
    harness = migration_harness(_SCRIPT_LOCATION, Base.metadata)
    assert harness.script_location == _SCRIPT_LOCATION
    assert harness.metadata is Base.metadata

    with harness.sqlite() as runner:
        assert runner.current() is None
        runner.upgrade(backup=False)
        assert runner.is_at_head()
        assert runner.check_parity(harness.metadata).matches


def test_migration_harness_yields_an_independent_database_each_time() -> None:
    """Each `sqlite()` block is a fresh temporary database, so tests cannot leak into each other."""
    harness = migration_harness(_SCRIPT_LOCATION, Base.metadata)
    with harness.sqlite() as first:
        first.upgrade(backup=False)
        assert first.is_at_head()
    with harness.sqlite() as second:
        assert second.current() is None, "a second block must not see the first block's schema"


def test_migration_harness_postgres_block_runs_the_same_history() -> None:
    harness = migration_harness(_SCRIPT_LOCATION, Base.metadata)
    with harness.postgres() as runner:
        runner.upgrade(backup=False)
        assert runner.is_at_head()


def test_stamp_marks_a_revision_without_running_it() -> None:
    """Recovery only: the schema is asserted to already match, and no migration is executed."""
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        runner.stamp("0001")
        assert runner.current() == "0001"
        with engine.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).fetchall()
            }
    assert "widgets" not in tables, "stamp must record the revision, never execute it"
