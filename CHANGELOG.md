# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `CREATE INDEX` / `CREATE UNIQUE INDEX` on EXISTING tables now render
  `CONCURRENTLY` by default in `plan` / `push` / `revision` output,
  including `revision`-generated migration SQL (API: `concurrently=True`
  on `plan` / `push` / `revision`; CLI opt-out `--no-concurrently` on
  `push` / `revision`): the plain form takes a SHARE lock that blocks
  all writes for the whole build, while CONCURRENTLY builds without
  blocking writers. Indexes on tables created in the same plan stay
  plain — a brand-new table has no concurrent writers, and the create
  stays inside the plan's atomic transaction. Concurrently-rendered ops
  run on push's autocommit segment (a failure there is a recorded
  partial failure, not a rollback) and are still classified `risky`.
  The knob threads through the advisory-lock winner path: the lock
  winner's re-plan renders exactly what the caller requested.
- `push` can bound each statement's runtime with a `statement_timeout`
  (API: `push(..., statement_timeout=...)`, seconds, `None` = set
  nothing; CLI: `push --statement-timeout`): the transactional segment
  applies it as `SET LOCAL statement_timeout`, the concurrent
  (autocommit) segment as a session `statement_timeout` that is `RESET`
  before the connection returns to the pool — a pooled borrower never
  inherits the budget. It threads through the advisory-lock winner path
  with `concurrently`, and negative values are rejected up front with
  a typed `SqlpushError`.
- The concurrent (autocommit) segment of `push` now runs under a
  session `lock_timeout` with the same value as the transactional
  segment's: previously `CREATE INDEX CONCURRENTLY` had no lock budget
  at all and could queue indefinitely behind another transaction's
  table lock. The session GUC is `RESET` (and the connection
  invalidated if the reset fails) so it never leaks to the pool's next
  borrower.
- Every operation in the versioned plan JSON (`diff --json`,
  `check --json`, `Plan.to_json_dict`) now carries a `"concurrent"`
  boolean — additive to the v1 contract, no existing key or value
  changed. It reports whether the operation's SQL was rendered with
  `CREATE INDEX CONCURRENTLY` (see the rendering change above).
- `migrate` now replays CONCURRENTLY-containing chain files per-op on
  the op-label delimiters: the plain segment runs first in one
  transaction (chain files can create→index within one file), then
  concurrent ops run statement-by-statement on a dedicated autocommit
  connection (session `lock_timeout` matching the per-file txn, RESET +
  close when the walk ends), and the versions row is written only after
  every concurrent op succeeds — a failed concurrent op blocks the file
  (partial failure, no versions row, strict-order stop, the already
  committed plain segment reported honestly in the notes). Concurrent-
  free files keep the exact 0.4.2 whole-text single-transaction replay,
  so existing chains are byte-identically unaffected, and
  `revision`-generated files round-trip through the chain.
- `migrate` gains `--statement-timeout` (API:
  `migrate(..., statement_timeout=...)`, seconds, unset by default):
  applied as `SET LOCAL` in every per-file transaction and as a session
  `statement_timeout` on the CONCURRENTLY autocommit connection (RESET
  before it closes). Negative values are rejected up front with a
  typed `SqlpushError` — same contract as `push`.

### Known limitations

- Generated chain files replay per-op on their `-- op N [label]`
  delimiters; hand-edits bypass that tokenization (pinned chain spec
  §7). A label-less body containing CONCURRENTLY routes whole to the
  autocommit lane statement-by-statement, and lines starting `--` are
  stripped from per-op parsing — dollar-quoted bodies containing `--`
  lines are only safe in the whole-text fast path.
- A failed or timed-out `CREATE INDEX CONCURRENTLY` leaves an INVALID
  index behind: recover with `DROP INDEX CONCURRENTLY <name>` and
  re-push (optionally `--no-concurrently`). `statement_timeout` applies
  inside index builds, and an aborted build still leaves the INVALID
  index.
