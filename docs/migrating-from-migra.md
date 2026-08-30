# Migrating from migra

[migra](https://github.com/djrobstep/migra) is deprecated. If you used it
to keep PostgreSQL schemas in sync, this page maps its workflow onto
sqlpush.

## What changes conceptually

migra compared two live databases: you pointed it at A and B and got a
diff. sqlpush compares your SQLAlchemy `MetaData` against one live
database. The models are the source of truth, so there is no second
database to keep current, and the diff can be applied, gated by risk.

## Command map

| migra | sqlpush |
| --- | --- |
| `migra postgresql://A postgresql://B` | `sqlpush diff "app.models:metadata"` |
| pipe the diff into `psql` | `sqlpush push "app.models:metadata"` |
| (eyeball the diff in a script) | `sqlpush check "app.models:metadata"` (exit 0 / 2 / 3) |

## The parts migra never did

- Every planned operation is classified `safe` / `risky` /
  `destructive`; destructive ones are blocked until you pass
  `--allow-destructive`.
- `push` applies atomically, with a bounded `lock_timeout` and an
  advisory lock so concurrent pushes take turns instead of interleaving.
- `check` is designed for CI: exit 2 on drift, 3 on destructive drift.
- `@hypertable` decorates a model and plans state-aware
  `create_hypertable` calls for TimescaleDB.

## Honest differences

- sqlpush wants your schema defined as SQLAlchemy models (SQLModel
  counts). If your source of truth is `.sql` files, two-database
  diffing was the better fit for you; look at
  [atlas](https://github.com/ariga/atlas) or
  [pg-schema-diff](https://github.com/stripe/pg-schema-diff) instead.
- sqlpush is PostgreSQL only (TimescaleDB included), by design.
- There is no two-database mode and none is planned.

If something in your migra workflow has no sqlpush equivalent, open a
discussion. The gap may be a feature.
