# tests/test_executor.py
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, text

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
