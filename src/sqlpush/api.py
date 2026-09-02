# src/sqlpush/api.py
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import NoReturn

from sqlalchemy import MetaData
from sqlalchemy.engine import Engine
from sqlalchemy.exc import (
    ArgumentError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from sqlpush.apply.executor import apply_plan, with_advisory_lock
from sqlpush.chain.format import (
    RISK_RANK,
    next_revision_id,
    render_migration_file,
)
from sqlpush.chain.migrate import run_migrate, run_stamp
from sqlpush.core.diff import DiffEngine
from sqlpush.directives.timescale import hypertable_operations
from sqlpush.types import (
    CheckResult,
    ConnectFailed,
    MigrateReport,
    Plan,
    Report,
    SqlpushError,
)

_engine = DiffEngine()

# Connection-phase failures (server unreachable, bad DSN, auth) re-type
# as ConnectFailed; every other SQLAlchemyError leaving a DB-touching
# verb becomes a plain SqlpushError. Either way only typed errors
# escape the API.
_CONNECT_PHASE = (OperationalError, ArgumentError)


def _raise_typed(exc: SQLAlchemyError) -> NoReturn:
    if isinstance(exc, _CONNECT_PHASE):
        raise ConnectFailed(f"could not connect to the database: {exc}") from exc
    raise SqlpushError(f"database error: {exc}") from exc


def _build_plan(
    metadata: MetaData, engine: Engine, schemas, exclude, concurrently: bool = True
) -> Plan:
    p = _engine.plan(metadata, engine, schemas=schemas, exclude=exclude, concurrently=concurrently)
    return Plan(operations=p.operations + tuple(hypertable_operations(metadata, engine)))


class _PlannerWithDirectives(DiffEngine):
    """DiffEngine facade whose plans carry directive ops.

    ``with_advisory_lock`` re-plans via ``reverify.plan(metadata, engine,
    schemas=..., exclude=...)`` once the lock is won; routing that call
    through the plan builder keeps directive operations (e.g.
    create_hypertable) on the DEFAULT locked push path.
    """

    def __init__(self, engine: DiffEngine) -> None:
        self._engine = engine

    def plan(self, metadata, engine, *, schemas=None, exclude=(), concurrently=True) -> Plan:
        return _build_plan(metadata, engine, schemas, exclude, concurrently)


def plan(metadata, engine, *, schemas=None, exclude=(), concurrently: bool = True) -> Plan:
    try:
        return _build_plan(metadata, engine, schemas, exclude, concurrently)
    except SQLAlchemyError as exc:
        _raise_typed(exc)


def push(
    metadata,
    engine,
    *,
    safe_only=False,
    allow_destructive=False,
    lock=True,
    lock_timeout=5.0,
    statement_timeout=None,
    advisory_wait=30.0,
    schemas=None,
    exclude=(),
    concurrently=True,
) -> Report:
    try:
        if lock:
            return with_advisory_lock(
                engine,
                metadata,
                wait=advisory_wait,
                timeout=lock_timeout,
                allow_destructive=allow_destructive,
                safe_only=safe_only,
                reverify=_PlannerWithDirectives(_engine),
                schemas=schemas,
                exclude=exclude,
                # THREADING (pinned): the lock winner re-plans via
                # reverify.plan(...) — both knobs must reach it and the
                # final apply_plan, or the winner renders differently
                # than requested
                concurrently=concurrently,
                statement_timeout=statement_timeout,
            )
        p = _build_plan(metadata, engine, schemas, exclude, concurrently)
        return apply_plan(
            engine,
            p,
            allow_destructive=allow_destructive,
            safe_only=safe_only,
            lock_timeout=lock_timeout,
            statement_timeout=statement_timeout,
        )
    except SQLAlchemyError as exc:
        _raise_typed(exc)


def check(metadata, engine, *, schemas=None, exclude=()) -> CheckResult:
    try:
        p = _build_plan(metadata, engine, schemas, exclude)
    except SQLAlchemyError as exc:
        _raise_typed(exc)
    return CheckResult(
        clean=not p.drift,
        drift=p.drift,
        has_destructive=p.has_destructive,
    )


def revision(
    metadata,
    ref_engine,
    *,
    out_dir="migrations/versions",
    message=None,
    schemas=None,
    exclude=(),
    concurrently=True,
) -> Path:
    """Generate the next annotated-SQL migration file from models-vs-ref drift.

    The reference DB must sit at the chain head (caller-provided — sqlpush
    stays docker-free). Empty drift refuses loudly: no empty files.
    ``concurrently`` follows :func:`plan` (default True: existing-table
    indexes render CONCURRENTLY in the generated file).
    """
    p = plan(metadata, ref_engine, schemas=schemas, exclude=exclude, concurrently=concurrently)
    if not p.operations:
        raise SqlpushError("no drift between models and reference DB — nothing to revise")
    risk = max((op.risk for op in p.operations), key=lambda r: RISK_RANK[r])
    ops = [(f"[{op.risk.name}] {op.type} {op.table or '?'}", op.sql) for op in p.operations]
    out = Path(out_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # same typing as the write below: only SqlpushError subclasses
        # escape the API surface (e.g. out_dir under a file)
        raise SqlpushError(f"cannot create migrations directory {out}: {exc}") from exc
    rev_id = next_revision_id(out)
    slug = re.sub(r"[^a-z0-9_]+", "_", (message or "migration").lower())[:40]
    path = out / f"{rev_id}_{slug}.sql"
    if path.exists():
        # NNNN+slug collision (e.g. same message re-run without the file being
        # consumed) must be loud, never a silent overwrite of chain history
        raise SqlpushError(f"refusing to overwrite existing migration file {path}")
    prev_n = int(rev_id) - 1
    try:
        path.write_text(
            render_migration_file(
                ops=ops,
                revision_id=rev_id,
                risk=risk,
                message=message,
                parent=f"{prev_n:04d}" if prev_n >= 1 else None,
            )
        )
    except OSError as exc:
        raise SqlpushError(f"cannot write migration file {path}: {exc}") from exc
    return path


def migrate(
    target,
    *,
    chain_dir="migrations/versions",
    allow_destructive=False,
    advisory_wait=30.0,
    lock_timeout=5.0,
) -> MigrateReport:
    """Replay annotated-SQL chain files with gates + same-txn bookkeeping.

    ``target`` is a DSN string, sync ``Engine`` or ``AsyncEngine`` (resolved
    via ``_sync_engine_from``; engines created here are disposed).
    ``advisory_wait`` bounds the advisory-lock wait and ``lock_timeout``
    bounds each per-file transaction's lock wait (seconds; 0 = fail
    immediately — same contract as ``push``). See
    ``chain.migrate.run_migrate`` for the execution contract.
    """
    engine, dispose = _sync_engine_from(target)
    try:
        return run_migrate(
            engine,
            chain_dir=chain_dir,
            allow_destructive=allow_destructive,
            advisory_wait=advisory_wait,
            lock_timeout=lock_timeout,
        )
    except SQLAlchemyError as exc:
        # MigrationFileError/SqlpushError (typed) pass through untouched
        _raise_typed(exc)
    finally:
        if dispose:
            engine.dispose()


def stamp(target, *, chain_dir="migrations/versions", force: bool = False) -> MigrateReport:
    """Bootstrap: register chain files as applied WITHOUT executing SQL.

    For adopting a DB whose schema already reflects the chain. A file
    already registered with a different checksum (edited after apply)
    is refused unless ``force`` is set — edit detection survives
    re-stamping. ``target`` resolution and error typing match
    :func:`migrate`. See ``chain.migrate.run_stamp`` for the report
    convention.
    """
    engine, dispose = _sync_engine_from(target)
    try:
        return run_stamp(engine, chain_dir=chain_dir, force=force)
    except SQLAlchemyError as exc:
        # MigrationFileError/SqlpushError (typed) pass through untouched
        _raise_typed(exc)
    finally:
        if dispose:
            engine.dispose()


def _translate_asyncpg(url):
    """Map a ``postgresql+asyncpg`` URL onto the sync psycopg driver.

    An asyncpg URL cannot back a SYNC engine (the dialect is
    async-only), but psycopg is a runtime dependency — the translated
    engine actually connects. asyncpg itself is never required in this
    process. Everything but the driver (host/db/credentials/query
    options) is preserved via the URL API, no string surgery.
    """
    if url.drivername == "postgresql+asyncpg":
        return url.set(drivername="postgresql+psycopg")
    return url


def _sync_engine_from(target):
    if isinstance(target, AsyncEngine):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        dsn = _translate_asyncpg(target.url)
        return create_engine(dsn.render_as_string(hide_password=False), poolclass=NullPool), True
    if isinstance(target, str):
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url
        from sqlalchemy.pool import NullPool

        # make_url raises the same ArgumentError a bad DSN would raise
        # inside create_engine — error typing downstream is unchanged
        return create_engine(_translate_asyncpg(make_url(target)), poolclass=NullPool), True
    return target, False


_ENSURE_MODES = ("push", "check")


def ensure_schema(metadata, target, mode="push", **kwargs) -> Report:
    # validated BEFORE any engine work: a typo'd mode must never fall
    # through to the writing branch
    if mode not in _ENSURE_MODES:
        raise SqlpushError(f"invalid mode {mode!r}: expected 'push' or 'check'")
    try:
        engine, dispose = _sync_engine_from(target)
        try:
            if mode == "check":
                result = check(metadata, engine, **kwargs)
                if result.drift:
                    raise SqlpushError("schema drift detected (ensure_schema mode='check')")
                return Report()
            return push(metadata, engine, **kwargs)
        finally:
            if dispose:
                engine.dispose()
    except SQLAlchemyError as exc:
        # includes ArgumentError from a bad string DSN in _sync_engine_from
        _raise_typed(exc)


async def aplan(metadata, engine, **kw):
    return await asyncio.to_thread(plan, metadata, engine, **kw)


async def apush(metadata, engine, **kw):
    return await asyncio.to_thread(push, metadata, engine, **kw)


async def acheck(metadata, engine, **kw):
    return await asyncio.to_thread(check, metadata, engine, **kw)


async def aensure_schema(metadata, target, **kw):
    return await asyncio.to_thread(ensure_schema, metadata, target, **kw)
