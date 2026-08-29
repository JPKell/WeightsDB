"""Tests for weightsdb.health."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

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
