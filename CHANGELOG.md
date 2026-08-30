# Changelog

All notable changes to `weightsdb` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

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
  never exercised PostgreSQL at all.
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
