# WeightsDB

Shared SQLAlchemy + Alembic plumbing: engines, sessions, pragmas, migrations, backup and health. No application table, no shared schema.

**Status:** specified, not yet implemented. This repository currently holds the project scaffold
(directory structure, tooling configuration, and the project documentation) —
see [development plan](docs/packages/weightsdb/development-plan.md) for what each phase adds.

Part of the **Local AI Suite**.

## Install

```bash
pip install weightsdb
```

## Quickstart

```python
import weightsdb
```

See [docs/packages/weightsdb/spec.md](docs/packages/weightsdb/spec.md) §20 for a runnable example.

## Documentation

Project documentation lives under [`docs/`](docs/README.md). Start with [`docs/README.md`](docs/README.md).

| Read this | For |
|---|---|
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
