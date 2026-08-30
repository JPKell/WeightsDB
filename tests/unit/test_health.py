"""Tests for weightsdb.health."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from sqlalchemy import Engine

from weightsdb import health as health_module
from weightsdb.backup import IntegrityResult
from weightsdb.engine import create_engine_for
from weightsdb.health import is_network_filesystem
from weightsdb.migrations import MigrationRunner
from weightsdb.testing import temporary_sqlite

_SCRIPT_LOCATION = str(Path(__file__).parent.parent / "integration" / "_migration_fixture")


def test_health_at_head() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        runner.upgrade(backup=False)
        report = health_module.database_health(engine, runner)
    assert report.is_at_head is True
    assert report.status == "ok"
    assert report.current_revision == report.head_revision == "0002"
    assert report.dialect == "sqlite"
    assert report.version is not None
    assert report.journal_mode == "wal"
    assert report.integrity_ok is True


def test_health_behind_head() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        runner.upgrade("0001", backup=False)
        report = health_module.database_health(engine, runner)
    assert report.is_at_head is False
    assert report.status == "degraded"
    assert any("pending migration" in reason for reason in report.degraded_reasons)


def test_health_ahead_of_head() -> None:
    """A database written by a newer build is reported distinctly from "behind head"."""
    from sqlalchemy import text

    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        runner.upgrade(backup=False)
        # Simulate a newer build's revision landing in alembic_version directly — Alembic's own
        # `stamp` command validates against the known script directory and would refuse this,
        # exactly as it should for a real operator; only a raw write reproduces "ahead" honestly.
        with engine.connect() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = '9999-future'"))
            connection.commit()
        report = health_module.database_health(engine, runner)
    assert report.is_at_head is False
    assert report.status == "degraded"
    assert any("ahead of this build" in reason for reason in report.degraded_reasons)
    assert not any("pending migration" in reason for reason in report.degraded_reasons)


def test_health_unmigrated_database_reports_no_current_revision() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=_SCRIPT_LOCATION)
        report = health_module.database_health(engine, runner)
    assert report.current_revision is None
    assert report.is_at_head is False


def test_health_without_runner_skips_revision_comparison() -> None:
    with temporary_sqlite() as engine:
        report = health_module.database_health(engine)
    assert report.current_revision is None
    assert report.head_revision is None
    assert report.is_at_head is None
    assert report.status == "ok"


def test_health_integrity_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    with temporary_sqlite() as engine:
        monkeypatch.setattr(
            health_module,
            "integrity_check",
            lambda _engine: IntegrityResult(ok=False, detail="malformed"),
        )
        report = health_module.database_health(engine)
    assert report.integrity_ok is False
    assert report.status == "degraded"
    assert any("integrity check failed" in reason for reason in report.degraded_reasons)


def test_health_low_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    with temporary_sqlite() as engine:
        monkeypatch.setattr(health_module, "_free_space_bytes", lambda _engine: 1024)
        report = health_module.database_health(engine)
    assert report.free_space_bytes == 1024
    assert report.status == "degraded"
    assert any("low disk" in reason for reason in report.degraded_reasons)


def test_health_stale_backup() -> None:
    with temporary_sqlite() as engine:
        database = Path(str(engine.url.database))
        backups = database.parent / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        old_backup = backups / "pre-migration-old.sqlite3"
        old_backup.write_bytes(b"x")
        old_time = time.time() - (10 * 24 * 3600)
        os.utime(old_backup, (old_time, old_time))

        report = health_module.database_health(engine)
    assert report.last_backup_age_seconds is not None
    assert report.last_backup_age_seconds > 9 * 24 * 3600
    assert report.status == "degraded"
    assert any("stale backup" in reason for reason in report.degraded_reasons)


def test_health_fresh_backup_not_stale() -> None:
    with temporary_sqlite() as engine:
        database = Path(str(engine.url.database))
        backups = database.parent / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        (backups / "pre-migration-new.sqlite3").write_bytes(b"x")

        report = health_module.database_health(engine)
    assert report.last_backup_age_seconds is not None
    assert report.last_backup_age_seconds < 60
    assert report.status == "ok"


def test_health_no_backup_at_all_is_not_penalized() -> None:
    with temporary_sqlite() as engine:
        report = health_module.database_health(engine)
    assert report.last_backup_age_seconds is None
    assert report.status == "ok"


def test_health_unreachable_database_is_unavailable_not_raised() -> None:
    engine = create_engine_for("sqlite:///:memory:")
    engine.dispose()

    class _BrokenEngine:
        dialect = engine.dialect

        def connect(self) -> object:
            raise RuntimeError("simulated connection failure")

    report = health_module.database_health(_BrokenEngine())  # type: ignore[arg-type]
    assert report.status == "unavailable"
    assert report.degraded_reasons


def test_network_filesystem_detection_with_mocked_mount_table(tmp_path: Path) -> None:
    nested = tmp_path / "mnt" / "share" / "db.sqlite3"
    mounts = [
        ("/", "ext4"),
        (str(tmp_path / "mnt" / "share"), "nfs4"),
    ]
    assert is_network_filesystem(nested, mounts=mounts) is True


def test_network_filesystem_detection_local_disk(tmp_path: Path) -> None:
    nested = tmp_path / "data" / "db.sqlite3"
    mounts = [
        ("/", "ext4"),
        (str(tmp_path), "ext4"),
    ]
    assert is_network_filesystem(nested, mounts=mounts) is False


def test_network_filesystem_detection_undetermined_with_no_mount_table() -> None:
    assert is_network_filesystem(Path("/some/path"), mounts=[]) is None


def test_network_filesystem_detection_picks_longest_prefix(tmp_path: Path) -> None:
    """A more specific mount point (a network share inside a local root) wins."""
    nested = tmp_path / "mnt" / "nfsshare" / "db.sqlite3"
    mounts = [
        ("/", "ext4"),
        (str(tmp_path), "ext4"),
        (str(tmp_path / "mnt" / "nfsshare"), "nfs"),
    ]
    assert is_network_filesystem(nested, mounts=mounts) is True


def _memory_engine() -> Engine:
    """A SQLite engine with no file behind it — the `:memory:` branch of every helper."""
    return create_engine_for("sqlite://")


def _postgres_engine() -> Engine:
    """A PostgreSQL engine that is never connected; construction opens nothing (spec §15)."""
    return create_engine_for("postgresql+psycopg://u:p@h:5432/db")


def test_proc_mounts_unreadable_is_undetermined_not_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform without a readable /proc/mounts answers "don't know", never "no" (ADR-0016)."""

    def _refuse(*_args: object, **_kwargs: object) -> object:
        raise OSError("no /proc on this platform")

    monkeypatch.setattr(Path, "open", _refuse)
    assert health_module._read_proc_mounts() is None
    assert is_network_filesystem(Path("/srv/data/app.sqlite3")) is None


