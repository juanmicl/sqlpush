# tests/test_contract.py
import json
from pathlib import Path

import jsonschema

from sqlpush.types import Plan, PlannedOperation, RiskClass

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["version", "drift", "operations", "sql"],
    "properties": {
        "version": {"const": 1},
        "drift": {"type": "boolean"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "risk", "table", "column", "sql"],
                "properties": {
                    "type": {"type": "string"},
                    "risk": {"enum": ["safe", "risky", "destructive"]},
                    "table": {"type": ["string", "null"]},
                    "column": {"type": ["string", "null"]},
                    "sql": {"type": "string"},
                },
            },
        },
        "sql": {"type": "string"},
    },
}


def _sample() -> Plan:
    return Plan(
        operations=(
            PlannedOperation(
                type="add_column",
                risk=RiskClass.SAFE,
                sql="ALTER TABLE t ADD COLUMN a INT",
                table="t",
                column="a",
            ),
        )
    )


def test_contract_valid_and_golden():
    payload = _sample().to_json_dict()
    jsonschema.validate(payload, SCHEMA)
    golden = Path(__file__).parent / "golden" / "plan_v1.json"
    if not golden.exists():
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(json.dumps(payload, indent=2) + "\n")
    assert json.loads(golden.read_text()) == payload
