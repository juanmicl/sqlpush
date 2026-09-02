# tests/test_enum_dedup.py
from __future__ import annotations

import pytest
from sqlalchemy import Column, Enum, Integer, MetaData, Table, text

from sqlpush.api import migrate, plan, revision

pytestmark = pytest.mark.pg

# S2 (cycle-6 switchover): two tables sharing a native enum each embed a
# verbatim CREATE TYPE in their add_table render — executing both dies with
# DuplicateObject. Same defect family as F1/F2 (embedded-index dedup), but
# statement-level and cross-table.

_DIRTY_TABLES = ("enum_t1", "enum_t2")
_ENUM_NAME = "shared_enum_type"


@pytest.fixture()
def enum_db(pg_engine):
    def _clean() -> None:
        with pg_engine.begin() as conn:
            for t in _DIRTY_TABLES:
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
            conn.execute(text(f"DROP TYPE IF EXISTS {_ENUM_NAME}"))
            conn.execute(text("DROP TABLE IF EXISTS sqlpush_versions"))

    _clean()
    yield pg_engine
    _clean()


def _shared_enum_md() -> MetaData:
    shared = Enum("A", "B", name=_ENUM_NAME)  # native_enum=True (default)
    md = MetaData()
    Table("enum_t1", md, Column("id", Integer, primary_key=True), Column("v", shared))
    Table("enum_t2", md, Column("id", Integer, primary_key=True), Column("w", shared))
    return md


def _create_type_count(sql_text: str) -> int:
    return sql_text.count(f"CREATE TYPE {_ENUM_NAME}")


def test_plan_renders_shared_enum_create_type_once(enum_db):
    # Plan level: across ALL ops, the enum's CREATE TYPE appears exactly
    # once (first occurrence keeps it embedded in its add_table render).
    p = plan(_shared_enum_md(), enum_db)
    assert _create_type_count("".join(op.sql for op in p.operations)) == 1


def test_migrate_shared_enum_succeeds_single_type(enum_db, tmp_path):
    """End-to-end S2 repro: revision → migrate on a fresh DB must succeed
    (was DuplicateObject) and leave exactly ONE pg_type row for the enum."""
    out = revision(_shared_enum_md(), enum_db, out_dir=tmp_path, message="shared enum")
    body = out.read_text()
    assert _create_type_count(body) == 1
    rep = migrate(enum_db, chain_dir=tmp_path)
    assert rep.applied == (out.name,), rep
    assert not rep.blocked and not rep.partial_failure, rep
    with enum_db.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM pg_type WHERE typname = :n"), {"n": _ENUM_NAME}
        ).scalar()
        tables = conn.execute(
            text(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname='public' AND tablename IN ('enum_t1','enum_t2')"
            )
        ).scalar()
    assert n == 1  # exactly one type, not one per table
    assert tables == 2
