# src/sqlpush/core/classify.py
from __future__ import annotations

from sqlpush.types import RiskClass

SAFE = frozenset({"add_table", "add_column", "add_constraint", "create_hypertable"})
DESTRUCTIVE = frozenset({"drop_column", "drop_table", "drop_index", "drop_constraint"})


def classify(op_type: str) -> RiskClass:
    """add_index renders standalone: on alembic 1.19.1 even plain
    declared indexes of NEW tables arrive standalone
    (CreateTableOp.from_table captures columns+constraints, not
    indexes; only instrumentation-embedded ones ride inside the
    add_table render, and the diff dedups those away). What survives
    runs CREATE INDEX alone — a SHARE lock that blocks writes, hence
    risky."""
    if op_type in DESTRUCTIVE:
        return RiskClass.DESTRUCTIVE
    if op_type in SAFE:
        return RiskClass.SAFE
    # modify_*, add_index, raw_sql, and anything unknown
    return RiskClass.RISKY
