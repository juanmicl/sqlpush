"""migrate: replay annotated-SQL chain files with gates + bookkeeping.

Execution contract (spec 2026-09-02 §5): each file's WHOLE TEXT is replayed
in ONE ``exec_driver_sql()`` call inside a per-file transaction — the parsed
``ops`` are for gating/display only, NEVER reconstructed for execution
(psycopg3 accepts multi-statement strings; nothing is tokenized, so
dollar-quoted bodies are safe). The checksum row is inserted INSIDE the same
transaction as the file's SQL — bookkeeping in a separate txn would let a
crash between apply and registry re-apply the file forever (crash-loop over
existing objects).

Fail-loud ordering: any blocked file (parse error, checksum mismatch,
destructive gate, SQL failure) stops the chain — nothing later runs (R4).
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from sqlpush.apply.executor import advisory_key
from sqlpush.chain.format import MigrationFileError, checksum, parse_migration_file
from sqlpush.types import MigrateReport, RiskClass

_VERSIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS public.sqlpush_versions ("
    "name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
    "applied_at timestamptz NOT NULL DEFAULT now())"
)


def run_migrate(engine: Engine, *, chain_dir: str | Path, allow_destructive: bool) -> MigrateReport:
    chain_path = Path(chain_dir)
    if not chain_path.is_dir():
        # a typo'd --dir silently no-op'ing is the footgun; an empty-but-
        # EXISTING dir is a legitimate idle run (versions table still ensured)
        raise MigrationFileError(f"chain dir not found: {chain_path}")

    applied: list[str] = []
    skipped: list[str] = []
    blocked: list[str] = []
    notes: list[str] = []
    partial = False
    chain = sorted(chain_path.glob("*.sql"))
    with engine.connect() as conn:
        # session-level lock with the SAME key derivation as push
        # (fnv1a_32(b"sqlpush") ^ db oid): serializes concurrent migrates and
        # excludes push on the same database
        key = advisory_key(conn)
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        # close the txn opened by the key query: session advisory locks survive
        # COMMIT/ROLLBACK, and an idle-in-transaction winner is exposed to
        # idle_in_transaction_session_timeout (executor.py winner-path note)
        conn.commit()
        try:
            conn.execute(text(_VERSIONS_DDL))
            conn.commit()
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
                        # whole-file replay: exec_driver_sql bypasses text()'s
                        # bind-param parsing entirely — ":casts" and ":=" in
                        # hand-edited SQL must reach the server verbatim
                        conn.exec_driver_sql(raw)
                        conn.execute(
                            text(
                                "INSERT INTO public.sqlpush_versions (name, sha256) VALUES (:n, :s)"
                            ),
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
        finally:
            # best-effort unlock (executor pattern): a secondary failure here
            # must never mask the report; the session lock dies with the
            # connection even if this fails
            if not conn.closed:
                with contextlib.suppress(Exception):
                    conn.rollback()
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                    conn.commit()
