# The chain: migration files for sqlpush

sqlpush has two ways to apply a model change. The push workflow
(`diff` / `push` / `check`) computes a plan from your models against
the live database and applies it directly, no files involved. The
chain is the other workflow: the same diff engine and renderer, but
the plan is written to a numbered SQL file you can review, edit and
replay. Files add three things the push workflow does not have:
ordering, checksums and per-file gates.

## The three verbs

| verb | what it does |
| --- | --- |
| `revision` | write the next file, from models vs. a reference DB |
| `migrate` | replay pending files, with gates and bookkeeping |
| `stamp` | adopt an existing DB: register files without executing them |

`revision` diffs your models against a reference database you supply
with `--ref-dsn`. The flag is required, and there is no `DATABASE_URL`
fallback on purpose: the reference DB sits at the chain head, which is
a different database from the push target, and conflating them would
generate the next file against the wrong baseline. Empty drift refuses
("nothing to revise"). An existing file is never overwritten.

```console
$ sqlpush revision "myapp.models:metadata" \
    --ref-dsn postgresql://user:pass@host:5432/db \
    --message add_users
migrations/versions/0001_add_users.sql
```

`migrate` applies every file not yet recorded, in order:

```console
$ sqlpush migrate
applied: 1, skipped: 0, blocked: 0, partial_failure: False
```

`stamp` registers every parseable file as applied without running a
single statement. Use it to adopt a database whose schema already
reflects the chain. Only the header must parse; the SQL bodies are
never executed, so they are not checked.

## The file format

A generated file looks like this:

```sql
-- sqlpush: revision=0001 risk=SAFE
-- parent=
-- create_users

-- op 1 [SAFE] add_table users
CREATE TABLE users (
    id SERIAL NOT NULL,
    email VARCHAR(255) NOT NULL,
    PRIMARY KEY (id)
);
```

The first line is the only structured requirement:
`-- sqlpush: revision=NNNN risk=SAFE|RISKY|DESTRUCTIVE`. The risk
value is the file's gate; a `DESTRUCTIVE` file needs
`--allow-destructive` on `migrate`. The other comment lines (`parent`,
the message) are informative. You can edit or delete them. Each op
starts with a `-- op N [label] description` line followed by its SQL.

The structure rides on legal SQL comments, so a chain file runs
directly under `psql`. There is no sqlpush-specific syntax anywhere in
the body.

The checksum is sha256 over the whole file, newline-normalized. Files
run in lexicographic filename order; the `NNNN_` prefix keeps that
order meaningful, and `revision` numbers the next file max+1. There is
no merge graph, just one linear chain. A missing header, an unknown
risk or a non-numeric revision is a hard parse error: the file is
refused, never guessed at.

## Gates on migrate

- The destructive gate reads the header. A file marked
  `risk=DESTRUCTIVE` is blocked until you pass `--allow-destructive`.
- The checksum gate catches edits. A file that changed after it was
  applied is refused, with an error naming the file. `stamp --force`
  is the only override.
- Strict order. A blocked file stops the chain; nothing after it runs.
- One lock for everyone. `migrate` takes the same advisory lock as
  `push`, keyed to the database, so chain workers and pushers take
  turns instead of interleaving. The wait is bounded
  (`--advisory-wait`, default 30s); a stuck holder raises a typed
  error rather than hanging.
- Budgets per file. `--lock-timeout` (default 5s) bounds lock waits;
  `--statement-timeout` bounds each statement's runtime if you set
  one.

## CONCURRENTLY inside files

`revision` renders `CREATE INDEX CONCURRENTLY` for indexes on existing
tables by default, like `push` does (`--no-concurrently` opts out).
Replay handles those files specially:

- A file whose text contains no `CONCURRENTLY` replays whole: the
  entire file goes to the server as one call inside one transaction.
  Nothing is tokenized, so dollar-quoted function bodies are safe.
- A file that contains `CONCURRENTLY` replays per-op on the op labels.
  Plain ops run first in one transaction, then concurrent ops run one
  statement at a time on a dedicated autocommit connection. The
  registry row is written only after every op succeeds.

If a concurrent op fails, the file is blocked: partial failure, no
registry row, and the chain stops. The plain segment that already
committed is reported in the notes, not hidden.

## Backfilling data with a revision

This is the workflow files exist for. Your table has rows, and the
model gains a column that needs data before it can be tightened. A
schema-only change cannot carry the data; with the chain, schema
change and data change ship as one reviewable unit.

Generate the revision:

```console
$ sqlpush revision "myapp.models:metadata" \
    --ref-dsn postgresql://user:pass@host:5432/db \
    --message add_slug
migrations/versions/0003_add_slug.sql
```

Then open the file and append a backfill op by hand, before the first
`migrate`:

```sql
-- op 2 [raw_sql] backfill slugs
UPDATE my_table SET slug = 'u' || id WHERE slug IS NULL;
```

The file has never been applied, so its checksum is not recorded yet
and the edit is free. (Editing after apply is refused; see
limitations.) Run `migrate`: the `ALTER` and the `UPDATE` apply in one
transaction. Verify the data, tighten the column in a follow-up
revision if needed, and `check` confirms models and database agree.

One rule when editing by hand: if the SQL you add is destructive, bump
the file's `risk=` header to match. The header is the gate.

## Installing extensions first

Extensions are cluster state, not model state, and never appear in a
plan. If your models use PostGIS `geometry` or TimescaleDB
hypertables, a push against a fresh database fails with
`type "geometry" does not exist` before any of your tables build.

Hand-author the chain's first file before generating any revision.
The name `0000_extensions.sql` makes it sort first:

```sql
-- sqlpush: revision=0000 risk=SAFE

-- op 1 [raw_sql] install extensions the models depend on
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

Fresh environments run `sqlpush migrate` and get the extensions
installed before any generated file, because `0000` sorts before
everything. A database that already has the extensions is adopted
with `stamp`, which registers the file without executing it. Re-runs
are safe: `IF NOT EXISTS` keeps the statements idempotent even if the
registry row is missing. The next `revision` after a hand-authored
`0000` numbers itself `0001`.

## The registry

Bookkeeping lives in `public.sqlpush_versions` (`name`, `sha256`,
`applied_at`). The table always lives in `public`, whatever schemas
your models target: it is chain state, not schema state. It is pruned
from every diff, same as `alembic_version`, so a `check` after
`migrate` comes back clean.

## Honest limitations

- Editing a file after it was applied is refused (checksum gate).
  Editing before the first apply is free; that is the backfill
  workflow above.
- A body with no op labels replays as one unit. If such a body
  contains `CONCURRENTLY`, it executes statement-by-statement on
  autocommit, with no atomicity across statements.
- Per-op parsing treats lines starting with `--` as comments. Keep
  them out of a labeled op's SQL unless they really are comments.
- A failed or timed-out `CREATE INDEX CONCURRENTLY` leaves an `INVALID`
  index. Recover with `DROP INDEX CONCURRENTLY <name>`, then re-run.
- Mixed files have a crash window: plain segment committed, concurrent
  op applied, no registry row yet. A re-run fails loud on the existing
  objects instead of silently re-applying.
- Generated concurrent index creates carry no `IF NOT EXISTS`. A re-run
  against an existing index fails loud, by design.
