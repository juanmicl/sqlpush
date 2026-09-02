# tests/test_api.py
from __future__ import annotations

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    text,
)

import sqlpush
from sqlpush import check, ensure_schema, plan, push
from sqlpush.annotations import hypertable
from sqlpush.apply.executor import apply_plan
from sqlpush.types import Plan, PlannedOperation, RiskClass

pytestmark = pytest.mark.pg


@pytest.fixture()
def md():
    m = MetaData()
    Table("api_hero", m, Column("id", Integer, primary_key=True), Column("name", String(30)))
    yield m
    import os

    import sqlalchemy as sa
    from sqlalchemy import create_engine

    eng = create_engine(
        os.environ.get(
            "SQLPUSH_TEST_DSN", "postgresql+psycopg://sqlpush:sqlpush@localhost:5433/sqlpush_test"
        ),
        poolclass=sa.pool.NullPool,
    )
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS api_hero"))
    eng.dispose()


@pytest.fixture()
def md_ht():
    m = MetaData()
    t = Table(
        "api_metrics",
        m,
        Column("id", Integer, primary_key=True),
        # Composite PK (id, ts): timescaledb requires the partitioning
        # column to be part of any primary key / unique constraint.
        Column("ts", DateTime, primary_key=True),
    )

    @hypertable(time_column="ts", chunk_time_interval="1 day")
    class ApiMetrics:
        __table__ = t

    assert ApiMetrics.__table__.info["sqlpush_hypertable"]
    yield m
    import os

    import sqlalchemy as sa
    from sqlalchemy import create_engine

    eng = create_engine(
        os.environ.get(
            "SQLPUSH_TEST_DSN", "postgresql+psycopg://sqlpush:sqlpush@localhost:5433/sqlpush_test"
        ),
        poolclass=sa.pool.NullPool,
    )
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS api_metrics"))
    eng.dispose()


@pytest.fixture()
def md_indexed():
    # Plain declared Index on a new table — on alembic 1.19.1
    # CreateTableOp.from_table captures columns+constraints only, so the
    # index renders standalone-only (no embedded copy, dedup no-op).
    # Together with test_push_declared_index_applies_once this pins
    # push-level idempotence of the standalone path; the
    # embedded-duplicate mechanism (instrumentation-appended indexes,
    # the actual fire-test F1/F2 trigger) is pinned in test_diff.py.
    m = MetaData()
    Table(
        "api_indexed",
        m,
        Column("id", Integer, primary_key=True),
        Column("email", String(50)),
        Index("ix_api_indexed_email", "email"),
    )
    yield m
    import os

    import sqlalchemy as sa
    from sqlalchemy import create_engine

    eng = create_engine(
        os.environ.get(
            "SQLPUSH_TEST_DSN", "postgresql+psycopg://sqlpush:sqlpush@localhost:5433/sqlpush_test"
        ),
        poolclass=sa.pool.NullPool,
    )
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS api_indexed"))
    eng.dispose()


def test_push_declared_index_applies_once(pg_engine, md_indexed):
    # Plain-declared standalone-only path: one standalone add_index op,
    # applied exactly once (must not raise DuplicateTable), leaving the
    # schema clean. NOT the embedded-duplicate mechanism — the dedup is
    # a no-op for this metadata; that mechanism lives in
    # test_diff.py's instrumentation tests.
    rep = push(md_indexed, pg_engine)  # must not raise DuplicateTable
    assert rep.applied
    with pg_engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_api_indexed_email'")
        ).scalar()
    assert n == 1
    assert check(md_indexed, pg_engine).clean


@pytest.fixture()
def md_conc_push(pg_engine):
    # §5.1: an EXISTING table gaining a declared index — the shape the
    # CONCURRENTLY-by-default rendering targets
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS conc_push"))
        conn.execute(text("CREATE TABLE conc_push (id INTEGER PRIMARY KEY, email VARCHAR(50))"))
    md = MetaData()
    Table(
        "conc_push",
        md,
        Column("id", Integer, primary_key=True),
        Column("email", String(50)),
        Index("ix_conc_push_email", "email"),
    )
    yield md
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS conc_push"))


