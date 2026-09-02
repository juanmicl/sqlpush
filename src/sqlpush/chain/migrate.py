"""migrate/stamp: chain replay + bootstrap with gates and bookkeeping.

migrate execution contract (spec 2026-09-02 §5, 0.5.0 hybrid): a file
whose text contains no CONCURRENTLY replays its WHOLE TEXT in ONE
``exec_driver_sql()`` call inside a per-file transaction — nothing is
tokenized, so dollar-quoted bodies (and their internal ``--`` lines)
are safe; that is the 0.4.2 fast path, byte-identical. A file that
DOES contain CONCURRENTLY replays per-op on the op-label delimiters
(``mf.ops``): the plain segment runs FIRST in one transaction (chain
files can create→index within one file — a table made by a plain op
must exist before a concurrent index on it; push's concurrent-first
order does NOT apply here), then concurrent ops run per-op on a
dedicated autocommit connection, and only after all of them succeed
is the versions row written. Known cost (chain spec §7): hand-edits
bypass the label mechanism — a label-less body containing
CONCURRENTLY routes the whole body to the autocommit lane (executed
statement-by-statement: the server runs a multi-statement string as
one implicit transaction, which CONCURRENTLY refuses), and lines
starting ``--`` are stripped from per-op parsing (they exist inside
dollar-quoted bodies at the author's risk). Concurrent-free files
are immune: they take the raw fast path. The crash window (plain
committed, concurrent applied, no row yet) re-runs loud on existing
objects — the documented chain-side cost of per-op replay.

Fail-loud ordering: any blocked file (parse error, checksum mismatch,
destructive gate, SQL failure) stops the chain — nothing later runs (R4).
The advisory lock wait is BOUNDED (--advisory-wait, default 30s): an
exhausted budget raises a typed error instead of hanging on a stuck
holder — diagnose the holder via pg_locks / pg_stat_activity.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from sqlpush.apply.executor import _session_gucs, advisory_key
from sqlpush.chain.format import MigrationFileError, checksum, parse_migration_file
from sqlpush.types import MigrateReport, RiskClass, SqlpushError

_VERSIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS public.sqlpush_versions ("
    "name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
    "applied_at timestamptz NOT NULL DEFAULT now())"
)


def _chain_files(chain_dir: str | Path) -> list[Path]:
    chain_path = Path(chain_dir)
    if not chain_path.is_dir():
        # a typo'd --dir silently no-op'ing is the footgun; an empty-but-
        # EXISTING dir is a legitimate idle run (versions table still ensured)
        raise MigrationFileError(f"chain dir not found: {chain_path}")
    return sorted(chain_path.glob("*.sql"))


def _set_local_gucs(
    conn: Connection, *, lock_timeout: float, statement_timeout: float | None
) -> None:
    # txn-scoped: SET LOCAL dies with the surrounding transaction (style:
    # push's transactional segment, executor.py). NOTE: PostgreSQL does
    # not accept bind parameters for SET (utility statement), so ints
    # are inlined; both timeouts are typed float parameters, not user
    # input. A chain file blocked behind another transaction's lock
    # fails fast instead of queuing.
    conn.execute(text(f"SET LOCAL lock_timeout = {int(lock_timeout * 1000)}"))
    if statement_timeout is not None:
        conn.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout * 1000)}"))


def _record_version(conn: Connection, name: str, sha: str) -> None:
    conn.execute(
        text("INSERT INTO public.sqlpush_versions (name, sha256) VALUES (:n, :s)"),
        {"n": name, "s": sha},
    )


_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _split_statements(sql: str) -> list[str]:
    """Split a concurrent-lane op into single statements.

    Physical constraint (live-verified): the server executes a
    multi-statement simple-protocol string inside ONE implicit
    transaction block, even on an autocommit connection — so
    ``CREATE INDEX CONCURRENTLY`` cannot ride a multi-statement
    ``exec_driver_sql`` call; the autocommit lane needs one statement
    per execute. Split on top-level ``;`` only, treating single-quoted
    strings (``''`` doubling), double-quoted identifiers, dollar-quoted
    bodies (``$tag$...$tag$``) and ``--`` / ``/* */`` comments as
    opaque (psql-equivalent boundaries). Generated ops are single
    statements — the splitter is a no-op for them; only hand-edited
    label-less bodies exercise it (chain spec §7: at the author's
    risk). The plain segment and the fast path NEVER split.
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":  # line comment
            j = sql.find("\n", i)
            j = n if j == -1 else j + 1
            buf.append(sql[i:j])
            i = j
        elif ch == "/" and nxt == "*":  # block comment
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            buf.append(sql[i:j])
            i = j
        elif ch == "'":  # string literal, '' doubling
            j = i + 1
            while j < n:
                if sql[j] == "'" and not (j + 1 < n and sql[j + 1] == "'"):
                    break
                j += 2 if sql[j] == "'" else 1
            j = min(j + 1, n)
            buf.append(sql[i:j])
            i = j
        elif ch == '"':  # quoted identifier
            j = sql.find('"', i + 1)
            j = n if j == -1 else j + 1
            buf.append(sql[i:j])
            i = j
        elif ch == "$":
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:  # dollar-quoted body: opaque through the closing tag
                end = sql.find(m.group(0), m.end())
                j = n if end == -1 else end + len(m.group(0))
                buf.append(sql[i:j])
                i = j
            else:
                buf.append(ch)
                i += 1
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


