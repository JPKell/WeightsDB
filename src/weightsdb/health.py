"""weightsdb.health — the ``database`` component every application's health endpoint reports from.

Never raises: a health check that itself crashes takes the whole health endpoint down with it,
which is exactly the outcome graceful degradation exists to prevent (the same convention FreeWeight
and LoadCoach already apply at their own health-report layer, moved one level down so both build
their ``database`` component from a single, shared implementation — spec §17).
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from weightsdb.backup import database_size_bytes, integrity_check

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from weightsdb.migrations import MigrationRunner

__all__ = ["DatabaseHealth", "database_health", "is_network_filesystem"]

type ComponentStatus = Literal["ok", "degraded", "unavailable"]

# No configuration reaches this module (spec §12 — WeightsDB reads no config file or environment
# variable), so the two judgement calls below are fixed, documented constants rather than tunable
# parameters. An application that wants a different bar reports raw values itself instead of
# calling this function — see docs/adoption-checklist.md.
_LOW_DISK_FLOOR_BYTES = 100 * 1024 * 1024  # 100 MiB
_STALE_BACKUP_AFTER_SECONDS = 7 * 24 * 3600  # 7 days

_NETWORK_FILESYSTEM_TYPES = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb",
        "smbfs",
        "smb2",
        "9p",
        "afs",
        "afpfs",
        "fuse.sshfs",
        "glusterfs",
        "ceph",
        "davfs",
    }
)


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    """The full ``database`` health snapshot, dialect-portable.

    Attributes:
        dialect: ``"sqlite"`` or ``"postgresql"``.
        version: The backend's own version string, or ``None`` if it could not be read.
        current_revision: The database's current Alembic revision, or ``None`` — either unmigrated,
            or ``runner`` was not given.
        head_revision: The revision this build's migrations produce, or ``None`` when ``runner``
            was not given.
        is_at_head: Whether ``current_revision == head_revision``, or ``None`` when ``runner`` was
            not given.
        journal_mode: SQLite's journal mode (normally ``"wal"``), or ``None`` on PostgreSQL.
        size_bytes: How much disk the database occupies.
        free_space_bytes: Free space on the containing device, or ``None`` if it could not be read.
        last_backup_age_seconds: Age of the newest file under the conventional
            ``<sqlite file>/../backups`` directory, or ``None`` on PostgreSQL (which has no such
            convention) or when no backup has ever been taken.
        integrity_ok: Whether the integrity check passed.
        integrity_detail: The backend's own integrity report.
        network_filesystem: Whether the SQLite file appears to live on a network filesystem
            (spec §16); always ``None`` on PostgreSQL and whenever this cannot be determined
            (non-Linux, ``/proc/mounts`` unreadable) — ``None`` means "undetermined", not "no".
        status: The overall verdict.
        degraded_reasons: Every condition that contributed to a non-``ok`` status, empty when
            ``status == "ok"``.
    """

    dialect: str
    version: str | None
    current_revision: str | None
    head_revision: str | None
    is_at_head: bool | None
    journal_mode: str | None
    size_bytes: int
    free_space_bytes: int | None
    last_backup_age_seconds: float | None
    integrity_ok: bool
    integrity_detail: str
    network_filesystem: bool | None
    status: ComponentStatus
    degraded_reasons: tuple[str, ...]


def _read_proc_mounts() -> list[tuple[str, str]] | None:
    """Return ``(mount_point, fs_type)`` pairs from ``/proc/mounts``, or ``None`` if unreadable."""
    try:
        with Path("/proc/mounts").open(encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    entries: list[tuple[str, str]] = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 3:
            entries.append((parts[1], parts[2]))
    return entries


def is_network_filesystem(
    path: Path, *, mounts: list[tuple[str, str]] | None = None
) -> bool | None:
    """Return whether ``path`` appears to live on a network filesystem (spec §16).

    Best-effort and Linux-only: reads ``/proc/mounts`` (or the ``mounts`` table given, for tests)
    and finds the longest-prefix mount point containing ``path``, then checks its filesystem type
    against a list of known network filesystem types (NFS, CIFS/SMB, 9p, and similar).

    Args:
        path: The file to check. Need not exist yet — only its ancestry is examined.
        mounts: ``(mount_point, fs_type)`` pairs, as read from ``/proc/mounts``. ``None`` reads the
            real table; a test supplies one directly rather than mocking the filesystem.

    Returns:
        ``True``/``False`` when determinable; ``None`` when it cannot be (no ``mounts`` table
        available, or no entry contains ``path``) — undetermined, not "no".
    """
    entries = mounts if mounts is not None else _read_proc_mounts()
    if not entries:
        return None
    resolved = path.absolute()
    best_match: str | None = None
    best_depth = -1
    for mount_point, fs_type in entries:
        mount_path = Path(mount_point)
        if mount_path != resolved and mount_path not in resolved.parents:
            continue
        depth = len(mount_path.parts)
        if depth > best_depth:
            best_depth = depth
            best_match = fs_type
    if best_match is None:
        return None
    return best_match.lower() in _NETWORK_FILESYSTEM_TYPES


def _backend_version(engine: Engine) -> str | None:
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "sqlite":
                return str(connection.execute(text("SELECT sqlite_version()")).scalar_one())
            return str(connection.execute(text("SHOW server_version")).scalar_one())
    except SQLAlchemyError:
        return None


def _journal_mode(engine: Engine) -> str | None:
    if engine.dialect.name != "sqlite":
        return None
    try:
        with engine.connect() as connection:
            return str(connection.execute(text("PRAGMA journal_mode")).scalar_one())
    except SQLAlchemyError:
        return None


def _free_space_bytes(engine: Engine) -> int | None:
    try:
        if engine.dialect.name == "sqlite":
            database = engine.url.database
            if not database or database == ":memory:":
                return None
            target = Path(database).parent
        else:
            # No local path for a remote PostgreSQL server; report on the current process's own
            # filesystem as the best available proxy — an application that needs the *server's*
            # free space monitors that server directly.
            target = Path.cwd()
        while not target.exists():
            parent = target.parent
            if parent == target:
                return None
            target = parent
        return shutil.disk_usage(target).free
    except OSError:
        return None


def _last_backup_age_seconds(engine: Engine, *, now: float) -> float | None:
    if engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    backups_directory = Path(database).parent / "backups"
    if not backups_directory.is_dir():
        return None
    newest: float | None = None
    for candidate in backups_directory.iterdir():
        if not candidate.is_file():
            continue
        mtime = candidate.stat().st_mtime
        if newest is None or mtime > newest:
            newest = mtime
    if newest is None:
        return None
    return max(0.0, now - newest)


def _network_filesystem_for(engine: Engine) -> bool | None:
    if engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return is_network_filesystem(Path(database))


def database_health(engine: Engine, runner: MigrationRunner | None = None) -> DatabaseHealth:
    """Build the full ``database`` health snapshot for ``engine``.

    Args:
        engine: The engine to report on — normally the same one the application is serving from
            (spec §17: this function supplies the ``database`` component of every application's
            health endpoint).
        runner: The application's own :class:`~weightsdb.migrations.MigrationRunner`, for the
            revision-vs-head comparison. ``None`` skips it — ``current_revision``, ``head_revision``
            and ``is_at_head`` are reported as ``None`` rather than raising, since a package that
            owns no schema has no revision history of its own to fall back on.

    Returns:
        The :class:`DatabaseHealth`. Never raises: a database that cannot be reached at all is
        reported as ``status="unavailable"``, not an exception.
    """
    dialect = engine.dialect.name
    try:
        integrity = integrity_check(engine)
    except Exception as exc:  # noqa: BLE001 — a health check must not itself raise
        return DatabaseHealth(
            dialect=dialect,
            version=None,
            current_revision=None,
            head_revision=None,
            is_at_head=None,
            journal_mode=None,
            size_bytes=0,
            free_space_bytes=None,
            last_backup_age_seconds=None,
            integrity_ok=False,
            integrity_detail="unreachable",
            network_filesystem=None,
            status="unavailable",
            degraded_reasons=(f"database unreachable: {exc}",),
        )

    version = _backend_version(engine)
    journal_mode = _journal_mode(engine)
    size_bytes = database_size_bytes(engine)
    free_space = _free_space_bytes(engine)
    backup_age = _last_backup_age_seconds(engine, now=time.time())
    network_fs = _network_filesystem_for(engine)

    current_revision: str | None = None
    head_revision: str | None = None
    is_at_head: bool | None = None
    is_ahead_of_head = False
    if runner is not None:
        current_revision = runner.current()
        heads = runner.heads()
        head_revision = heads[0] if heads else None
        is_at_head = current_revision == head_revision if head_revision is not None else None
        is_ahead_of_head = (
            current_revision is not None
            and not is_at_head
            and current_revision not in runner.known_revisions()
        )

    reasons: list[str] = []
    if is_ahead_of_head:
        reasons.append(
            f"database is ahead of this build: at {current_revision!r}, a revision this build's "
            "migrations do not produce — it was likely written by a newer application version"
        )
    elif is_at_head is False:
        reasons.append(f"pending migration: at {current_revision!r}, head is {head_revision!r}")
    if not integrity.ok:
        reasons.append(f"integrity check failed: {integrity.detail}")
    if free_space is not None and free_space < _LOW_DISK_FLOOR_BYTES:
        reasons.append(f"low disk space: {free_space} bytes free")
    if backup_age is not None and backup_age > _STALE_BACKUP_AFTER_SECONDS:
        reasons.append(f"stale backup: last one is {backup_age / 86400:.1f} days old")
    if network_fs:
        reasons.append("database file appears to be on a network filesystem")

    status: ComponentStatus = "degraded" if reasons else "ok"
    return DatabaseHealth(
        dialect=dialect,
        version=version,
        current_revision=current_revision,
        head_revision=head_revision,
        is_at_head=is_at_head,
        journal_mode=journal_mode,
        size_bytes=size_bytes,
        free_space_bytes=free_space,
        last_backup_age_seconds=backup_age,
        integrity_ok=integrity.ok,
        integrity_detail=integrity.detail,
        network_filesystem=network_fs,
        status=status,
        degraded_reasons=tuple(reasons),
    )
