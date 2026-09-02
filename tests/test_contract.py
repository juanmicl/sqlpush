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
                "required": ["type", "risk", "table", "column", "sql", "concurrent"],
                "properties": {
                    "type": {"type": "string"},
                    "risk": {"enum": ["safe", "risky", "destructive"]},
                    "table": {"type": ["string", "null"]},
                    "column": {"type": ["string", "null"]},
                    "sql": {"type": "string"},
                    "concurrent": {"type": "boolean"},
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


def test_json_contract_v1_additive_concurrent():
    # A5: `concurrent` is ADDITIVE to JSON v1 — every operation carries
    # it (a boolean, never null), and the pre-existing keys keep their
    # exact values (byte-stable against the golden).
    payload = _sample().to_json_dict()
    op = payload["operations"][0]
    assert op["concurrent"] is False
    jsonschema.validate(payload, SCHEMA)
    golden = json.loads((Path(__file__).parent / "golden" / "plan_v1.json").read_text())
    assert golden["operations"][0]["concurrent"] is False
    # the five v1 keys are untouched: same keys, same values
    for key in ("type", "risk", "table", "column", "sql"):
        assert op[key] == golden["operations"][0][key]
    # a CONCURRENTLY-rendered op reports true
    from sqlpush.types import Plan as _Plan

    flagged = _Plan(
        operations=(
            PlannedOperation(
                type="add_index",
                risk=RiskClass.RISKY,
                sql="CREATE INDEX CONCURRENTLY ix ON t (c)",
                table="t",
                concurrent=True,
            ),
        )
    ).to_json_dict()
    assert flagged["operations"][0]["concurrent"] is True
    jsonschema.validate(flagged, SCHEMA)
