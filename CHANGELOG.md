# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-09-01

### Added

- Implicit spatial index pruning, catalog-driven: single-column
  GiST indexes named exactly `<table>_<column>_idx` on a `geometry` /
  `geography` column — what geoalchemy2 (<0.18 or `spatial_index=True`)
  creates at table-create time and no model declares — are pruned from
  DB-only plans as extension/library state, same as the TimescaleDB
  auto-indexes. Detection queries the catalogs (`pg_index` ⋈ `pg_class`
  ⋈ `pg_attribute` ⋈ `pg_type`), so it keys on the column TYPE, never
  on the name alone: the same index name on a non-spatial column stays
  real drift, and indexes declared in the metadata are never affected.
  It also works regardless of geoalchemy2 being importable in the
  sqlpush process. The test image switched to
  `timescale/timescaledb-ha:pg17` (the PostGIS-carrying HA image) so
  the behavior is exercised in CI.

### Fixed

- `diff` / `check` / `push` no longer report false drift from
  extension-managed state. TimescaleDB's implicit per-hypertable
  time-column index (`<hypertable>_<dimension>_idx`) is pruned from
  DB-only plans, and extension-owned namespaces (e.g. `topology` from
  postgis_topology, which also `ALTER DATABASE`s itself onto the
  search_path at install time) never enter the scope derived from the
  live search_path. The reflection session's search_path is pinned to
  the default schema alone — never to the full scope list, and only
  when the default schema is a scope member: the default-schema
  (unqualified) reflection pass resolves table names via session
  visibility, so a full-scope pin made non-default tables masquerade
  as default-schema tables in mixed scopes (`public` plus another
  schema), producing false destructive `DROP TABLE`s and duplicate
  unqualified drops. The default-schema-only pin also keeps extension
  tables from resurfacing through name visibility and being dropped
  twice (schema-qualified and unqualified). Schemas passed explicitly
  via `schemas=` are never
  filtered; the default schema (`public`) stays in scope even when
  extensions are relocated into it.
- The auto-index prune now keys the map by qualified
  `{schema}.{index}` with a **set** of qualified hypertable owners, so
  the same heuristic index name produced from distinct hypertables
  (table `a` dimension `b_c` and table `a_b` dimension `c` both compute
  to `a_b_c_idx`) no longer leaks one of them as false drift: with a
  str value the last row won and the other owner's index was planned
  (as destructive `DROP INDEX`). Heuristic note: a user-created DB-only
  index whose name is exactly `<table>_<dimension>_idx` on a known
  hypertable is pruned as extension state; indexes declared in the
  metadata are never affected. Additionally, a failure while restoring
  the reflection session's search_path no longer masks the root error
  from the diff itself, and the affected pooled connection is
  discarded (invalidated) instead of being recycled with the
  restricted path. A TimescaleDB-suffixed auto index
  (`<table>_<dim>_idx1` from a same-schema name near-collision) still
  surfaces as false drift — known limitation.

## [0.1.0] - 2026-08-31

First public release.

### Added

- `diff`, `check`, and `push` CLI verbs over SQLAlchemy `MetaData`
  (`module:attribute`), with `--schema` / `--exclude` scoping and
  `--json` output (versioned contract, `"version": 1`).
- Risk classification of every planned operation (`safe` / `risky` /
  `destructive`); destructive operations are blocked until
  `--allow-destructive`, and `push --safe-only` runs only the safe ones.
- Exit codes built for CI: `check` exits 0 / 2 / 3 (clean / drift /
  destructive drift); `push` exits 1 while destructive operations are
  blocked and 2 on partial failure.
- Atomic apply: one transaction with a bounded `lock_timeout`;
  `CREATE INDEX CONCURRENTLY` statements split onto autocommit, one per
  transaction.
- Advisory lock keyed to the database (not the DSN), so racing deploy
  jobs take turns; losers wait bounded and re-verify.
- `@hypertable` decorator and Timescale directive for state-aware
  `create_hypertable` planning on TimescaleDB.
- Python API: `plan`, `push`, `check`, `ensure_schema` plus the async
  variants (`aplan`, `apush`, `acheck`, `aensure_schema`), with typed
  errors only (`SqlpushError` subclasses) escaping the public surface.

[0.1.0]: https://github.com/juanmicl/sqlpush/releases/tag/v0.1.0
