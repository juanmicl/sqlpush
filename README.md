# sqlpush

[![PyPI](https://img.shields.io/pypi/v/sqlpush?style=for-the-badge)](https://pypi.org/project/sqlpush/)
[![Python](https://img.shields.io/pypi/pyversions/sqlpush?style=for-the-badge)](https://pypi.org/project/sqlpush/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](LICENSE)

**Prisma `db push` for SQLAlchemy.** Apply your models (SQLAlchemy,
SQLModel, anything built on `MetaData`) to a live PostgreSQL / TimescaleDB
database directly, no migration files required. sqlpush
diffs your models against the real schema, classifies every operation by
risk (safe / risky / destructive), and applies the plan atomically. Drift
checks exit with codes your CI can gate on.

```console
sqlpush diff "myapp.models:metadata"      # see the SQL, ordered by risk
sqlpush check "myapp.models:metadata"     # CI gate: exit 0/2/3
sqlpush push "myapp.models:metadata"      # apply (destructive gated)
```

If you've ever run `Base.metadata.create_all()` in production and known it
was wrong, then sighed at the migration-script treadmill when you reached
for alembic: sqlpush is for you.

## Why

Declarative models are already the source of truth. Migration files
re-encode what the models say, drift from them, and pile up forever.
sqlpush closes the loop the way Prisma's `db push` does for its schema
language, but for the SQLAlchemy ecosystem (SQLModel included):

- **No migration files required.** The diff *is* the migration: computed fresh
  from models vs. live database on every run, via alembic's autogenerate
  engine used as a library. Files exist as a second workflow when you want
  them (see below).
- **Risk-aware by default.** Every operation is classified `safe` /
  `risky` / `destructive`. Destructive ops (drops) are **blocked until
  `--allow-destructive`**: nothing executes at all while any is present.
- **Drift detection built for CI.** `check` plans once and exits `0` clean /
  `2` drift / `3` destructive drift, scriptable without parsing output.
  `--json` emits a stable versioned contract.
- **Safe under concurrency.** An advisory lock (keyed to the database, not
  the DSN) coordinates workers: one pusher at a time, losers wait bounded
  and re-verify, so deploy pipelines can race without corrupting anything.
- **Hypertables without hand-written SQL.** Decorate a model with
  `@hypertable` and the `create_hypertable` directive is planned
  state-aware: idempotent pushes, clean checks, no false drift.

PostgreSQL only, by design.

## When you want files: the chain

The push workflow has no files because most of the time you don't need
them. When you do, sqlpush has a second workflow built on the same
diff engine: the chain. `revision` writes the next numbered SQL file
from your models against a reference DB, `migrate` replays pending
files with gates and checksums, and `stamp` adopts an existing
database without executing anything.

The files are plain SQL you can review, edit before first apply, and
run under `psql`. Schema change and data backfill ship as one file.
The [chain guide](docs/the-chain.md) covers the format, the gates and
the workflows.

## Install

```console
pip install sqlpush
```

Or from source:

```console
git clone https://github.com/juanmicl/sqlpush && cd sqlpush && uv sync
```

## The 30-second tour

Point sqlpush at your metadata (`module:attribute`) and a database
(`--dsn` or `$DATABASE_URL`):

```console
$ export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db"

$ sqlpush diff "myapp.models:metadata"
-- safe

CREATE TABLE hero (
    id SERIAL NOT NULL PRIMARY KEY,
    name VARCHAR(50) NOT NULL
);

-- risky

CREATE INDEX ix_hero_name ON hero (name);
```

Push it (the destructive gate is on by default):

```console
$ sqlpush push "myapp.models:metadata"
1 destructive operation(s) blocked; re-run with --allow-destructive
$ echo $?
1

$ sqlpush push "myapp.models:metadata" --allow-destructive
$ echo $?
0
```

In CI, check drift and fail loudly (see exit codes below). Limit scope with
repeated `--schema` / `--exclude` options.

## An inherited database

The first `check` against a database with history often reports drift:
hand-built indexes, audit tables, that column someone added at 2am. If
any of the drift looks destructive, `check` exits `3` and `push`
blocks. That is the tool refusing to silently drop your legacy
objects. Two escape hatches: `--exclude` accepts objects you choose to
keep (fnmatch patterns, repeatable), and `--allow-destructive` accepts
the drops when you really do want them.

## Exit codes

| verb | 0 | 1 | 2 | 3 |
| --- | --- | --- | --- | --- |
| `diff` | always | | | |
| `check` | clean | | drift | destructive drift |
| `push` | applied | destructive blocked | error (incl. partial failure) | |
| `revision` | file written | error (empty drift refuses) | | |
| `migrate` | clean | blocked or partial failure | | |
| `stamp` | registered | blocked or refused | | |

Failures print a typed error on stderr, never a traceback.

`push --safe-only` runs only safe operations and skips the rest
informationally (exit `0`). Indexes on existing tables build
`CONCURRENTLY` by default (opt out with `--no-concurrently`); a failed
`CREATE INDEX CONCURRENTLY` marks the run as partial failure (exit `2`)
instead of silently half-applying, and leaves an INVALID index: drop it
(`DROP INDEX CONCURRENTLY`) and re-push. `stamp` refuses a file whose
checksum no longer matches the registry; `--force` accepts the new
content.

The knobs, per verb:

| verb | flags |
| --- | --- |
| `push` | `--allow-destructive` `--safe-only` `--no-lock` `--lock-timeout` `--advisory-wait` `--no-concurrently` `--statement-timeout` |
| `revision` | `--ref-dsn` (required) `-m/--message` `--no-concurrently` `--dir` |
| `migrate` | `--allow-destructive` `--advisory-wait` `--lock-timeout` `--statement-timeout` `--dir` |
| `stamp` | `--force` `--dir` |

Every verb except `revision` takes `--dsn` (or `$DATABASE_URL`).
`revision` requires `--ref-dsn`, with no env fallback: the reference
DB is a different database from the push target. `diff`, `check`,
`push` and `revision` also take repeatable `--schema` / `--exclude`.
Timeouts are seconds; a `lock_timeout` bounds how long a statement
waits on a lock before failing, `statement_timeout` bounds each
statement's runtime, and an exhausted `advisory-wait` raises instead
of hanging on a stuck lock holder.

## FastAPI / SQLModel: replace `create_all`

```python
from contextlib import asynccontextmanager
from sqlpush import aensure_schema


@asynccontextmanager
async def lifespan(app):
    await aensure_schema(SQLModel.metadata, engine, mode="check")
    yield
```

Push in the deploy pipeline, check at startup. asyncpg URLs work too:
a DSN or `AsyncEngine` spelling `postgresql+asyncpg` is translated to
the psycopg driver automatically, and asyncpg is never required in the
sqlpush process.

## How it works

```mermaid
flowchart LR
    models["SQLAlchemy MetaData"] --> diff["diff<br>alembic autogenerate, scoped"]
    db[("live PostgreSQL")] --> diff
    diff --> risk["risk classification<br>safe / risky / destructive"]
    risk --> plan["plan"]
    plan --> render["render"]
    render --> apply["apply<br>atomic txn · CONCURRENTLY split · advisory lock"]
    apply --> report["report"]
```

- **Diff engine** scopes reflection to your target schemas (default: the
  session's real `search_path`) and prunes system catalogs (TimescaleDB
  internals included) before reflection even starts.
- **Classifier** maps each operation to a risk class; unknown operations
  are `risky`, never silently safe.
- **Executor** splits the plan: existing-table indexes render
  `CREATE INDEX CONCURRENTLY` and run one-per-transaction on autocommit
  (`--no-concurrently` opts out; indexes on tables the same plan creates
  stay in the atomic transaction), everything else applies in a single
  atomic transaction with a bounded `lock_timeout`.
- **Typed errors**: only `SqlpushError` / `ConnectFailed` /
  `MetadataImportError` escape the API, never raw driver exceptions.

Scoping: `--schema` restricts the diff to named schemas (default: the
session's real `search_path`). Extension-owned schemas never enter
scope automatically, and schemas you pass explicitly are never
filtered. The chain's registry table (`public.sqlpush_versions`)
always lives in `public` and is pruned from every diff, so `check`
after `migrate` is clean. `alembic_version` gets the same treatment.

## Comparison

An honest view of the neighborhood (stars as of 2026-08):

| | migration files | source of truth | risk gate | CI drift exit codes | TimescaleDB |
| --- | --- | --- | --- | --- | --- |
| **sqlpush** | optional: push needs none; the chain has reviewable, checksummed files | SQLAlchemy `MetaData` | classified safe/risky/destructive, destructive blocked by default | `check` 0/2/3 | `@hypertable` directives |
| [alembic](https://github.com/sqlalchemy/alembic) (4.4k★) | yes | migration scripts (autogenerate assists) | no | no | no |
| [atlas](https://github.com/ariga/atlas) (8.7k★) | optional (HCL) | HCL / SQL (ORMs via providers) | lint policies | yes | no |
| [prisma `db push`](https://www.prisma.io/docs/orm/reference/prisma-cli-reference) (47k★) | none | Prisma schema (Node/TS) | no | no | no |
| [migra](https://github.com/djrobstep/migra) (3.1k★) | diff only | SQL | n/a | partial | no (*deprecated*) |

sqlpush is narrower than atlas and younger than alembic, deliberately.
It is one tool for one job: keep a PostgreSQL schema in lockstep with
SQLAlchemy models, safely enough to run from CI.

Guides: [the chain](docs/the-chain.md) (file format, gates, backfills),
[migrating from alembic](docs/migrating-from-alembic.md), and
[migrating from migra](docs/migrating-from-migra.md) (deprecated).

## Design notes

- `import sqlpush` stays light: the public API loads lazily, so the
  annotations module carries none of alembic/typer/psycopg.
- The advisory-lock key derives from the database OID: two DSN spellings
  of the same database contend for the same lock.
- `--json` output is a versioned contract (`"version": 1`) meant for
  tooling; additive changes only within a version (operations now carry
  a `concurrent` boolean).

## Roadmap

- jsonschema-validated `--json` output

## License

[MIT](LICENSE) · © 2026 Juan Miguel Contreras
