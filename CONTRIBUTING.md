# Contributing to sqlpush

Thanks for considering a contribution. This page covers the practical
parts: getting a dev environment running, what the code expects, and how
a change goes from idea to merged.

## Development setup

```console
git clone https://github.com/juanmicl/sqlpush && cd sqlpush
uv sync
docker compose up -d pg # local test database (TimescaleDB image)
```

Tests read `SQLPUSH_TEST_DSN` and default to
`postgresql+psycopg://sqlpush:sqlpush@localhost:5433/sqlpush_test`.
Tests marked `pg` or `timescale` skip themselves when the database is
unreachable; every other test must pass without one.

```console
uv run pytest -v
```

## Checks to run before pushing

```console
uv run ruff check .
uv run ruff format .
uv run ty check
```

CI runs the same three, plus a test matrix over Python 3.10 to 3.13 and
a compatibility matrix over the two most recent alembic minors.

## Code conventions

- Python 3.10 is the floor. Don't use syntax or stdlib APIs beyond it.
- `ruff format` with line length 100 is the canonical style.
- `uv run ty check` must be clean. `ty` is pinned; version bumps are
  deliberate, not drive-by.
- Commits follow the conventional style: `feat:`, `fix:`, `test:`,
  `chore:`, `docs:`.
- Every alembic import lives in `src/sqlpush/core/diff.py` and stays
  there. The rest of the package does not import alembic, typer, or
  psycopg directly.
- `src/sqlpush/annotations.py` and the type surface stay import-light:
  no heavy dependencies leak into them.

## Where things live

- `src/sqlpush/core/`: diff engine, risk classification, rendering
- `src/sqlpush/apply/`: executor (atomic transaction, CONCURRENTLY
  split, advisory lock)
- `src/sqlpush/directives/`: TimescaleDB hypertable directives
- `src/sqlpush/api.py`: public functions (`plan`, `push`, `check`,
  `ensure_schema`, plus the async variants)
- `src/sqlpush/cli.py`: the CLI

## Bugs and ideas

Open an issue with the template filled in. For open questions and
half-formed ideas, start a discussion instead; issues are for work
someone has agreed to do.

## Security

Don't open public issues for security problems. See [SECURITY.md](SECURITY.md).