@contextlib.contextmanager
def _chain_session(engine: Engine, *, advisory_wait: float = 30.0) -> Iterator[Connection]:
    """Session-scoped advisory lock + versions table, shared by every verb.

    Same key derivation as push (fnv1a_32(b"sqlpush") ^ db oid): serializes
    concurrent chain workers and excludes push on the same database. The
    wait is BOUNDED, mirroring executor.with_advisory_lock:
    ``pg_try_advisory_lock`` polled every 0.5 s against a
    ``time.monotonic()`` deadline; an exhausted budget raises
    :class:`SqlpushError` — a blocking ``pg_advisory_lock`` would hang
    forever on a stuck holder. The txn opened by the key/probe queries
    is committed right away — session advisory locks survive
    COMMIT/ROLLBACK, and an idle-in-transaction session is exposed to
    idle_in_transaction_session_timeout (executor.py note).
    """
    if advisory_wait < 0:
        raise SqlpushError(f"advisory_wait must be >= 0, got {advisory_wait}")
    with engine.connect() as conn:
        key = advisory_key(conn)
        deadline = time.monotonic() + advisory_wait
        locked = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        while not locked and time.monotonic() < deadline:
            time.sleep(0.5)
            locked = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        if not locked:
            raise SqlpushError(
                f"another sqlpush worker holds the advisory lock after {advisory_wait}s"
            )
        conn.commit()
        try:
            conn.execute(text(_VERSIONS_DDL))
            conn.commit()
            yield conn
        finally:
            # best-effort unlock (executor pattern): a secondary failure here
            # must never mask the primary outcome; the session lock dies with
            # the connection even if this fails
            if not conn.closed:
                with contextlib.suppress(Exception):
                    conn.rollback()
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                    conn.commit()


def run_migrate(
    engine: Engine,
    *,
    chain_dir: str | Path,
    allow_destructive: bool,
    advisory_wait: float = 30.0,
    lock_timeout: float = 5.0,
    statement_timeout: float | None = None,
) -> MigrateReport:
    if lock_timeout < 0:
        # same contract as push (executor.with_advisory_lock): budgets
        # are typed floats, never user input, and a negative one must
        # fail before any file or connection work
        raise SqlpushError(f"lock_timeout must be >= 0, got {lock_timeout}")
    if statement_timeout is not None and statement_timeout < 0:
        raise SqlpushError(f"statement_timeout must be >= 0, got {statement_timeout}")
    applied: list[str] = []
    skipped: list[str] = []
    blocked: list[str] = []
    notes: list[str] = []
    partial = False
    chain = _chain_files(chain_dir)
    with _chain_session(engine, advisory_wait=advisory_wait) as conn:
        recorded = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT name, sha256 FROM public.sqlpush_versions"))
        }
        conn.commit()
        # B11: ONE lazily-created autocommit connection for the whole
        # walk, GUC-pinned for its entire lifetime. ExitStack ordering:
        # _session_gucs' finally (per-GUC RESET, suppress + invalidate
        # on reset failure) unwinds BEFORE the connection close (LIFO).
        concurrent_stack = contextlib.ExitStack()
        concurrent_conn: Connection | None = None
        try:
            for f in chain:
                raw = f.read_text()
                try:
                    mf = parse_migration_file(raw, name=f.name)
                except MigrationFileError as exc:
                    blocked.append(f.name)
                    notes.append(f"{f.name}: {exc}")
                    break  # orden estricto: nada posterior corre
                if f.name in recorded:
                    if recorded[f.name] != checksum(raw):
                        blocked.append(f.name)
                        notes.append(f"{f.name}: checksum mismatch (edited after apply?)")
                        break
                    skipped.append(f.name)
                    continue
                if mf.risk is RiskClass.DESTRUCTIVE and not allow_destructive:
                    blocked.append(f.name)
                    notes.append(f"{f.name}: DESTRUCTIVE requires --allow-destructive")
                    break
                if "CONCURRENTLY" not in raw.upper():
                    # fast path — byte-identical to 0.4.2: whole text,
                    # one txn, one exec_driver_sql call (":casts" and
                    # ":=" reach the server verbatim; dollar-quoted
                    # bodies and their internal `--` lines are safe —
                    # the parser's line-stripping quirks never execute
                    # on this path). Checksum row rides the SAME txn.
                    try:
                        with conn.begin():
                            _set_local_gucs(
                                conn,
                                lock_timeout=lock_timeout,
                                statement_timeout=statement_timeout,
                            )
                            conn.exec_driver_sql(raw)
                            _record_version(conn, f.name, checksum(raw))
                        applied.append(f.name)
                    except Exception as exc:  # noqa: BLE001 — report, no mask
                        blocked.append(f.name)
                        notes.append(f"{f.name}: {exc}")
                        partial = True
                        break
                    continue
                # mixed path — per-op replay on the op-label delimiters.
                # PLAIN SEGMENT FIRST (create→index dependencies inside
                # one file); concurrent ops afterwards, per-op on the
                # dedicated autocommit connection; versions row LAST.
                plain = [sql for _, sql in mf.ops if "CONCURRENTLY" not in sql.upper()]
                conc = [sql for _, sql in mf.ops if "CONCURRENTLY" in sql.upper()]
                plain_committed = False
                try:
                    if plain:
                        with conn.begin():
                            _set_local_gucs(
                                conn,
                                lock_timeout=lock_timeout,
                                statement_timeout=statement_timeout,
                            )
                            for sql in plain:
                                conn.exec_driver_sql(sql)
                        plain_committed = True
                    for sql in conc:
                        if concurrent_conn is None:
                            concurrent_conn = engine.connect().execution_options(
                                isolation_level="AUTOCOMMIT"
                            )
                            concurrent_stack.callback(concurrent_conn.close)
                            session_gucs: dict[str, int] = {
                                "lock_timeout": int(lock_timeout * 1000)
                            }
                            if statement_timeout is not None:
                                session_gucs["statement_timeout"] = int(statement_timeout * 1000)
                            concurrent_stack.enter_context(
                                _session_gucs(concurrent_conn, **session_gucs)
                            )
                        # statement-per-execute: a multi-statement string
                        # is one implicit server txn (CONCURRENTLY refuses
                        # it) — generated ops are single statements, so
                        # this split only fires for hand-edited bodies
                        for stmt in _split_statements(sql):
                            concurrent_conn.exec_driver_sql(stmt)
                    # never record before the concurrent ops succeed: a
                    # row for a file whose concurrent op then failed would
                    # be silent divergence. The crash window this leaves
                    # (plain committed + concurrent applied + no row)
                    # re-runs loud on existing objects — documented cost.
                    with conn.begin():
                        _record_version(conn, f.name, checksum(raw))
                    applied.append(f.name)
                except Exception as exc:  # noqa: BLE001 — report, no mask
                    blocked.append(f.name)
                    notes.append(f"{f.name}: {exc}")
                    if plain_committed:
                        # honest report: the plain segment of THIS file is
                        # already committed (same class as push's partial
                        # failure) and no versions row was recorded
                        notes.append(
                            f"{f.name}: plain segment already committed; no versions row recorded"
                        )
                    partial = True
                    break
        finally:
            concurrent_stack.close()
        return MigrateReport(
            applied=tuple(applied),
            skipped=tuple(skipped),
            blocked=tuple(blocked),
            partial_failure=partial,
            notes=tuple(notes),
        )


