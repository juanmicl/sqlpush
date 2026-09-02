# tests/test_validation.py: DB-free tests for the API exception
# boundary and input validation (final-review items C1/I4/I5/I6).
from __future__ import annotations

from typing import cast

import pytest
from alembic.operations.ops import AlterColumnOp
from sqlalchemy import DefaultClause, MetaData, String, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from sqlpush import api
from sqlpush.apply.executor import with_advisory_lock
from sqlpush.core.diff import DiffEngine
from sqlpush.types import ConnectFailed, RiskClass, SqlpushError

UNREACHABLE = "postgresql+psycopg://u:p@127.0.0.1:5999/x?connect_timeout=2"

# --- typed exception boundary at the API surface --------------------------


def test_plan_wraps_connect_failure_as_connect_failed():
    engine = create_engine(UNREACHABLE, poolclass=NullPool)
    with pytest.raises(ConnectFailed) as ei:
        api.plan(MetaData(), engine)
    # chained from the original SQLAlchemy error, not swallowing it
    assert isinstance(ei.value.__cause__, OperationalError)


def test_push_wraps_connect_failure_as_connect_failed():
    engine = create_engine(UNREACHABLE, poolclass=NullPool)
    with pytest.raises(ConnectFailed):
        api.push(MetaData(), engine)


def test_check_wraps_connect_failure_as_connect_failed():
    engine = create_engine(UNREACHABLE, poolclass=NullPool)
    with pytest.raises(ConnectFailed):
        api.check(MetaData(), engine)


# --- I5: ensure_schema mode validation -------------------------------------


def test_ensure_schema_rejects_unknown_mode():
    # a typo'd mode must fail fast as SqlpushError, never fall through
    # to the writing branch (and never as a connect failure: validation
    # precedes engine creation)
    with pytest.raises(SqlpushError) as ei:
        api.ensure_schema(MetaData(), UNREACHABLE, mode="pushh")
    assert not isinstance(ei.value, ConnectFailed)
    assert "mode" in str(ei.value)


# --- I4: advisory-lock input validation (validation runs before any
# connection attempt, so these are DB-free) ---------------------------------


def _lazy_engine():
    return create_engine(UNREACHABLE, poolclass=NullPool)


def test_with_advisory_lock_requires_reverify():
    with pytest.raises(SqlpushError, match="reverify"):
        with_advisory_lock(_lazy_engine(), MetaData(), reverify=None)


@pytest.mark.parametrize("kwargs", [{"wait": -1}, {"timeout": -1}])
def test_with_advisory_lock_rejects_negative_budgets(kwargs):
    with pytest.raises(SqlpushError, match=">= 0"):
        # cast: the dummy must never be used: validation has to reject
        # negative budgets before reverify.plan() is ever touched
        with_advisory_lock(
            _lazy_engine(), MetaData(), reverify=cast(DiffEngine, object()), **kwargs
        )


# --- B3 (0.4.2): migrate advisory-wait validation (validation runs
# before any connection attempt, so DB-free like I4) ------------------------


def test_migrate_rejects_negative_advisory_wait(tmp_path):
    with pytest.raises(SqlpushError, match=">= 0"):
        api.migrate(_lazy_engine(), chain_dir=tmp_path, advisory_wait=-1)


# --- I6: AlterColumnOp disambiguation (sentinel semantics per
# docs/notes/alembic-notes.md Pattern C: False/None = do not touch) ---------


def test_alter_column_op_disambiguation():
    engine = _lazy_engine()  # dialect only, never connects
    cases = [
        # default-only change: modify_server_default set to a value
        (
            AlterColumnOp(
                "t", "c", existing_type=String(10), modify_server_default=DefaultClause(text("'g'"))
            ),
            "modify_default",
        ),
        # nullable-only change: modify_nullable set to a value
        (
            AlterColumnOp(
                "t", "c", existing_type=String(10), existing_nullable=False, modify_nullable=True
            ),
            "modify_nullable",
        ),
        # type change (the historical catch-all label)
        (AlterColumnOp("t", "c", existing_type=String(10), modify_type=String(20)), "modify_type"),
        # sentinel False == None ("do not touch"): must not steal the
        # label from a real modify_type
        (
            AlterColumnOp(
                "t",
                "c",
                existing_type=String(10),
                modify_type=String(20),
                modify_server_default=False,
                modify_nullable=False,
            ),
            "modify_type",
        ),
    ]
    for op, expected in cases:
        (po,) = DiffEngine()._translate(op, engine, ())
        assert po.type == expected
        assert po.risk is RiskClass.RISKY
