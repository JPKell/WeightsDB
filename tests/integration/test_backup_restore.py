"""Tests for weightsdb.backup: backup, restore, rotation, integrity, disk-full handling."""

from __future__ import annotations

import gzip
import os
import shlex
import shutil
import sqlite3
import subprocess
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine, create_mock_engine, text

from weightsdb.backup import (
    IntegrityResult,
    backup,
    backup_revision,
    checkpoint,
    database_size_bytes,
    integrity_check,
    pg_restore_command,
    prune_backups,
    reclaimable_bytes,
    restore,
    sqlite_path,
)
from weightsdb.engine import create_engine_for
from weightsdb.errors import DatabaseError, StorageFull
from weightsdb.testing import temporary_postgres, temporary_sqlite

# `weightsdb/__init__.py` re-exports the *function* `backup`, which shadows the submodule of
# the same name: neither `from weightsdb import backup` nor monkeypatch's dotted-string form
# can reach `weightsdb.backup`'s own globals. import_module returns the module itself.
_backup_module = import_module("weightsdb.backup")


def _seed(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(text("INSERT INTO t (name) VALUES ('a')"))
        connection.commit()


def test_backup_and_restore_round_trip(tmp_path: Path) -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        destination = tmp_path / "backup.sqlite3"
        result = backup(engine, destination)
        assert result.path == destination
        assert result.dialect == "sqlite"
        assert destination.is_file()

        with engine.connect() as connection:
            connection.execute(text("INSERT INTO t (name) VALUES ('b')"))
            connection.commit()

        restore_result = restore(engine, destination, confirm=True)
        assert restore_result.path is not None
        with engine.connect() as connection:
            names = [row[0] for row in connection.execute(text("SELECT name FROM t"))]
        assert names == ["a"]


def test_restore_requires_confirm() -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        with pytest.raises(DatabaseError, match="confirm"):
            restore(engine, Path("/nonexistent"), confirm=False)


def test_backup_of_database_with_open_write_transaction_succeeds(tmp_path: Path) -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        writer = engine.connect()
        writer.execute(text("INSERT INTO t (name) VALUES ('mid-transaction')"))
        # Not committed yet — backup must still succeed and be internally consistent.
        destination = tmp_path / "live-backup.sqlite3"
        result = backup(engine, destination)
        writer.rollback()
        writer.close()

        check = sqlite3.connect(destination)
        try:
            row = check.execute("PRAGMA integrity_check").fetchone()
        finally:
            check.close()
        assert row[0] == "ok"
        assert result.path.is_file()


def test_restore_of_corrupt_backup_refused_before_touching_live_database(tmp_path: Path) -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        corrupt = tmp_path / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a sqlite database")

        with pytest.raises(DatabaseError, match="integrity check"):
            restore(engine, corrupt, confirm=True)

        with engine.connect() as connection:
            names = [row[0] for row in connection.execute(text("SELECT name FROM t"))]
        assert names == ["a"]


def test_restore_refuses_backup_at_unknown_revision(tmp_path: Path) -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        destination = tmp_path / "backup.sqlite3"
        backup(engine, destination)
        with sqlite3.connect(destination) as connection:
            connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            connection.execute("INSERT INTO alembic_version VALUES ('9999-unknown')")
            connection.commit()

        with pytest.raises(DatabaseError, match="unknown|does not contain"):
            restore(engine, destination, confirm=True, known_revisions=frozenset({"0001"}))


def test_disk_full_during_backup_removes_partial_file_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        destination = tmp_path / "backup.sqlite3"

        class _FullDiskConnection:
            def backup(self, _target: object) -> None:
                raise sqlite3.OperationalError("database or disk is full")

            def close(self) -> None:
                pass

        original_connect = sqlite3.connect

        def _fake_connect(path: Any, *args: Any, **kwargs: Any) -> Any:
            if str(path) == str(engine.url.database):
                return _FullDiskConnection()
            return original_connect(path, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", _fake_connect)
        with pytest.raises(StorageFull):
            backup(engine, destination)
    assert not destination.exists()


def test_prune_backups_keeps_only_the_newest(tmp_path: Path) -> None:
    import time

    paths = []
    for index in range(5):
        path = tmp_path / f"pre-migration-000{index}.sqlite3"
        path.write_bytes(b"x")
        paths.append(path)
        time.sleep(0.01)

    pruned = prune_backups(tmp_path, prefix="pre-migration-", keep=2)
    assert len(pruned) == 3
    assert pruned == tuple(paths[:3])
    remaining = sorted(tmp_path.iterdir())
    assert remaining == sorted(paths[3:])


def test_prune_backups_never_touches_operator_named_files(tmp_path: Path) -> None:
    named = tmp_path / "my-own-backup.sqlite3"
    named.write_bytes(b"x")
    auto = tmp_path / "pre-migration-0001.sqlite3"
    auto.write_bytes(b"x")

    prune_backups(tmp_path, prefix="pre-migration-", keep=0)
    assert named.exists()
    assert not auto.exists()


def test_backup_revision_reads_version_table(tmp_path: Path) -> None:
    destination = tmp_path / "versioned.sqlite3"
    with sqlite3.connect(destination) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('0002')")
        connection.commit()
    assert backup_revision(destination) == "0002"


def test_backup_revision_none_when_no_version_table(tmp_path: Path) -> None:
    destination = tmp_path / "unversioned.sqlite3"
    with sqlite3.connect(destination) as connection:
        connection.execute("CREATE TABLE t (id INTEGER)")
        connection.commit()
    assert backup_revision(destination) is None


def test_integrity_check_reports_ok() -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        result = integrity_check(engine)
        assert result.ok
        assert result.detail == "ok"


def test_database_size_bytes_nonzero_after_seed() -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        assert database_size_bytes(engine) > 0


def test_restore_refuses_on_postgresql_and_names_the_command(tmp_path: Path) -> None:
    """No automatic PostgreSQL restore (spec §11.4): refuses and names pg_restore.

    No server needed — the check happens before any connection is opened.
    """
    engine = create_engine_for("postgresql+psycopg://user:pass@localhost/db")
    try:
        with pytest.raises(DatabaseError, match="pg_restore"):
            restore(engine, tmp_path / "whatever.dump", confirm=True)
    finally:
        engine.dispose()


def test_pg_restore_command_names_host_port_user_and_database() -> None:
    engine = create_engine_for("postgresql+psycopg://alice:secret@dbhost:5433/loadcoach")
    try:
        command = pg_restore_command(engine, Path("/backups/loadcoach.dump"))
    finally:
        engine.dispose()
    assert "dbhost" in command
    assert "5433" in command
    assert "alice" in command
    assert "loadcoach" in command
    assert "/backups/loadcoach.dump" in command
    assert "secret" not in command


def test_backup_and_restore_round_trip_postgresql(tmp_path: Path) -> None:
    with temporary_postgres() as engine:
        _seed(engine)
        destination = tmp_path / "backup.dump"
        result = backup(engine, destination)
        assert result.dialect == "postgresql"
        assert destination.is_file()

        with engine.connect() as connection:
            connection.execute(text("INSERT INTO t (name) VALUES ('b')"))
            connection.commit()

        # restore() itself refuses on PostgreSQL (spec §11.4); the backup file is exercised via
        # pg_restore directly, proving the archive produced by backup() is a real, usable dump.
        pg_restore_available = shutil.which("pg_restore") is not None
        if not pg_restore_available:
            pytest.skip("pg_restore is not installed; backup() output cannot be verified")
        restore_command = pg_restore_command(engine, destination)
        subprocess.run(  # noqa: S603
            [*shlex.split(restore_command)],
            check=True,
            capture_output=True,
            env={**os.environ, "PGPASSWORD": engine.url.password or ""},
        )
        with engine.connect() as connection:
            names = sorted(row[0] for row in connection.execute(text("SELECT name FROM t")))
        assert names == ["a"]


def _postgres_engine() -> Engine:
    """A PostgreSQL engine that is never connected — construction opens nothing (spec §15)."""
    return create_engine_for("postgresql+psycopg://u:p@h:5432/appdb")


# --- sqlite_path / checkpoint -------------------------------------------------------------


def test_sqlite_path_refuses_a_non_sqlite_engine() -> None:
    with pytest.raises(DatabaseError, match="Expected a SQLite engine"):
        sqlite_path(_postgres_engine())


def test_sqlite_path_refuses_an_in_memory_database() -> None:
    """An in-memory database has no file to copy; an "empty backup" would look successful."""
    with pytest.raises(DatabaseError, match="in-memory"):
        sqlite_path(create_engine_for("sqlite://"))


def test_checkpoint_is_a_no_op_off_sqlite() -> None:
    """WAL is a SQLite concept — this must not try to connect to the PostgreSQL server."""
    checkpoint(_postgres_engine())


def test_checkpoint_failure_is_logged_and_does_not_abort_the_caller(tmp_path: Path) -> None:
    """A database too damaged to checkpoint is exactly the one a restore is about to replace."""
    engine = create_engine_for(f"sqlite:///{tmp_path}")  # a directory, not a database
    checkpoint(engine)


# --- backup_revision ----------------------------------------------------------------------


def test_backup_revision_refuses_a_version_table_that_is_not_an_identifier() -> None:
    with pytest.raises(DatabaseError, match="Invalid version table"):
        backup_revision(Path("unused.sqlite3"), version_table="alembic_version; DROP TABLE t")


def test_backup_revision_reports_a_backup_it_cannot_open(tmp_path: Path) -> None:
    with pytest.raises(DatabaseError, match="could not be opened"):
        backup_revision(tmp_path / "no" / "such" / "file.sqlite3")


# --- backup -----------------------------------------------------------------------------


def test_backup_keep_without_prefix_is_refused(tmp_path: Path) -> None:
    """Rotation without a prefix would make every file in the directory a deletion candidate."""
    with temporary_sqlite() as engine:
        with pytest.raises(DatabaseError, match="requires a prefix"):
            backup(engine, tmp_path / "backup.sqlite3", keep=3)


def test_backup_refuses_when_the_database_file_does_not_exist(tmp_path: Path) -> None:
    """An empty backup of a database that was never created looks like a successful one."""
    engine = create_engine_for(f"sqlite:///{tmp_path}/never-created.sqlite3")
    with pytest.raises(DatabaseError, match="no database at"):
        backup(engine, tmp_path / "backup.sqlite3")


def test_backup_reports_a_non_disk_operational_error_as_a_plain_database_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a genuinely out-of-space failure becomes StorageFull; nothing else is miscast."""
    with temporary_sqlite() as engine:
        _seed(engine)
        destination = tmp_path / "backup.sqlite3"

        class _ReadOnlyConnection:
            def backup(self, _target: object) -> None:
                raise sqlite3.OperationalError("attempt to write a readonly database")

            def close(self) -> None:
                pass

        original_connect = sqlite3.connect

        def _fake_connect(path: Any, *args: Any, **kwargs: Any) -> Any:
            if str(path) == str(engine.url.database):
                return _ReadOnlyConnection()
            return original_connect(path, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", _fake_connect)
        with pytest.raises(DatabaseError) as caught:
            backup(engine, destination)
    assert not isinstance(caught.value, StorageFull)
    assert not destination.exists()


def test_backup_compressed_writes_only_the_gzip_and_it_restores(tmp_path: Path) -> None:
    with temporary_sqlite() as engine:
        _seed(engine)
        destination = tmp_path / "backup.sqlite3"
        result = backup(engine, destination, compress=True)

    assert result.path == Path(f"{destination}.gz")
    assert result.path.is_file()
    assert not destination.exists(), "the uncompressed intermediate must not be left behind"
    with gzip.open(result.path, "rb") as archive:
        assert archive.read(16).startswith(b"SQLite format 3")


def test_backup_compression_failure_leaves_no_half_written_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial ".gz" beside a real backup would look like a backup and restore as nothing."""

    def _fail(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("no space left on device")

    monkeypatch.setattr(_backup_module.shutil, "copyfileobj", _fail)
    with temporary_sqlite() as engine:
        _seed(engine)
        destination = tmp_path / "backup.sqlite3"
        with pytest.raises(DatabaseError, match="Compressing backup"):
            backup(engine, destination, compress=True)
    assert not Path(f"{destination}.gz").exists()


def test_backup_on_postgresql_invokes_pg_dump_in_custom_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive format and the credential handling are the contract worth pinning."""
    invocations: list[tuple[list[str], dict[str, str] | None]] = []

    def _record(command: list[str], **kwargs: Any) -> Any:
        invocations.append((command, kwargs.get("env")))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(_backup_module.subprocess, "run", _record)
    destination = tmp_path / "backup.dump"
    result = backup(_postgres_engine(), destination)

    assert result.dialect == "postgresql"
    assert destination.is_file()
    command, env = invocations[0]
    assert command[:2] == ["pg_dump", "--format=custom"]
    assert f"--file={destination}" in command
    assert "--host=h" in command
    assert "--port=5432" in command
    assert "--username=u" in command
    assert command[-1] == "appdb"
    assert env is not None
    assert env["PGPASSWORD"] == "p", "the password goes in the environment, never in argv"
    assert not any("p" == part.split("=")[-1] for part in command if part.startswith("--password"))


def test_backup_on_postgresql_reports_a_missing_pg_dump_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _missing(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("pg_dump")

    monkeypatch.setattr(_backup_module.subprocess, "run", _missing)
    destination = tmp_path / "backup.dump"
    with pytest.raises(DatabaseError, match="pg_dump is not installed"):
        backup(_postgres_engine(), destination)
    assert not destination.exists()


def test_backup_on_postgresql_surfaces_pg_dumps_own_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(command: list[str], **_kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(1, command, b"", b"FATAL: role does not exist")

    monkeypatch.setattr(_backup_module.subprocess, "run", _fail)
    destination = tmp_path / "backup.dump"
    with pytest.raises(DatabaseError, match="role does not exist"):
        backup(_postgres_engine(), destination)
    assert not destination.exists()


# --- prune_backups ------------------------------------------------------------------------


def test_prune_backups_refuses_negative_retention(tmp_path: Path) -> None:
    with pytest.raises(DatabaseError, match="must not be negative"):
        prune_backups(tmp_path, prefix="auto-", keep=-1)


def test_prune_backups_on_a_missing_directory_prunes_nothing(tmp_path: Path) -> None:
    assert prune_backups(tmp_path / "no-such-directory", prefix="auto-", keep=1) == ()


# --- restore ------------------------------------------------------------------------------


def test_restore_refuses_a_source_that_does_not_exist(tmp_path: Path) -> None:
    with temporary_sqlite() as engine:
        with pytest.raises(DatabaseError, match="does not exist"):
            restore(engine, tmp_path / "no-such-backup.sqlite3", confirm=True)


def test_restore_onto_a_database_that_does_not_exist_yet(tmp_path: Path) -> None:
    """A fresh install restoring someone else's backup has no original to copy aside."""
    source = tmp_path / "backup.sqlite3"
    with temporary_sqlite() as origin:
        _seed(origin)
        backup(origin, source)

    target = tmp_path / "fresh" / "app.sqlite3"
    engine = create_engine_for(f"sqlite:///{target}")
    target.unlink(missing_ok=True)
    result = restore(engine, source, confirm=True)

    assert result.path == target
    assert not Path(f"{target}.pre-restore").exists()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM t")).scalar_one() == "a"


def test_restore_puts_the_original_back_when_the_restored_file_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback path: the database being replaced must survive a bad restore."""
    source = tmp_path / "backup.sqlite3"
    database = tmp_path / "app.sqlite3"
    engine = create_engine_for(f"sqlite:///{database}")
    _seed(engine)
    backup(engine, source)
    with engine.connect() as connection:
        connection.execute(text("INSERT INTO t (name) VALUES ('b')"))
        connection.commit()
    original_rows = 2

    # Verification of the *restored* file fails; verification of the source, which happens
    # earlier through _verify_backup_file, is untouched.
    monkeypatch.setattr(
        _backup_module,
        "integrity_check",
        lambda _engine: IntegrityResult(ok=False, detail="database disk image is malformed"),
    )
    with pytest.raises(DatabaseError, match="original database has been put back"):
        restore(engine, source, confirm=True)

    monkeypatch.undo()
    assert not Path(f"{database}.pre-restore").exists()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM t")).scalar_one() == original_rows


def test_restore_reports_a_backup_it_cannot_open(tmp_path: Path) -> None:
    """A backup file the process may not read is reported as such, not as a corrupt one."""
    unreadable = tmp_path / "unreadable.sqlite3"
    unreadable.write_bytes(b"SQLite format 3\x00")
    unreadable.chmod(0o000)
    try:
        with temporary_sqlite() as engine:
            with pytest.raises(DatabaseError, match="could not be opened"):
                restore(engine, unreadable, confirm=True)
    finally:
        unreadable.chmod(0o600)


def test_restore_refuses_a_source_that_is_not_a_database(tmp_path: Path) -> None:
    """SQLite reports this either as a bad row or by refusing the pragma; both are one refusal."""
    source = tmp_path / "not-a-database.sqlite3"
    source.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
    with temporary_sqlite() as engine:
        with pytest.raises(DatabaseError, match="integrity check"):
            restore(engine, source, confirm=True)


# --- integrity_check / size / reclaimable --------------------------------------------------


def test_integrity_check_reports_an_unopenable_database_rather_than_raising(
    tmp_path: Path,
) -> None:
    """Every caller acts on a failed check; half of them cannot if it arrives as an exception."""
    engine = create_engine_for(f"sqlite:///{tmp_path}")  # a directory, not a database
    result = integrity_check(engine)
    assert result.ok is False
    assert result.detail


def test_database_size_of_an_in_memory_database_is_zero() -> None:
    """A size report must not be the thing that breaks a status command."""
    assert database_size_bytes(create_engine_for("sqlite://")) == 0


def test_reclaimable_bytes_is_zero_off_sqlite() -> None:
    """PostgreSQL has no cheap estimate, so this reports 0 rather than guessing."""
    assert reclaimable_bytes(_postgres_engine()) == 0


def test_reclaimable_bytes_grows_after_a_delete_and_is_zero_after_vacuum(tmp_path: Path) -> None:
    database = tmp_path / "app.sqlite3"
    engine = create_engine_for(f"sqlite:///{database}")
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE big (id INTEGER PRIMARY KEY, blob TEXT)"))
        for index in range(500):
            connection.execute(
                text("INSERT INTO big (blob) VALUES (:blob)"), {"blob": "x" * 500, "id": index}
            )
        connection.commit()
    assert reclaimable_bytes(engine) == 0

    with engine.connect() as connection:
        connection.execute(text("DELETE FROM big"))
        connection.commit()
    assert reclaimable_bytes(engine) > 0

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("VACUUM")
    assert reclaimable_bytes(engine) == 0


def test_backup_refuses_a_dialect_that_is_neither_sqlite_nor_postgresql(tmp_path: Path) -> None:
    """`create_engine_for` refuses a third dialect, so this branch is defence in depth."""
    mock = cast("Engine", create_mock_engine("mysql://", lambda *_a, **_kw: None))
    with pytest.raises(DatabaseError, match="Unsupported dialect 'mysql'"):
        backup(mock, tmp_path / "backup.dump")


def test_restore_refuses_a_backup_whose_integrity_check_reports_bad_pages(tmp_path: Path) -> None:
    """The other shape of corruption: the pragma runs and answers with something other than "ok"."""
    source = tmp_path / "corrupt.sqlite3"
    with temporary_sqlite() as origin:
        _seed(origin)
        with origin.connect() as connection:
            connection.execute(text("CREATE INDEX ix_t_name ON t (name)"))
            for index in range(2000):
                connection.execute(
                    text("INSERT INTO t (name) VALUES (:name)"), {"name": f"row-{index}"}
                )
            connection.commit()
        backup(origin, source)

    # Zero the final page. The file still opens and the pragma still runs — it answers with the
    # damaged tree rather than refusing, which is the other of the two shapes corruption takes.
    raw = bytearray(source.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big") or 4096
    raw[-page_size:] = b"\x00" * page_size
    source.write_bytes(bytes(raw))

    with temporary_sqlite() as engine:
        with pytest.raises(DatabaseError, match="failed its integrity check"):
            restore(engine, source, confirm=True)
