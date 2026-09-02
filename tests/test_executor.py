# tests/test_executor.py
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, text

from sqlpush.apply.executor import apply_plan
from sqlpush.types import Plan, PlannedOperation, RiskClass, SqlpushError

pytestmark = pytest.mark.pg


def _col_op(table="hero", sql=None, risk=RiskClass.SAFE, type_="add_column"):
    return PlannedOperation(
        type=type_,
        risk=risk,
        sql=sql or f"ALTER TABLE {table} ADD COLUMN x INT",
        table=table,
        column="x",
    )


@pytest.fixture()
def hero_table(pg_engine):
    md = MetaData()
    Table("hero", md, Column("id", Integer, primary_key=True))
    md.create_all(pg_engine)
    yield pg_engine
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS hero"))


def _cols(engine, table="hero"):
    import sqlalchemy as sa

    insp = sa.inspect(engine)
    return {c["name"] for c in insp.get_columns(table)}


def test_atomic_apply(hero_table):
    plan = Plan(operations=(_col_op(sql="ALTER TABLE hero ADD COLUMN a INT"),))
    report = apply_plan(hero_table, plan)
    assert "a" in _cols(hero_table)
    assert report.applied and report.applied[0].status == "applied"


def test_destructive_blocked_without_flag(hero_table):
    plan = Plan(
        operations=(
            _col_op(sql="ALTER TABLE hero ADD COLUMN a INT"),
            PlannedOperation(
                type="drop_column",
                risk=RiskClass.DESTRUCTIVE,
                sql="ALTER TABLE hero DROP COLUMN id",
                table="hero",
                column="id",
            ),
        )
    )
    report = apply_plan(hero_table, plan)
    assert report.applied == ()
    assert len(report.blocked) == 1
    assert "a" not in _cols(hero_table)  # nothing ran at all


def test_destructive_with_flag(hero_table):
    plan = Plan(
        operations=(
            PlannedOperation(
                type="drop_column",
                risk=RiskClass.DESTRUCTIVE,
                sql="ALTER TABLE hero DROP COLUMN id",
                table="hero",
                column="id",
            ),
        )
    )
    apply_plan(hero_table, plan, allow_destructive=True)
    assert "id" not in _cols(hero_table)


def test_safe_only_skips_risky(hero_table):
    plan = Plan(
        operations=(
            _col_op(),
            PlannedOperation(
                type="add_index",
                risk=RiskClass.RISKY,
                sql="CREATE INDEX CONCURRENTLY ix_hero_x ON hero (x)",
                table="hero",
            ),
        )
    )
    report = apply_plan(hero_table, plan, safe_only=True)
    assert len(report.applied) == 1
    assert "x" in _cols(hero_table)  # SAFE op still ran
    assert [op.type for op in report.skipped] == ["add_index"]
    assert all(a.type != "add_index" for a in report.applied)


def test_failure_in_txn_rolls_back_everything(hero_table):
    plan = Plan(
        operations=(
            _col_op(sql="ALTER TABLE hero ADD COLUMN a INT"),
            _col_op(sql="ALTER TABLE hero ADD COLUMN b DOESNOTEXIST"),
        )
    )
    with pytest.raises(SqlpushError):
        apply_plan(hero_table, plan)
    assert "a" not in _cols(hero_table)  # atomic: nothing persisted


def test_concurrently_split_and_partial_failure(hero_table):
    plan = Plan(
        operations=(
            PlannedOperation(
                type="add_index",
                risk=RiskClass.RISKY,
                sql="CREATE INDEX CONCURRENTLY ix_ok ON hero (id)",
                table="hero",
            ),
            PlannedOperation(
                type="add_index",
                risk=RiskClass.RISKY,
                sql="CREATE INDEX CONCURRENTLY ix_bad ON hero (nope)",
                table="hero",
            ),
            _col_op(sql="ALTER TABLE hero ADD COLUMN a INT"),
        )
    )
    report = apply_plan(hero_table, plan)  # index failure is partial, not fatal
    assert report.partial_failure is True
    statuses = {a.type + a.status for a in report.applied}
    assert any("failed" in s for s in statuses)
    assert "a" in _cols(hero_table)  # txn segment still ran


