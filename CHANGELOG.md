# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- `diff` / `check` / `push` no longer report false drift from
  extension-managed state. TimescaleDB's implicit per-hypertable
  time-column index (`<hypertable>_<dimension>_idx`) is pruned from
  DB-only plans, and extension-owned namespaces (e.g. `topology` from
  postgis_topology, which also `ALTER DATABASE`s itself onto the
  search_path at install time) never enter the scope derived from the
  live search_path. The reflection session's search_path is pinned to
  the resolved scope — extension tables previously also resurfaced
  through name visibility and were dropped twice (schema-qualified and
  unqualified). Schemas passed explicitly via `schemas=` are never
  filtered; the default schema (`public`) stays in scope even when
  extensions are relocated into it.

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
