from __future__ import annotations

import pytest

from sqlpush.chain.format import (
    RISK_RANK,
    MigrationFileError,
    checksum,
    next_revision_id,
    parse_migration_file,
    render_migration_file,
)
from sqlpush.types import RiskClass


def test_render_then_parse_roundtrip(tmp_path):
    text = render_migration_file(
        ops=[("[SAFE] add_table t1", "CREATE TABLE t1 (id integer PRIMARY KEY);")],
        revision_id="0003",
        risk=RiskClass.SAFE,
        message="add t1",
    )
    (tmp_path / "0003_add_t1.sql").write_text(text)
    mf = parse_migration_file(text, name="0003_add_t1.sql")
    assert mf.revision_id == "0003"
    assert mf.risk == RiskClass.SAFE
    assert mf.ops[0][1] == "CREATE TABLE t1 (id integer PRIMARY KEY);"


def test_parse_fail_loud_missing_header():
    with pytest.raises(MigrationFileError, match="missing -- sqlpush: header"):
        parse_migration_file("CREATE TABLE t1 (id int);", name="x.sql")


def test_parse_fail_loud_malformed_header():
    with pytest.raises(MigrationFileError, match="malformed"):
        parse_migration_file("-- sqlpush: revision=\nSELECT 1;", name="x.sql")


def test_parse_fail_loud_missing_risk():
    with pytest.raises(MigrationFileError, match="risk"):
        parse_migration_file("-- sqlpush: revision=0002\nSELECT 1;", name="x.sql")


def test_parse_handwritten_minimal_header():
    mf = parse_migration_file(
        "-- sqlpush: revision=0008 risk=SAFE\nUPDATE t SET a = 1;\n",
        name="0008_hand.sql",
    )
    assert mf.risk is RiskClass.SAFE
    assert mf.ops == [("", "UPDATE t SET a = 1;")]


def test_parse_parent_and_generated_ignorable():
    text = render_migration_file(
        ops=[("[RISKY] modify_type t.n", "ALTER TABLE t ALTER COLUMN n TYPE bigint;")],
        revision_id="0004",
        risk=RiskClass.RISKY,
    )
    assert "parent=" in text  # el writer lo emite
    mf = parse_migration_file(text, name="0004.sql")
    assert mf.revision_id == "0004"  # y el parser no lo exige


def test_checksum_normalizes_crlf_and_trailing():
    a = "SELECT 1;\n"
    b = "SELECT 1;\r\n   "
    assert checksum(a) == checksum(b)
    assert checksum(a) != checksum("SELECT 2;\n")


def test_risk_rank_ordering():
    assert RISK_RANK[RiskClass.SAFE] < RISK_RANK[RiskClass.RISKY] < RISK_RANK[RiskClass.DESTRUCTIVE]


def test_next_revision_id_gap_free(tmp_path):
    (tmp_path / "0001_a.sql").write_text("-- sqlpush: revision=0001 risk=SAFE\nSELECT 1;")
    assert next_revision_id(tmp_path) == "0002"
    (tmp_path / "0042_b.sql").write_text("-- sqlpush: revision=0042 risk=SAFE\nSELECT 1;")
    assert next_revision_id(tmp_path) == "0043"


def test_next_revision_id_empty_dir(tmp_path):
    assert next_revision_id(tmp_path) == "0001"
