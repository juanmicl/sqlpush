# tests/test_classify.py
import pytest

from sqlpush.core.classify import classify
from sqlpush.types import RiskClass


@pytest.mark.parametrize(
    ("op_type", "expected"),
    [
        ("add_table", RiskClass.SAFE),
        ("add_column", RiskClass.SAFE),
        ("add_constraint", RiskClass.SAFE),
        ("create_hypertable", RiskClass.SAFE),
        # index on an existing table: SHARE lock blocks writes
        ("add_index", RiskClass.RISKY),
        ("modify_type", RiskClass.RISKY),
        ("modify_nullable", RiskClass.RISKY),
        ("modify_default", RiskClass.RISKY),
        ("raw_sql", RiskClass.RISKY),
        ("drop_column", RiskClass.DESTRUCTIVE),
        ("drop_table", RiskClass.DESTRUCTIVE),
        ("drop_index", RiskClass.DESTRUCTIVE),
        ("drop_constraint", RiskClass.DESTRUCTIVE),
    ],
)
def test_classification_map(op_type, expected):
    assert classify(op_type) is expected


def test_unknown_op_is_risky():
    assert classify("modify_comment") is RiskClass.RISKY
