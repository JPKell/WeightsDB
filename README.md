# WeightsDB

Shared SQLAlchemy + Alembic plumbing: engines, sessions, pragmas, migrations, backup and health. No application table, no shared schema.

**Status:** Phases 1–3 complete at `0.2.1` — engine, sessions, types, migrations, backup/restore and health
are implemented and tested on SQLite (PostgreSQL exercised wherever a server is configured; see
[docs/packages/weightsdb/development-plan.md](docs/packages/weightsdb/development-plan.md)).
Extracted from FreeWeight's `infrastructure.db` per ADR-0011; FreeWeight itself adopts this package
in its own Phase 12 — see [docs/adoption-checklist.md](docs/adoption-checklist.md).

Part of the **Local AI Suite**.

## Install

```bash
pip install weightsdb
# or, with PostgreSQL support:
pip install "weightsdb[postgres]"
```

## Quickstart

```python
from weightsdb import create_engine_for, session_factory, session_scope

engine = create_engine_for("sqlite:///./myapp.sqlite3")
factory = session_factory(engine)
with session_scope(factory) as session:
    ...
```

See [docs/quickstart.md](docs/quickstart.md) for the full walkthrough (engine, schema, sessions,
upserts, migrations, backup/restore, health, and testing your own application against it), and
[docs/packages/weightsdb/spec.md](docs/packages/weightsdb/spec.md) §20 for the formal acceptance
criteria.

## Documentation

Project documentation lives under [`docs/`](docs/README.md). Start with [`docs/README.md`](docs/README.md).

| Read this | For |
|---|---|
| [docs/quickstart.md](docs/quickstart.md) | Get started: engine, schema, sessions, migrations, backup, health |
| [docs/adoption-checklist.md](docs/adoption-checklist.md) | FreeWeight Phase 12: what to delete, what changed shape, what must still pass |
| [docs/packages/weightsdb/spec.md](docs/packages/weightsdb/spec.md) | Purpose, scope, non-goals, public contracts, configuration, acceptance criteria |
| [docs/packages/weightsdb/development-plan.md](docs/packages/weightsdb/development-plan.md) | The phased build plan: goals, work, tests, acceptance criteria per phase |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
pytest -m "not live and not performance"
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow and [`SECURITY.md`](SECURITY.md) for
how to report a vulnerability.

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
