# src/sqlpush/apply/executor.py
from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.engine import Engine

from sqlpush.types import (
    AppliedOperation,
    Plan,
    PlannedOperation,
    Report,
    RiskClass,
    SqlpushError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import MetaData

    from sqlpush.core.diff import DiffEngine


def _is_concurrent(op: PlannedOperation) -> bool:
    # Union (A5): the `concurrent` flag is authoritative for generated
    # plans (set by the diff's injection); the SQL substring keeps
    # hand-built Plans splitting to the autocommit segment.
    return op.concurrent or "CONCURRENTLY" in op.sql.upper()


@contextlib.contextmanager
def _session_gucs(conn, **gucs: str | int) -> Iterator[None]:
    """Session-level ``SET``/``RESET`` with pooled-connection hygiene.

    The autocommit (CONCURRENTLY) segment cannot use ``SET LOCAL`` —
    there is no surrounding transaction — so its GUCs are session-level,
    and a session ``SET`` survives segment end (0.4.2 lesson). RESET runs
    per GUC in ``finally``; a reset failure is suppressed (it must never
    mask the segment's own outcome) and the connection is invalidated so
    the pool discards it instead of handing the next borrower a session
    still carrying the shrunken budget.
    """
    try:
        for name, value in gucs.items():
            # names come from sqlpush code, values are ints inlined in
            # the same style as the txn segment's SET LOCAL (PostgreSQL
            # does not accept bind parameters for SET)
            conn.execute(text(f"SET {name} = '{value}'"))
        yield
    finally:
        for name in gucs:
            try:
                conn.execute(text(f"RESET {name}"))
            except Exception:  # noqa: BLE001  # never mask the segment's outcome
                conn.invalidate()


def apply_plan(
    engine: Engine,
    plan: Plan,
    *,
    allow_destructive: bool = False,
    safe_only: bool = False,
    lock_timeout: float = 5.0,
    statement_timeout: float | None = None,
) -> Report:
    start = time.monotonic()
    if statement_timeout is not None and statement_timeout < 0:
        raise SqlpushError(f"statement_timeout must be >= 0, got {statement_timeout}")

    blocked = tuple(op for op in plan.operations if op.risk is RiskClass.DESTRUCTIVE)
    if blocked and not allow_destructive:
        # Destructive gate: nothing executes at all. When safe_only is also
        # active, risky ops are still recorded as policy-skipped; destructive
        # ones are already in `blocked` and are not double-listed.
        skipped = (
            tuple(op for op in plan.operations if op.risk is RiskClass.RISKY) if safe_only else ()
        )
        return Report(
            applied=(),
            blocked=blocked,
            skipped=skipped,
            partial_failure=False,
            duration=time.monotonic() - start,
        )

    if safe_only:
        runnable = [op for op in plan.operations if op.risk is RiskClass.SAFE]
        skipped = tuple(op for op in plan.operations if op.risk is not RiskClass.SAFE)
    else:
        runnable = list(plan.operations)
        skipped = ()

    applied: list[AppliedOperation] = []
    partial_failure = False

    # --- CONCURRENTLY segment: autocommit, one op per statement ----------
    concurrent = [op for op in runnable if _is_concurrent(op)]
    if concurrent:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            # A7: the concurrent segment gets the SAME lock budget as the
            # txn segment (session-level: autocommit has no surrounding
            # transaction for SET LOCAL), plus the optional statement
            # budget; both RESET before the connection returns to the pool
            session_gucs: dict[str, int] = {"lock_timeout": int(lock_timeout * 1000)}
            if statement_timeout is not None:
                session_gucs["statement_timeout"] = int(statement_timeout * 1000)
            with _session_gucs(conn, **session_gucs):
                for op in concurrent:
                    try:
                        conn.execute(text(op.sql))
                        applied.append(AppliedOperation(op.type, "applied"))
                    except Exception:  # noqa: BLE001  # concurrent ops record any DB failure and continue
                        applied.append(AppliedOperation(op.type, "failed"))
                        partial_failure = True

    # --- transactional segment: atomic ----------------------------------
    plain = [op for op in runnable if not _is_concurrent(op)]
    if plain:
        try:
            with engine.begin() as conn:
                # NOTE: PostgreSQL does not accept bind parameters for SET
                # (utility statement), so the int is inlined; both timeouts
                # are typed float parameters, not user input.
                conn.execute(text(f"SET LOCAL lock_timeout = {int(lock_timeout * 1000)}"))
                if statement_timeout is not None:
                    conn.execute(
                        text(f"SET LOCAL statement_timeout = {int(statement_timeout * 1000)}")
                    )
                for op in plain:
                    conn.execute(text(op.sql))
            applied.extend(AppliedOperation(op.type, "applied") for op in plain)
        except Exception as exc:
            msg = f"transactional segment failed, rolled back: {exc}"
            if applied:
                msg += f"; concurrent segment had already applied: {[a.type for a in applied]}"
            raise SqlpushError(msg) from exc

    return Report(
        applied=tuple(applied),
        blocked=(),
        skipped=skipped,
        partial_failure=partial_failure,
        duration=time.monotonic() - start,
    )


# --- advisory lock: winner/loser semantics --------------------------------
#
# v0.1 design notes:
# - The lock is session-scoped and taken/released on `conn`, while
#   `apply_plan` opens its own connections. Acceptable because the
#   advisory lock's job is worker coordination (one pusher per database
#   at a time), not txn participation.
# - Session advisory locks survive ROLLBACK: the winner rolls back the
#   txn opened by the key/probe queries right after acquiring, so the
#   session never idles in transaction (an
#   idle_in_transaction_session_timeout would kill the winner mid-push
#   and silently release the lock).


def fnv1a_32(data: bytes) -> int:
    h = 2166136261
    for byte in data:
        h ^= byte
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def advisory_key(conn) -> int:
    # Deterministic across DSN spellings: the key derives from the
    # database's oid, not from the connection string.
    oid = conn.execute(
        text("SELECT oid FROM pg_database WHERE datname = current_database()")
    ).scalar()
    return fnv1a_32(b"sqlpush") ^ oid


def with_advisory_lock(
    engine: Engine,
    metadata: MetaData,
    *,
    wait: float = 30.0,
    timeout: float = 5.0,
    allow_destructive: bool = False,
    safe_only: bool = False,
    reverify: DiffEngine | None = None,
    schemas: Sequence[str] | None = None,
    exclude: Sequence[str] = (),
    concurrently: bool = True,
    statement_timeout: float | None = None,
) -> Report:
    """Winner migrates; losers block (bounded), then re-verify.

    Losers poll ``pg_try_advisory_lock`` every 0.5 s against a
    ``time.monotonic()`` deadline; once the lock is acquired the winner
    path re-plans (covering the case where the previous winner died
    mid-push). Raises :class:`SqlpushError` if the wait budget is
    exhausted. ``concurrently`` and ``statement_timeout`` thread into
    BOTH the winner's re-plan and its apply — the re-plan must render
    exactly what the caller asked for.
    """
    if reverify is None:
        raise SqlpushError(
            "reverify is required: pass an object exposing a "
            "DiffEngine-compatible .plan(metadata, engine, ...)"
        )
    if wait < 0:
        raise SqlpushError(f"wait must be >= 0, got {wait}")
    if timeout < 0:
        raise SqlpushError(f"timeout (lock_timeout) must be >= 0, got {timeout}")
    deadline = time.monotonic() + wait
    with engine.connect() as conn:
        key = advisory_key(conn)
        locked = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        while not locked and time.monotonic() < deadline:
            time.sleep(0.5)
            locked = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        if not locked:
            raise SqlpushError(f"another sqlpush worker holds the advisory lock after {wait}s")
        # close the txn opened by the key/probe queries: session
        # advisory locks survive ROLLBACK, and an idle-in-transaction
        # winner is exposed to idle_in_transaction_session_timeout
        # (killed mid-push = lock silently released)
        conn.rollback()
        try:
            plan = reverify.plan(
                metadata,
                engine,
                schemas=schemas,
                exclude=exclude,
                concurrently=concurrently,
            )
            if not plan.drift:
                return Report()
            return apply_plan(
                engine,
                plan,
                allow_destructive=allow_destructive,
                safe_only=safe_only,
                lock_timeout=timeout,
                statement_timeout=statement_timeout,
            )
        finally:
            # best-effort unlock: a secondary failure here (e.g. the
            # txn is already aborted) must never mask the original
            # exception out of the winner path
            if not conn.closed:
                with contextlib.suppress(Exception):
                    conn.rollback()
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
