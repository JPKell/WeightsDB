# Changelog

All notable changes to `weightsdb` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

### Added
- Repository scaffold generated from the suite's development plan (no functional code yet).
- Phase 1: `create_engine_for`, `session_factory`/`session_scope`/`transaction`, `UtcDateTime`,
  `PortableJSON`, `ulid_primary_key`, `measurement_columns`, `upsert`, the `DatabaseError` hierarchy,
  `redact_url`, and `weightsdb.testing.temporary_sqlite`/`temporary_postgres` — extracted from
  FreeWeight's `infrastructure.db` (ADR-0011).
- Phase 2: `MigrationRunner` (upgrade/downgrade/stamp/check_parity with automatic pre-migration
  backup and SQLite restore-on-failure), `backup`/`restore`/`integrity_check` and friends, and
  `weightsdb.testing.migration_harness`.
