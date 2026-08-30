from typing import TYPE_CHECKING, Any

from sqlpush.types import (
    AppliedOperation,
    CheckResult,
    ConnectFailed,
    DestructiveBlocked,
    MetadataImportError,
    Plan,
    PlannedOperation,
    Report,
    RiskClass,
    SqlpushError,
)

# Static-checker visibility for the PEP 562 lazy exports below: this
# block never executes at runtime, so the light-import guarantee is
# untouched (tests/test_annotations.py still guards it).
if TYPE_CHECKING:
    from sqlpush.api import (
        acheck,
        aensure_schema,
        aplan,
        apush,
        check,
        ensure_schema,
        plan,
        push,
    )

__version__ = "0.1.0"

# Public API (plan/push/check/ensure_schema + async facade) is exported
# lazily via PEP 562: an eager import would pull alembic (through
# sqlpush.core.diff) into every `import sqlpush.*`, breaking the
# light-import guarantee of the annotations module (see
# tests/test_annotations.py::test_annotations_module_has_no_heavy_imports).
_LAZY_API = (
    "plan",
    "push",
    "check",
    "ensure_schema",
    "aplan",
    "apush",
    "acheck",
    "aensure_schema",
)


def __getattr__(name: str) -> Any:
    if name in _LAZY_API:
        from sqlpush import api

        return getattr(api, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AppliedOperation",
    "CheckResult",
    "ConnectFailed",
    "DestructiveBlocked",
    "MetadataImportError",
    "Plan",
    "PlannedOperation",
    "Report",
    "RiskClass",
    "SqlpushError",
    "__version__",
    "acheck",
    "aensure_schema",
    "aplan",
    "apush",
    "check",
    "ensure_schema",
    "plan",
    "push",
]