def test_push_existing_table_index_concurrent_end_to_end(pg_engine, md_conc_push):
    # Live push through the DEFAULT locked path: the index op is planned
    # CONCURRENTLY (flag + SQL), applied on the autocommit segment, lands
    # VALID in pg_indexes, and the push is honest about outcomes — a
    # failing concurrent op is a partial failure while the transactional
    # segment still applies (mirror of test_executor.py's split test,
    # with the injected-plan shape: flag AND SQL both set).
    rep = push(md_conc_push, pg_engine)
    idx_applied = [a for a in rep.applied if a.type == "add_index"]
    assert idx_applied and all(a.status == "applied" for a in idx_applied)
    with pg_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT x.indisvalid FROM pg_indexes i "
                "JOIN pg_class c ON c.relname = i.indexname "
                "JOIN pg_index x ON x.indexrelid = c.oid "
                "WHERE i.schemaname = 'public' AND i.indexname = 'ix_conc_push_email'"
            )
        ).first()
    assert row is not None and row[0] is True  # exists AND not INVALID
    assert check(md_conc_push, pg_engine).clean

    plan = Plan(
        operations=(
            PlannedOperation(
                type="add_index",
                risk=RiskClass.RISKY,
                sql="CREATE INDEX CONCURRENTLY ix_cok ON conc_push (id)",
                table="conc_push",
                concurrent=True,
            ),
            PlannedOperation(
                type="add_index",
                risk=RiskClass.RISKY,
                sql="CREATE INDEX CONCURRENTLY ix_cbad ON conc_push (nope)",
                table="conc_push",
                concurrent=True,
            ),
            PlannedOperation(
                type="add_column",
                risk=RiskClass.SAFE,
                sql="ALTER TABLE conc_push ADD COLUMN a2 INT",
                table="conc_push",
                column="a2",
            ),
        )
    )
    report = apply_plan(pg_engine, plan)
    assert report.partial_failure is True
    statuses = {a.type + a.status for a in report.applied}
    assert any("failed" in s for s in statuses)
    with pg_engine.connect() as conn:
        has_col = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'conc_push' AND column_name = 'a2'"
            )
        ).first()
    assert has_col is not None  # txn segment still ran


@pytest.fixture()
def md_ht_schema():
    # F3a (push fire-test): @hypertable on a NON-default-schema table.
    # Composite PK (id, ts): timescaledb requires the partitioning column
    # in any primary key / unique constraint.
    import os

    import sqlalchemy as sa
    from sqlalchemy import create_engine

    eng = create_engine(
        os.environ.get(
            "SQLPUSH_TEST_DSN", "postgresql+psycopg://sqlpush:sqlpush@localhost:5433/sqlpush_test"
        ),
        poolclass=sa.pool.NullPool,
    )
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS api_other CASCADE"))
        conn.execute(text("CREATE SCHEMA api_other"))
    eng.dispose()

    m = MetaData()
    t = Table(
        "api_metrics_other",
        m,
        Column("id", Integer, primary_key=True),
        Column("ts", DateTime, primary_key=True),
        schema="api_other",
    )

    @hypertable(time_column="ts")
    class ApiMetricsOther:
        __table__ = t

    assert ApiMetricsOther.__table__.info["sqlpush_hypertable"]
    yield m

    eng = create_engine(
        os.environ.get(
            "SQLPUSH_TEST_DSN", "postgresql+psycopg://sqlpush:sqlpush@localhost:5433/sqlpush_test"
        ),
        poolclass=sa.pool.NullPool,
    )
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS api_other CASCADE"))
    eng.dispose()


@pytest.mark.timescale
def test_push_annotated_non_public_schema_registers(pg_engine, md_ht_schema):
    # F3a regression: create_hypertable must schema-qualify its relation —
    # unqualified, it resolved via the session search_path to
    # public.api_metrics_other and died with UndefinedTable before the
    # hypertable could register in the table's own schema.
    rep = push(md_ht_schema, pg_engine, schemas=("api_other",))
    assert any(a.type == "create_hypertable" for a in rep.applied)
    with pg_engine.connect() as conn:
        is_ht = conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM "
                "timescaledb_information.hypertables "
                "WHERE hypertable_schema = 'api_other' "
                "AND hypertable_name = 'api_metrics_other')"
            )
        ).scalar()
    assert is_ht
    # state-aware directive: the schema-qualified registration suppresses
    # the op on re-push, and check() reports clean
    rep2 = push(md_ht_schema, pg_engine, schemas=("api_other",))
    assert rep2.applied == ()
    assert check(md_ht_schema, pg_engine, schemas=("api_other",)).clean


def test_plan_push_check_roundtrip(pg_engine, md):
    p = plan(md, pg_engine)
    assert p.drift
    rep = push(md, pg_engine)
    assert rep.applied
    assert check(md, pg_engine).clean
    assert not plan(md, pg_engine).drift


def test_ensure_schema_idempotent(pg_engine, md):
    ensure_schema(md, pg_engine, mode="push")
    rep2 = ensure_schema(md, pg_engine, mode="push")
    assert rep2.applied == ()


def test_async_facade(pg_engine, md):
    import asyncio

    asyncio.run(sqlpush.aensure_schema(md, pg_engine, mode="push"))
    assert check(md, pg_engine).clean


