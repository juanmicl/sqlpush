# tests/test_types.py
from sqlpush.types import (
    CheckResult,
    DestructiveBlocked,
    Plan,
    PlannedOperation,
    RiskClass,
)


def _op(risk=RiskClass.SAFE, **kw):
    return PlannedOperation(
        type=kw.get("type", "add_column"),
        risk=risk,
        sql=kw.get("sql", "ALTER TABLE t ADD COLUMN c INT"),
        table=kw.get("table", "t"),
        column=kw.get("column", "c"),
    )


def test_plan_drift_and_destructive():
    p = Plan(operations=(_op(), _op(RiskClass.DESTRUCTIVE, type="drop_column")))
    assert p.drift is True
    assert p.has_destructive is True


def test_plan_clean():
    p = Plan(operations=())
    assert p.drift is False
    assert p.has_destructive is False


def test_json_contract_v1_shape():
    d = Plan(operations=(_op(),)).to_json_dict()
    assert d["version"] == 1
    assert d["drift"] is True
    op = d["operations"][0]
    assert set(op) == {"type", "risk", "table", "column", "sql", "concurrent"}
    assert op["risk"] == "safe"
    assert op["concurrent"] is False


def test_planned_operation_concurrent_defaults_false():
    # additive field: every pre-existing construction site (tests,
    # hand-built plans) keeps working unchanged; injection flips it
    # via dataclasses.replace on exactly the ops it rewrites
    op = PlannedOperation(
        type="add_index", risk=RiskClass.RISKY, sql="CREATE INDEX ix ON t (c)", table="t"
    )
    assert op.concurrent is False


def test_exception_hierarchy():
    assert issubclass(DestructiveBlocked, Exception)
    r = CheckResult(clean=True, drift=False, has_destructive=False)
    assert r.clean and not r.drift