def test_executor_detection_flag_union(hero_table):
    # A5: detection is op.concurrent OR the SQL substring. The flag is
    # authoritative for generated plans; the substring keeps hand-built
    # Plans (no flag, pre-0.5 SQL) splitting to the autocommit segment.
    # (a) unflagged op with CONCURRENTLY SQL: routes to the concurrent
    # segment — executed on autocommit it SUCCEEDS (in a txn it would
    # die with "cannot run inside a transaction block" and raise).
    plan = Plan(
        operations=(
            PlannedOperation(  # no flag on purpose (default False)
                type="add_index",
                risk=RiskClass.RISKY,
                sql="CREATE INDEX CONCURRENTLY ix_flag_u ON hero (id)",
                table="hero",
            ),
            _col_op(sql="ALTER TABLE hero ADD COLUMN a INT"),
        )
    )
    report = apply_plan(hero_table, plan)
    assert report.partial_failure is False
    with hero_table.connect() as conn:
        ok = conn.execute(text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_flag_u'")).scalar()
    assert ok is not None
    assert "a" in _cols(hero_table)

    # (b) flagged op WITHOUT CONCURRENTLY SQL: routes to the concurrent
    # segment too — a failing op there is a recorded partial failure,
    # whereas the txn segment would raise SqlpushError (rollback).
    plan2 = Plan(
        operations=(
            PlannedOperation(
                type="add_index",
                risk=RiskClass.RISKY,
                sql="CREATE INDEX ix_flag_bad ON hero (nope)",
                table="hero",
                concurrent=True,
            ),
        )
    )
    report2 = apply_plan(hero_table, plan2)
    assert report2.partial_failure is True
    assert [a.status for a in report2.applied] == ["failed"]


def _pooled(engine):
    # a real QueuePool with exactly ONE underlying connection: whatever
    # session state apply_plan leaves behind is handed, not recreated,
    # to the next borrower — the 0.4.2 pooled-GUC-leak observation
    dsn = engine.url.render_as_string(hide_password=False)
    return create_engine(dsn, pool_size=1, max_overflow=0)


def test_concurrent_segment_lock_timeout_set_and_reset(pg_engine, hero_table):
    # A7: the autocommit (CONCURRENTLY) segment now runs under a session
    # lock_timeout — an external ACCESS EXCLUSIVE holder makes the index
    # op fail WITHIN the budget (partial failure, no unbounded queue) —
    # and the RESET returns the pooled connection to the server default.
    holder = pg_engine.connect()
    pooled = _pooled(pg_engine)
    plan = Plan(
        operations=(
            PlannedOperation(
                type="add_index",
                risk=RiskClass.RISKY,
                sql="CREATE INDEX CONCURRENTLY ix_lt ON hero (id)",
                table="hero",
                concurrent=True,
            ),
        )
    )
    try:
        with pooled.connect() as probe:
            default = probe.execute(text("SHOW lock_timeout")).scalar()
        holder.execute(text("LOCK TABLE hero IN ACCESS EXCLUSIVE MODE"))
        report = apply_plan(pooled, plan, lock_timeout=1.0)
        assert report.partial_failure is True  # failed, did not hang
        assert report.duration < 10.0  # bounded by the ~1s budget
        # RESET check on the SAME pool (pool_size=1 => same session):
        # without it this borrower inherits lock_timeout='1s'
        with pooled.connect() as probe:
            assert probe.execute(text("SHOW lock_timeout")).scalar() == default
    finally:
        holder.rollback()
        holder.close()
    # holder released: the same op through the same pooled engine works
    try:
        report2 = apply_plan(pooled, plan)
        assert report2.partial_failure is False
        with pg_engine.connect() as conn:
            ok = conn.execute(text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_lt'")).scalar()
        assert ok is not None
    finally:
        pooled.dispose()


def test_statement_timeout_txn_local_and_session_reset(pg_engine):
    # B12: a not-None statement_timeout reaches BOTH segments — SET LOCAL
    # on the transactional one, session SET (then RESET) on the
    # autocommit one — observable via current_setting() inside each
    # segment's execution window; None touches no GUC anywhere.
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS st_probe"))
        conn.execute(text("CREATE TABLE st_probe (seg text, val text)"))
    pooled = _pooled(pg_engine)
    try:
        with pooled.connect() as probe:
            default_st = probe.execute(text("SHOW statement_timeout")).scalar()

        def _probe(seg, concurrent=False):
            return PlannedOperation(
                type="raw_sql",
                risk=RiskClass.SAFE,
                sql=f"INSERT INTO st_probe VALUES ('{seg}', current_setting('statement_timeout'))",
                table="st_probe",
                concurrent=concurrent,
            )

        plan = Plan(operations=(_probe("txn"), _probe("conc", concurrent=True)))
        report = apply_plan(pooled, plan, statement_timeout=2.5)
        assert report.partial_failure is False
        with pooled.connect() as conn:
            rows = dict(conn.execute(text("SELECT seg, val FROM st_probe")).all())
            # both segments saw the injected budget
            assert rows == {"txn": "2500ms", "conc": "2500ms"}
            # session RESET: the pooled borrower is back at the default
            assert conn.execute(text("SHOW statement_timeout")).scalar() == default_st

        # None → no GUC touched on either segment
        conn = pooled.connect()
        conn.execute(text("TRUNCATE st_probe"))
        conn.close()
        report2 = apply_plan(pooled, plan, statement_timeout=None)
        assert report2.partial_failure is False
        with pooled.connect() as conn:
            rows2 = dict(conn.execute(text("SELECT seg, val FROM st_probe")).all())
            assert rows2 == {"txn": default_st, "conc": default_st}
            assert conn.execute(text("SHOW statement_timeout")).scalar() == default_st
    finally:
        pooled.dispose()
        with pg_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS st_probe"))


def test_apply_plan_rejects_negative_statement_timeout(hero_table):
    # B12 validation mirrors the 0.4.2 lock_timeout budget pattern:
    # negative refuses up front, typed, before anything executes
    with pytest.raises(SqlpushError, match=">= 0"):
        apply_plan(hero_table, Plan(), statement_timeout=-1)
