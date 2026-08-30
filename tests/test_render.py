# tests/test_render.py
from sqlpush.core.render import render
from sqlpush.types import Plan, PlannedOperation, RiskClass


def test_render_groups_by_risk():
    plan = Plan(
        operations=(
            PlannedOperation(
                type="add_column",
                risk=RiskClass.SAFE,
                sql="ALTER TABLE t ADD COLUMN a INT",
                table="t",
                column="a",
            ),
            PlannedOperation(
                type="drop_column",
                risk=RiskClass.DESTRUCTIVE,
                sql="ALTER TABLE t DROP COLUMN b",
                table="t",
                column="b",
            ),
        )
    )
    out = render(plan)
    assert "-- safe" in out and "-- destructive" in out
    assert "ADD COLUMN a INT" in out and "DROP COLUMN b" in out


def test_render_empty_plan():
    assert render(Plan()) == ""
