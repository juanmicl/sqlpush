from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, text

from sqlpush.api import revision
from sqlpush.chain.format import parse_migration_file
from sqlpush.types import SqlpushError

pytestmark = pytest.mark.pg


@pytest.fixture()
def clean_db(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS revt"))
        conn.execute(text("DROP TABLE IF EXISTS doomed"))
    yield pg_engine
    # teardown: revision() only plans (never applies), but a test that CREATEs
    # a table itself (doomed) must not leak it into the session-scoped DB —
    # other modules' plans would see phantom db-only drift
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS revt"))
        conn.execute(text("DROP TABLE IF EXISTS doomed"))


def _md_with_table() -> MetaData:
    md = MetaData()
    Table("revt", md, Column("id", Integer, primary_key=True), schema=None)
    return md


def test_revision_writes_parsed_clean_file(clean_db, tmp_path):
    out = revision(_md_with_table(), clean_db, out_dir=tmp_path, message="add revt")
    assert out.exists() and out.suffix == ".sql"
    mf = parse_migration_file(out.read_text(), name=out.name)
    assert mf.revision_id == "0001"
    assert any("revt" in sql for _, sql in mf.ops)


def test_revision_empty_plan_refuses(clean_db, tmp_path):
    # metadata vacía vs BD vacía = sin drift
    with pytest.raises(SqlpushError, match="nothing to revise"):
        revision(MetaData(), clean_db, out_dir=tmp_path, message="empty")


def test_revision_gap_free_and_scoped(clean_db, tmp_path):
    (tmp_path / "0007_prev.sql").write_text("-- sqlpush: revision=0007 risk=SAFE\nSELECT 1;")
    out = revision(_md_with_table(), clean_db, out_dir=tmp_path, message="next")
    assert out.name.startswith("0008_")


def test_revision_header_risk_is_max(clean_db, tmp_path):
    # drop de tabla existente = DESTRUCTIVE en el header
    with clean_db.begin() as conn:
        conn.execute(text("CREATE TABLE doomed (id integer PRIMARY KEY)"))
    out = revision(MetaData(), clean_db, out_dir=tmp_path, message="drop doomed")
    assert "risk=DESTRUCTIVE" in out.read_text()
