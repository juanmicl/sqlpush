# Migrating from alembic

There is no automated conversion of alembic revisions, and none is
planned. Your alembic chain stays where it is. sqlpush starts a new
chain from today, and the two can coexist for as long as you want.

## Command map

| alembic | sqlpush |
| --- | --- |
| `alembic revision --autogenerate -m "..."` | `sqlpush revision "app.models:metadata" --ref-dsn ... -m "..."` |
| `alembic upgrade head` | `sqlpush migrate` |
| `alembic stamp head` | `sqlpush stamp` |
| `alembic current` / `alembic heads` | `SELECT * FROM public.sqlpush_versions` (plus the file numbering) |
| `alembic downgrade -1` | none. Forward only. |

## Phase 1: observe parity first

Before changing anything, point `check` at your alembic-managed
database:

```console
$ export DATABASE_URL="postgresql://user:pass@host:5432/db"
$ sqlpush check "myapp.models:metadata"
$ echo $?
0
```

`alembic_version` is pruned from every diff automatically, the same
treatment sqlpush gives its own registry table, so it never shows up
as drift.

Exit `0` means your models match what alembic actually built. That is
the usual case and the green light to continue. Exit `2` or `3` means
there is real drift alembic never knew about: manual hotfixes, objects
created outside migrations, tables someone dropped by hand. Exit `3`
adds destructive drift, which means sqlpush wants to drop something to
make the database match the models.

Legacy objects you deliberately keep are the normal cause. Accept them
with `--exclude`, which takes fnmatch patterns against table names and
can be repeated:

```console
$ sqlpush check "myapp.models:metadata" --exclude "legacy_*" --exclude audit_2020
```

Run `check` this way in CI for a week or a month. When it is green,
your models describe the real database.

## Phase 2: baseline the chain

With database and models in agreement, the new chain starts empty.
There is nothing to convert: the first `sqlpush revision` comes from
your next model change, generated against a reference DB sitting at
the chain head (see [the chain guide](the-chain.md) for the file
format and gates).

Two things may belong in the chain before the first generated file:

- Extensions. If your models use PostGIS or TimescaleDB, hand-author
  `0000_extensions.sql` so fresh environments install them first. The
  pattern is documented in [the chain guide](the-chain.md).
- Seed state. Same idea: a hand-authored `0000` file, with the SQL you
  want every environment to run.

If the database already has the extensions or seed data, `stamp`
registers those files without executing them.

## Phase 3: run both, then switch

For a while, keep `alembic upgrade` as the applier and add
`sqlpush check` to CI next to it. The check gate is exit `0`. Both
tools can look at the same database: alembic never sees sqlpush's
registry, and sqlpush prunes `alembic_version` from every diff
forever.

When you trust the parity signal, switch over. New model changes go
through `sqlpush revision` and `sqlpush migrate`. Stop running alembic
whenever it is convenient; its version table can stay where it is,
untouched, because it is pruned from every future diff. The old chain
becomes an archive.

## Phase 4: prove the chain on a scratch DB

Before you retire alembic, prove that a fresh replay of the files
rebuilds the schema. Take a schema-only dump of the baseline (before
any chain file ran) and restore it into a scratch database, then run
the chain head-to-toe:

```console
$ pg_dump --schema-only "$PROD_DSN" | psql "$SCRATCH_DSN"
$ sqlpush migrate --dsn "$SCRATCH_DSN"
applied: 4, skipped: 0, blocked: 0, partial_failure: False
$ sqlpush check "myapp.models:metadata" --dsn "$SCRATCH_DSN"
$ echo $?
0
```

Exit `0` is the proof: the files alone rebuilt everything the models
describe. If the dump already contains a `public.sqlpush_versions`
table (because it was taken after the chain started running), drop
that table on the scratch copy first, or every file will skip as
already applied. `stamp` is for the opposite situation: adopting a
database whose chain already ran.

## Honest differences

- Forward only. There is no `downgrade`. To revert a change, revert
  the model change and generate a new file. The models are the source
  of truth, and a downgrade chain is a second history to maintain.
- No offline mode. alembic can render SQL without touching a database
  (`alembic upgrade --sql`). Every sqlpush verb diffs against a live
  database. The review surface is the files themselves, which are
  plain SQL you can read before running.
- No branches. alembic has `branch_labels` and merge revisions. The
  chain is strictly linear: lexicographic filename order is the whole
  contract, and `revision` numbers files `max + 1`.

If something in your alembic workflow has no sqlpush equivalent, open
a discussion. The gap may be a feature.