def test_network_filesystem_undetermined_when_no_mount_point_contains_the_path() -> None:
    """A mount table that simply does not cover the path is undetermined, not "local"."""
    assert (
        is_network_filesystem(Path("/srv/data/app.sqlite3"), mounts=[("/mnt/elsewhere", "nfs")])
        is None
    )


def test_journal_mode_is_none_off_sqlite() -> None:
    """WAL is a SQLite concept; reporting a journal mode for PostgreSQL would be invented."""
    assert health_module._journal_mode(_postgres_engine()) is None


def test_journal_mode_is_none_when_the_pragma_cannot_run(tmp_path: Path) -> None:
    """A path that is a directory cannot be opened as a database; report None, do not raise."""
    engine = create_engine_for(f"sqlite:///{tmp_path}")
    assert health_module._journal_mode(engine) is None


def test_backend_version_is_none_when_the_database_cannot_be_reached() -> None:
    assert health_module._backend_version(_postgres_engine()) is None


def test_free_space_is_none_for_an_in_memory_database() -> None:
    """An in-memory database has no filesystem to report free space on."""
    assert health_module._free_space_bytes(_memory_engine()) is None


def test_free_space_off_sqlite_reports_the_local_filesystem_as_a_proxy() -> None:
    """No local path exists for a remote server, so this reports the process's own filesystem."""
    free = health_module._free_space_bytes(_postgres_engine())
    assert free is not None
    assert free > 0