- Mixed-file crash window: plain segment committed + concurrent segment
  applied + no versions row → a re-run fails loud on the existing
  objects rather than silently re-applying.
- Re-running against an already-existing index fails loud: the rendered
  `CREATE INDEX CONCURRENTLY` carries no `IF NOT EXISTS`.

## [0.4.2] - 2026-09-02

### Fixed

- `migrate` no longer blocks forever on the advisory lock: the chain
  session's wait is now bounded — `pg_try_advisory_lock` polled every
  0.5s against a monotonic deadline (default 30s), mirroring `push` —
  and `migrate` exposes `--advisory-wait` (API: `advisory_wait=`) to
  tune or zero it. An exhausted budget raises a typed
  `SqlpushError` instead of hanging on a stuck holder. `stamp` shares
  the chain session and gets the same bounded default.
- `stamp` no longer silently refreshes the checksum of a file that was
  edited after it was applied/stamped: the recorded checksum is read
  first, and a registered-but-different checksum now refuses with a
  typed error (first mismatch stops the walk — nothing after it
  registers) instead of overwriting the registry. `--force` (API:
  `force=True`) accepts the new content, so the chain's edit-detection
  integrity survives re-stamping.
- `migrate` now sets a per-file transaction-scoped `lock_timeout`
  (default 5s), mirroring `push`: a chain file whose DDL is blocked
  behind another transaction's lock fails fast with a typed error
  instead of queuing indefinitely. Tunable via `--lock-timeout`
  (API: `lock_timeout=`; negative values are rejected up front).
- The sync facade (`ensure_schema` / `migrate` / `stamp` — everything
  resolved from a DSN or `AsyncEngine`) now accepts `postgresql+asyncpg`
  URLs by translating them to the `postgresql+psycopg` driver
  (host, database, credentials and query options preserved) instead of
  failing on the async-only driver at connect time. asyncpg is never
  required to be installed in the sqlpush process; plain psycopg
  targets are untouched.

## [0.4.1] - 2026-09-02

### Added

- `@hypertable` is now dual-mechanism: besides recording the annotation on
  `Table.info` (planned as a `create_hypertable` op on push/revision), the
  decorator registers an `after_create` listener on the table so
  `MetaData.create_all` paths register the hypertable too. The listener runs
  the same idempotent SQL the directive plans (`if_not_exists => true,
  create_default_indexes => false`), so coexistence with a later push is
  duplicate-free and `check()` stays clean.
- `create_hypertable` rendering is now a single shared helper
  (`sqlpush.annotations.create_hypertable_sql`) consumed by both the
  directive and the listener — the two mechanisms cannot drift apart.

### Fixed

- Shared native enums no longer render duplicate `CREATE TYPE` across table
  ops: SQLAlchemy's offline per-table render embeds the enum DDL in EVERY
  table op that references it (per-invoke create/drop memos), so two tables
  sharing one Python enum produced an unreplayable plan — the second
  `CREATE TYPE` died with `DuplicateObject` on BOTH the push path and
  `migrate` replay of a generated chain. The plan now emits each
  verbatim `CREATE/DROP TYPE` statement exactly once (first occurrence
  stays embedded in its original op); non-verbatim duplicates still fail
  loudly at apply — no silent first-win.
- TimescaleDB's born-DESC default time index (`<table>_<dimension>_idx`)
  now compares EQUAL to a metadata-declared index with the same qualified
  name, owning hypertable and column sequence: the sort-order difference is
  extension-birth state, not drift. Previously a declared time index over
  a `create_hypertable`-defaulted table reported a `drop_index` +
  `add_index` PAIR on every `check()`/`plan()` (both-present sibling of the
  DB-only auto-index prune). Genuinely different declarations (reordered or
  extra columns) still report the pair.

## [0.4.0] - 2026-09-02

### Added