def run_stamp(engine: Engine, *, chain_dir: str | Path, force: bool = False) -> MigrateReport:
    """Register every parseable chain file WITHOUT executing any SQL.

    Bootstrap seam (spec §5 R7): adopt a DB whose schema already reflects
    the chain. Only the header must parse (fail-loud on THAT) — invalid SQL
    in a body still registers, because stamp never executes anything. A
    file already recorded with a DIFFERENT checksum is refused — it was
    edited after apply/stamp, and silently refreshing would wipe the
    edit-detection integrity — unless ``force`` is set; the first mismatch
    raises and nothing after it registers (strict order, same as migrate).
    Unrecorded files, unchanged re-stamps and forced re-stamps upsert
    idempotently (``ON CONFLICT (name) DO UPDATE``).

    Report convention: registered files are listed in ``skipped`` (stamp
    never populates ``applied`` and never sets ``partial_failure``); a
    header that fails to parse goes to ``blocked`` + ``notes`` and stops
    the walk (strict order, same as migrate).
    """
    skipped: list[str] = []
    blocked: list[str] = []
    notes: list[str] = []
    chain = _chain_files(chain_dir)
    with _chain_session(engine) as conn:
        recorded = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT name, sha256 FROM public.sqlpush_versions"))
        }
        conn.commit()
        for f in chain:
            raw = f.read_text()
            try:
                parse_migration_file(raw, name=f.name)
            except MigrationFileError as exc:
                blocked.append(f.name)
                notes.append(f"{f.name}: {exc}")
                break  # orden estricto: nada posterior se registra
            if f.name in recorded and recorded[f.name] != checksum(raw) and not force:
                raise SqlpushError(
                    f"{f.name}: checksum mismatch (file edited after apply?); "
                    "pass force=True/--force to accept the new content"
                )
            with conn.begin():
                conn.execute(
                    text(
                        "INSERT INTO public.sqlpush_versions (name, sha256) VALUES (:n, :s) "
                        "ON CONFLICT (name) DO UPDATE SET sha256 = EXCLUDED.sha256"
                    ),
                    {"n": f.name, "s": checksum(raw)},
                )
            skipped.append(f.name)
        return MigrateReport(
            applied=(),
            skipped=tuple(skipped),
            blocked=tuple(blocked),
            partial_failure=False,
            notes=tuple(notes),
        )
