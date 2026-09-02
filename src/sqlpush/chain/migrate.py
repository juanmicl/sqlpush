"""migrate/stamp: chain replay + bootstrap with gates and bookkeeping.

migrate execution contract (spec 2026-09-02 §5): each file's WHOLE TEXT is
replayed in ONE ``exec_driver_sql()`` call inside a per-file transaction —
the parsed ``ops`` are for gating/display only, NEVER reconstructed for
execution (psycopg3 accepts multi-statement strings; nothing is tokenized,
so dollar-quoted bodies are safe). The checksum row is inserted INSIDE the
same transaction as the file's SQL — bookkeeping in a separate txn would
let a crash between apply and registry re-apply the file forever
(crash-loop over existing objects).

Fail-loud ordering: any blocked file (parse error, checksum mismatch,
destructive gate, SQL failure) stops the chain — nothing later runs (R4).
The advisory lock wait is BOUNDED (--advisory-wait, default 30s): an
exhausted budget raises a typed error instead of hanging on a stuck
holder — diagnose the holder via pg_locks / pg_stat_activity.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from sqlpush.apply.executor import advisory_key
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
) -> MigrateReport:
    if lock_timeout < 0:
        # same contract as push (executor.with_advisory_lock): budgets
        # are typed floats, never user input, and a negative one must
        # fail before any file or connection work
        raise SqlpushError(f"lock_timeout must be >= 0, got {lock_timeout}")
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
            try:
                with conn.begin():
                    # txn-scoped: SET LOCAL dies with the per-file txn
                    # (style: push's transactional segment, executor.py).
                    # NOTE: PostgreSQL does not accept bind parameters for
                    # SET (utility statement), so the int is inlined;
                    # lock_timeout is a typed float parameter, not user
                    # input. A chain file blocked behind another
                    # transaction's lock fails fast instead of queuing.
                    conn.execute(text(f"SET LOCAL lock_timeout = {int(lock_timeout * 1000)}"))
                    # whole-file replay: exec_driver_sql bypasses text()'s
                    # bind-param parsing entirely — ":casts" and ":=" in
                    # hand-edited SQL must reach the server verbatim
                    conn.exec_driver_sql(raw)
                    conn.execute(
                        text("INSERT INTO public.sqlpush_versions (name, sha256) VALUES (:n, :s)"),
                        {"n": f.name, "s": checksum(raw)},
                    )
                applied.append(f.name)
            except Exception as exc:  # noqa: BLE001 — report, no mask
                blocked.append(f.name)
                notes.append(f"{f.name}: {exc}")
                partial = True
                break
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
