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
