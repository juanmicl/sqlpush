# src/sqlpush/core/classify.py
from __future__ import annotations

from sqlpush.types import RiskClass

SAFE = frozenset({"add_table", "add_column", "add_constraint", "create_hypertable"})
DESTRUCTIVE = frozenset({"drop_column", "drop_table", "drop_index", "drop_constraint"})


def classify(op_type: str) -> RiskClass:
    """Standalone add_index targets an EXISTING table
    (indexes of new tables ride along with add_table), so it is risky:
    a plain CREATE INDEX takes a SHARE lock that blocks writes."""
    if op_type in DESTRUCTIVE:
        return RiskClass.DESTRUCTIVE
    if op_type in SAFE:
        return RiskClass.SAFE
    # modify_*, add_index, raw_sql, and anything unknown
    return RiskClass.RISKY