def test_ensure_schema_async_engine(pg_engine, md):
    import asyncio
    import os

    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    dsn = os.environ.get(
        "SQLPUSH_TEST_DSN",
        "postgresql+psycopg://sqlpush:sqlpush@localhost:5433/sqlpush_test",
    )
    aengine = create_async_engine(dsn)
    try:
        asyncio.run(sqlpush.aensure_schema(md, aengine, mode="push"))
        assert inspect(pg_engine).has_table("api_hero")
    finally:
        asyncio.run(aengine.dispose())


def test_sync_engine_from_translates_asyncpg_dsn(pg_engine):
    # B2: a postgresql+asyncpg DSN string must yield a WORKING sync
    # engine on psycopg (runtime dep) — asyncpg is not installed in this
    # environment, so connecting through it proves the translation.
    from sqlalchemy.engine import make_url

    from sqlpush.api import _sync_engine_from

    asyncpg_dsn = make_url(pg_engine.url).set(drivername="postgresql+asyncpg")
    engine, dispose = _sync_engine_from(asyncpg_dsn.render_as_string(hide_password=False))
    assert dispose
    try:
        assert engine.dialect.driver == "psycopg"
        assert engine.url.drivername == "postgresql+psycopg"
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()


def test_asyncpg_url_translation_preserves_components(pg_engine):
    # An AsyncEngine over asyncpg cannot even be constructed here
    # (create_async_engine imports the driver — verified absent), so the
    # AsyncEngine-branch translation is pinned at the URL level: driver
    # swapped, every other component preserved, non-asyncpg passthrough.
    from sqlalchemy.engine import URL

    from sqlpush.api import _translate_asyncpg

    url = URL.create(
        "postgresql+asyncpg",
        username="sqlpush",
        password="sqlpush",
        host="localhost",
        port=5433,
        database="sqlpush_test",
        query={"connect_timeout": "10"},
    )
    out = _translate_asyncpg(url)
    assert out.drivername == "postgresql+psycopg"
    assert (out.username, out.password, out.host, out.port, out.database) == (
        "sqlpush",
        "sqlpush",
        "localhost",
        5433,
        "sqlpush_test",
    )
    assert dict(out.query) == {"connect_timeout": "10"}
    plain = URL.create("postgresql+psycopg", host="localhost", database="x")
    assert _translate_asyncpg(plain) is plain


def test_sync_engine_from_plain_psycopg_unchanged(pg_engine):
    # Non-asyncpg targets keep the exact pre-B2 behavior: str DSN and
    # plain-psycopg AsyncEngine still resolve to working disposable
    # psycopg engines, and a sync Engine passes through untouched.
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from sqlpush.api import _sync_engine_from

    dsn = pg_engine.url.render_as_string(hide_password=False)
    eng, dispose = _sync_engine_from(dsn)
    assert dispose and eng.url.drivername == "postgresql+psycopg"
    with eng.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
    eng.dispose()

    aeng = create_async_engine(dsn)
    try:
        eng2, dispose2 = _sync_engine_from(aeng)
        assert dispose2 and eng2.url.drivername == "postgresql+psycopg"
        with eng2.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
        eng2.dispose()
    finally:
        asyncio.run(aeng.dispose())

    eng3, dispose3 = _sync_engine_from(pg_engine)
    assert eng3 is pg_engine and not dispose3


@pytest.mark.timescale
def test_push_default_locked_path_applies_hypertable(pg_engine, md_ht):
    # Regression pin: push() defaults to lock=True, whose winner path
    # re-plans via `reverify.plan()`. That re-plan must go through the
    # plan builder (diff ops + directive ops), not the raw DiffEngine;
    # otherwise create_hypertable never runs on the DEFAULT push path.
    rep = push(md_ht, pg_engine)  # lock=True is the default
    assert any(a.type == "create_hypertable" for a in rep.applied)
    with pg_engine.connect() as conn:
        is_ht = conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM "
                "timescaledb_information.hypertables "
                "WHERE hypertable_name = 'api_metrics')"
            )
        ).scalar()
    assert is_ht


@pytest.mark.timescale
def test_push_annotated_idempotent(pg_engine, md_ht):
    # Re-push of an already-hypertable model must be a no-op: the state-
    # aware directive suppresses create_hypertable once applied.
    rep1 = push(md_ht, pg_engine)
    assert any(a.type == "create_hypertable" for a in rep1.applied)
    rep2 = push(md_ht, pg_engine)
    assert rep2.applied == ()


@pytest.mark.timescale
def test_check_clean_after_push_annotated(pg_engine, md_ht):
    # check() must report clean on a fully-synced annotated schema:
    # forever-drift would kill the CI drift gate for the flagship use
    # case.
    push(md_ht, pg_engine)
    assert check(md_ht, pg_engine).clean is True
    # must not raise on a synced annotated schema
    ensure_schema(md_ht, pg_engine, mode="check")
