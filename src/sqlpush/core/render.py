# src/sqlpush/core/render.py
from __future__ import annotations

from sqlpush.types import Plan, RiskClass

_HEADERS = {
    RiskClass.SAFE: "-- safe",
    RiskClass.RISKY: "-- risky",
    RiskClass.DESTRUCTIVE: "-- destructive",
}


def render(plan: Plan) -> str:
    if not plan.operations:
        return ""
    sections: list[str] = []
    for risk in (RiskClass.SAFE, RiskClass.RISKY, RiskClass.DESTRUCTIVE):
        sqls = [op.sql for op in plan.operations if op.risk is risk]
        if sqls:
            sections.append(_HEADERS[risk] + "\n" + ";\n".join(sqls) + ";")
    return "\n\n".join(sections)
