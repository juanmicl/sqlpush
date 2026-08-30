from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, text

from sqlpush.core.diff import DiffEngine
from sqlpush.types import RiskClass

pytestmark = pytest.mark.pg


@pytest.fixture()
def clean_db(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS hero"))
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        conn.execute(text("DROP SCHEMA IF EXISTS other CASCADE"))
    return pg_engine


@pytest.fixture()
def other_schema(clean_db):
    with clean_db.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS other"))
    yield clean_db
    with clean_db.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS other CASCADE"))


def _md() -> MetaData:
    md = MetaData()
    Table("hero", md, Column("id", Integer, primary_key=True), Column("name", String(50)))
    return md


def test_clean_when_in_sync(clean_db):
    _md().create_all(clean_db)
    plan = DiffEngine().plan(_md(), clean_db)
    assert plan.drift is False


def test_add_column_detected(clean_db):
    _md().create_all(clean_db)
    # rebuild with the extra column properly
    md3 = MetaData()
    Table(
        "hero",
        md3,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        Column("age", Integer, nullable=True),
    )
    plan = DiffEngine().plan(md3, clean_db)
    assert plan.drift is True
    assert any(op.type == "add_column" and op.risk is RiskClass.SAFE for op in plan.operations)


def test_drop_column_gated_destructive(clean_db):
    _md().create_all(clean_db)
    plan = DiffEngine().plan(_minimal_without_name(), clean_db)
    # DropColumnOp has no `.column` attribute; the plain-string
    # column_name extraction path must still yield the column
    assert any(op.risk is RiskClass.DESTRUCTIVE and op.column == "name" for op in plan.operations)


def _minimal_without_name() -> MetaData:
    md = MetaData()
    Table("hero", md, Column("id", Integer, primary_key=True))
    return md


def test_server_default_drift_detected(clean_db):
    md = MetaData()
    Table(
        "hero",
        md,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), server_default=text("'anon'")),
    )
    md.create_all(clean_db)
    md2 = MetaData()
    Table("hero", md2, Column("id", Integer, primary_key=True), Column("name", String(50)))
    plan = DiffEngine().plan(md2, clean_db)
    assert plan.drift is True  # default removal visible, drift contract


def test_alembic_version_and_system_schemas_ignored(clean_db):
    with clean_db.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
    plan = DiffEngine().plan(_md(), clean_db)
    # hero-create is the only expected op, which proves no catalog leakage
    assert plan.operations and all(op.table == "hero" for op in plan.operations)


def test_exclude_pattern(clean_db):
    _md().create_all(clean_db)
    md3 = MetaData()
    Table(
        "hero",
        md3,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        Column("age", Integer),
    )
    plan = DiffEngine().plan(md3, clean_db, exclude=("hero.age",))
    # hero.age was the only drift; excluding it empties the plan
    assert plan.operations == ()


def test_exclude_table_pattern_reaches_column_ops(clean_db):
    _md().create_all(clean_db)
    md3 = MetaData()
    Table(
        "hero",
        md3,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        Column("age", Integer),
    )
    plan = DiffEngine().plan(md3, clean_db, exclude=("hero",))
    assert plan.operations == ()


def test_metadata_table_outside_target_schemas_ignored(other_schema):
    md = MetaData()
    Table(
        "hero",
        md,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        schema="other",
    )
    # default scope is the search path (public): a table declared under
    # schema "other" must not be planned: neither create nor drop
    plan = DiffEngine().plan(md, other_schema)
    assert plan.operations == ()


def test_column_drift_in_non_default_schema_planned(other_schema):
    md = MetaData()
    Table("hero", md, Column("id", Integer, primary_key=True), schema="other")
    md.create_all(other_schema)
    md3 = MetaData()
    Table(
        "hero", md3, Column("id", Integer, primary_key=True), Column("age", Integer), schema="other"
    )
    # scope excludes the dialect default: column drift on a table inside
    # the scoped schema must still be planned (child objects resolve
    # their schema through the parent table)
    plan = DiffEngine().plan(md3, other_schema, schemas=("other",))
    assert plan.drift is True
    assert any(op.type == "add_column" and op.column == "age" for op in plan.operations)