- Migration chain engine: `revision` / `migrate` / `stamp` verbs (API-first
  + CLI) over annotated-SQL chain files — plain editable SQL whose
  `-- sqlpush: revision=NNNN risk=SAFE|RISKY|DESTRUCTIVE` header carries
  the risk classification (the maximum over the file's ops, by an explicit
  rank map). Op labels are legal SQL comments, so generated files are
  directly psql-runnable; parsing is fail-loud: a missing or malformed
  header refuses the file — never assume-safe (`MigrationFileError`, a
  `SqlpushError` subclass). `parent=` / `generated=` header info is
  optional-ignorable.
- `revision` writes the next `NNNN_slug.sql` from models-vs-reference-DB
  drift: gap-free numbering (max NNNN + 1), message slug whitelisted to
  `[a-z0-9_]`, refuses empty drift loudly ("nothing to revise"), refuses
  to overwrite an existing file, and reports filesystem failures as typed
  errors. The reference DB is caller-provided — sqlpush stays docker-free.
- `migrate` replays pending files under the same advisory-lock key
  derivation as `push`: each file's WHOLE text runs in ONE multi-statement
  execute inside a per-file transaction (nothing is tokenized —
  dollar-quoted bodies are safe), and the checksum row (sha256 over
  newline-normalized bytes) is inserted INSIDE that same transaction, so a
  crash between apply and bookkeeping cannot crash-loop the file over
  existing objects. Gates: per-file destructive gate off the header's
  `risk=`, checksum-mismatch refuse for files edited after apply, and
  strict ordering — any blocked file stops the chain, nothing later runs.
  CLI exits 0 clean / 1 blocked or partial.
- `stamp` adopts an existing DB into the chain (bootstrap seam): registers
  every parseable file — checksums included — WITHOUT executing any SQL;
  registered files are reported in `skipped` (stamp never "applies").
  Caveat: re-running `stamp` over a DB whose chain was applied via
  `migrate` REFRESHES the recorded checksums — edit-detection for
  already-applied files is reset; the mismatch-refuse + `--force` shape
  lands in 0.4.x/0.5.
- `MigrateReport(applied, skipped, blocked, partial_failure, notes)` — the
  chain-run report: bare filenames in the lists (exact CI membership
  checks) with the human-readable reasons in `notes` as `"name: reason"`
  strings.

### Fixed

- `sqlpush_versions` (the chain engine's own bookkeeping table) is pruned
  from diffs as a system table, same as `alembic_version`: public-scoped
  `check` / `plan` / `push` against a migrated DB no longer report
  `drop_table sqlpush_versions` as destructive drift (cycle-5 review
  finding C1, fixed pre-publish).

## [0.3.0] - 2026-09-01

### Fixed

- `push` no longer fails with `DuplicateTable` on tables whose indexes
  are attached by construction-time instrumentation (e.g. geoalchemy2's
  implicit spatial index, appended by a listener when the `Table` is
  built): `CreateTableOp.from_table` captures columns+constraints only,
  so a plain declared `Index(...)` renders standalone-only and is
  unaffected — but when instrumentation re-attaches the index during
  the op's table reconstruction, the offline CreateTable render embeds
  it as a trailing statement and autogen ALSO emits a standalone
  `CreateIndexOp` for it: the identical statement executed twice. The
  plan now drops a standalone `add_index` op whose statement is already
  embedded in the `add_table` render of the same table (found by the
  atlas dogfooding push fire-test, cycle 4 findings F1/F2).
- `@hypertable` tables living in a non-default schema now get a
  schema-qualified `create_hypertable('schema.table', ...)` relation.
  Unqualified, the relation resolved via the session search_path to
  `public.<name>` and the op died with `UndefinedTable` before the
  hypertable could register. The state-aware registration probe is
  schema-qualified too (`hypertable_schema` + `hypertable_name`), so a
  same-named hypertable in another schema no longer suppresses the op
  (atlas dogfooding push fire-test, cycle 4 finding F3a).

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
