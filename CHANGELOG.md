# Changelog

All notable changes to `weightsdb` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

## [0.2.1] — 2026-08-31

### Added
- **SQLite connections now set `PRAGMA secure_delete=ON`** alongside the existing per-connection
  pragmas. `secure_delete` defaults to whatever the host's SQLite build chose, so whether a
  deleted row's content was actually overwritten on disk varied by machine — and a consumer's
  retention scrub is a promise about the disk, not about the schema (the M4 handoff's
  "WeightsDB 0.2.1" item, raised by LoadCoach's retention verification). The pragma is applied
  by the same `connect` listener as the rest, so a pool reconnect keeps it, and the test observes
  it on a live connection rather than trusting the connect string.

## [0.2.0] — 2026-08-29

### Added
- Repository scaffold generated from the suite's development plan (no functional code yet).
- Phase 1: `create_engine_for`, `session_factory`/`session_scope`/`transaction`, `UtcDateTime`,
  `PortableJSON`, `ulid_primary_key`, `measurement_columns`, `upsert`, the `DatabaseError` hierarchy,
  `redact_url`, and `weightsdb.testing.temporary_sqlite`/`temporary_postgres` — extracted from
  FreeWeight's `infrastructure.db` (ADR-0011).
- Phase 2: `MigrationRunner` (upgrade/downgrade/stamp/check_parity with automatic pre-migration
  backup and SQLite restore-on-failure), `backup`/`restore`/`integrity_check` and friends, and
  `weightsdb.testing.migration_harness`.
- Phase 3: `database_health()` (dialect/version, revision vs head, journal mode, size, free space,
  last backup age, integrity, network-filesystem warning, one `status`/`degraded_reasons` verdict),
  `is_network_filesystem()`, the `py.typed` marker, `docs/quickstart.md` and
  `docs/adoption-checklist.md` for FreeWeight Phase 12.

### Fixed
- SQLite lock contention beyond `busy_timeout` now raises the typed `StorageBusy` instead of a raw
  `sqlalchemy.exc.OperationalError` — a gap in the code this package was extracted from, invisible
  with one consumer and no test for it.
- `database_health()` no longer conflates a database *ahead of head* with a *pending migration*:
  any `current != head` read as "pending migration", which is actively misleading when the
  database was written by a newer build. Phase 3's own test list names the two as distinct cases.
- **Coverage fell below the 95 % floor on Python 3.12 (94.89 %).** The uncovered lines were real
  gaps, not measurement noise: `upsert`'s documented refusal of a third dialect, the `StorageBusy`
  translation on a *read-only* `BEGIN`, `temporary_postgres`'s `WEIGHTSDB_REQUIRE_POSTGRES=1`
  failure path, longest-prefix mount selection against an unsorted mount table, and two backups
  sharing an mtime. Each now has a test; the floor is met on 3.12, 3.13 and 3.14 with margin.
- **`psycopg[binary]` is now part of the `dev` extra.** Seventeen tests assert dialect-specific
  behaviour by building a `postgresql://` engine — no server involved — which SQLAlchemy cannot do
  without an importable DBAPI. Every environment that installed `[dev]` alone failed them with
  `ModuleNotFoundError` and landed coverage at 88 % against a 95 % floor; the local `.venv` hid it
  by having been created with `[dev,postgres]`. The optional `postgres` extra is unchanged, and is
  still what a consumer installs to talk to a real server.
- **The `db-matrix` CI job read the wrong environment variable.** It set `DATABASE_URL`, which
  nothing in this package reads; `weightsdb.testing.temporary_postgres` reads
  `WEIGHTSDB_POSTGRES_URL` and otherwise falls back to its own default. With
  `WEIGHTSDB_REQUIRE_POSTGRES=1` the fallback is a hard failure rather than a skip, so the job had
  never exercised PostgreSQL at all. The service container's user, password and database now match
  the documented default URL, and the job sets `WEIGHTSDB_POSTGRES_URL` explicitly rather than
  relying on that default.
- **The PostgreSQL round-trip no longer fails on client/server version skew.** `pg_restore` exits 1
  for warnings unrelated to the data — a client newer than the server emits
  `SET transaction_timeout = 0`, which an older server rejects and reports as "errors ignored on
  restore" — so the test asserted an exit code where its actual claim is that the rows come back.
  It now checks the restored rows and attaches `pg_restore`'s stderr when they are wrong. A missing
  `pg_dump` skips the test the way a missing `pg_restore` already did, instead of failing inside
  `backup()`.
- **`test_restore_reports_a_backup_it_cannot_open` skips when running as root.** `chmod 000` does
  not make a file unreadable to root, so the open succeeds and the corruption branch answers
  instead — a failure that says nothing about the code. CI runs as an ordinary user; a container
  run as root no longer reports it as a defect.
- The `dev` extra moves to `pytest>=9.0.3,<10`, matching BaseAiCore, SetSpec, ModelRack and
  SweatMeter: PYSEC-2026-1845 affects pytest through 9.0.2 and failed the security job.
- **The backup tests' table seed was not dialect-portable.** `INSERT INTO t (name) …` relies on
  SQLite treating `INTEGER PRIMARY KEY` as a rowid alias; PostgreSQL rejects the row with a
  NOT NULL violation. The id is now supplied explicitly, so the round-trip test can actually run
  on the dialect it names.

### Changed
- CI installs from committed, hash-verified lockfiles (`requirements/ci.lock`,
  `requirements/release.lock`) rather than an editable checkout, per Packaging Standards §4;
  `pip-audit` audits those locks instead of an empty environment; `release.yml` gains the
  `pypi` deployment environment, the manual TestPyPI dry run required before a first release, and
  a build chain pinned byte-for-byte to the one the dry run proves. `[tool.coverage.run] source`
  now names the importable package rather than `src/weightsdb`, because a non-editable install
  reports 0 % against a path-based source.
