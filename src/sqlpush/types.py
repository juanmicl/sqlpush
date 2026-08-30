# src/sqlpush/types.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class RiskClass(str, Enum):
    SAFE = "safe"
    RISKY = "risky"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class PlannedOperation:
    type: str
    risk: RiskClass
    sql: str
    table: str | None = None
    column: str | None = None


@dataclass(frozen=True)
class Plan:
    operations: tuple[PlannedOperation, ...] = ()

    @property
    def drift(self) -> bool:
        return bool(self.operations)

    @property
    def has_destructive(self) -> bool:
        return any(op.risk is RiskClass.DESTRUCTIVE for op in self.operations)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "drift": self.drift,
            "operations": [
                {
                    "type": op.type,
                    "risk": op.risk.value,
                    "table": op.table,
                    "column": op.column,
                    "sql": op.sql,
                }
                for op in self.operations
            ],
            "sql": ";\n".join(op.sql for op in self.operations),
        }


@dataclass(frozen=True)
class CheckResult:
    clean: bool
    drift: bool
    has_destructive: bool


@dataclass(frozen=True)
class AppliedOperation:
    type: str
    # execution outcome only: "applied" | "failed". Blocked operations
    # are refused by the gate before execution and live in
    # Report.blocked instead.
    status: str


@dataclass(frozen=True)
class Report:
    # `applied` is the EXECUTION TRAIL of the run: it may contain
    # "failed" entries (a CONCURRENTLY op that errored, see
    # partial_failure), not only successes.
    applied: tuple[AppliedOperation, ...] = ()
    # `blocked`: destructive ops refused by the gate (allow_destructive=False)
    # Hard stop, CLI maps this to exit 1.
    # `skipped`: ops declined by policy (safe_only), informational only,
    # the run proceeds without them.
    blocked: tuple[PlannedOperation, ...] = ()
    skipped: tuple[PlannedOperation, ...] = ()
    partial_failure: bool = False
    duration: float = 0.0


class SqlpushError(Exception):
    pass


class ConnectFailed(SqlpushError):
    pass


class MetadataImportError(SqlpushError):
    pass


class DestructiveBlocked(SqlpushError):
    """Reserved for future use: the destructive gate currently reports
    through ``Report.blocked`` instead of raising."""