def test_free_space_walks_up_to_an_existing_ancestor(tmp_path: Path) -> None:
    """A database directory that has gone missing still yields the device's free space.

    `create_engine_for` creates the parent up front, so this removes it again afterwards to
    reach the walk-up: a directory can disappear under a long-lived engine.
    """
    engine = create_engine_for(f"sqlite:///{tmp_path}/gone/deeper/app.sqlite3")
    shutil.rmtree(tmp_path / "gone")
    free = health_module._free_space_bytes(engine)
    assert free is not None
    assert free > 0


def test_free_space_is_none_when_the_filesystem_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(_path: object) -> object:
        raise OSError("stat failed")

    monkeypatch.setattr("weightsdb.health.shutil.disk_usage", _refuse)
    with temporary_sqlite() as engine:
        assert health_module._free_space_bytes(engine) is None


def test_last_backup_age_is_none_off_sqlite_and_in_memory() -> None:
    """The backups directory is discovered relative to the database *file*; neither has one."""
    assert health_module._last_backup_age_seconds(_postgres_engine(), now=time.time()) is None
    assert health_module._last_backup_age_seconds(_memory_engine(), now=time.time()) is None


def test_last_backup_age_ignores_subdirectories_of_the_backups_directory(tmp_path: Path) -> None:
    """Only files are backups. A directory alongside them must not be read as "no backup"."""
    database = tmp_path / "app.sqlite3"
    engine = create_engine_for(f"sqlite:///{database}")
    backups = tmp_path / "backups"
    (backups / "a-directory").mkdir(parents=True)
    assert health_module._last_backup_age_seconds(engine, now=time.time()) is None

    (backups / "app-0001.sqlite3").write_bytes(b"")
    age = health_module._last_backup_age_seconds(engine, now=time.time())
    assert age is not None
    assert age >= 0.0


def test_network_filesystem_for_is_none_off_sqlite_and_in_memory() -> None:
    assert health_module._network_filesystem_for(_postgres_engine()) is None
    assert health_module._network_filesystem_for(_memory_engine()) is None


def test_health_on_a_network_filesystem_is_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Database standards: a SQLite file on NFS is a supported-but-degraded deployment."""
    monkeypatch.setattr(health_module, "_network_filesystem_for", lambda _engine: True)
    with temporary_sqlite() as engine:
        report = health_module.database_health(engine)
    assert report.network_filesystem is True
    assert report.status == "degraded"
    assert any("network filesystem" in reason for reason in report.degraded_reasons)


def test_network_filesystem_detection_when_the_mount_table_is_not_sorted(tmp_path: Path) -> None:
    """/proc/mounts is in mount order, not path order: a later, shallower entry must not win."""
    nested = tmp_path / "mnt" / "nfsshare" / "db.sqlite3"
    mounts = [
        (str(tmp_path / "mnt" / "nfsshare"), "nfs"),
        (str(tmp_path), "ext4"),
        ("/", "ext4"),
    ]
    assert is_network_filesystem(nested, mounts=mounts) is True


def test_last_backup_age_with_two_backups_written_at_the_same_instant(tmp_path: Path) -> None:
    """Equal mtimes: neither displaces the other, and the age is that shared instant's age."""
    database = tmp_path / "app.sqlite3"
    engine = create_engine_for(f"sqlite:///{database}")
    backups = tmp_path / "backups"
    backups.mkdir()
    now = time.time()
    for name in ("app-0001.sqlite3", "app-0002.sqlite3"):
        path = backups / name
        path.write_bytes(b"")
        os.utime(path, (now - 60.0, now - 60.0))

    age = health_module._last_backup_age_seconds(engine, now=now)

    assert age == pytest.approx(60.0, abs=1.0)
