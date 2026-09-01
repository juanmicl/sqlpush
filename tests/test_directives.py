from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, Table

from sqlpush.annotations import hypertable
from sqlpush.directives.timescale import hypertable_operations
from sqlpush.types import RiskClass


def _md():
    md = MetaData()
    t = Table("metrics", md, Column("ts", DateTime), Column("v", Integer))
    t.info["sqlpush_hypertable"] = type(
        "H", (), {"time_column": "ts", "chunk_time_interval": "1 day"}
    )()
    return md


def test_operation_emitted():
    ops = hypertable_operations(_md())
    assert len(ops) == 1
    assert ops[0].type == "create_hypertable"
    assert ops[0].risk is RiskClass.SAFE
    assert "create_hypertable" in ops[0].sql
    assert "'metrics'" in ops[0].sql and "'ts'" in ops[0].sql
    assert "chunk_time_interval" in ops[0].sql


def test_real_annotation_round_trip():
    # End-to-end with the real decorator + real HypertableInfo: proves the
    # dataclass recorded by @hypertable flows through hypertable_operations
    # and produces the expected SQL shape (fake-info test above covers only
    # attribute duck-typing).
    md = MetaData()
    table = Table("events", md, Column("ts", DateTime), Column("v", Integer))

    class Events:
        __table__ = table

    hypertable(time_column="ts", chunk_time_interval="7 days")(Events)

    ops = hypertable_operations(md)
    assert len(ops) == 1
    assert ops[0].sql == (
        "SELECT create_hypertable('events', 'ts', "
        "chunk_time_interval => INTERVAL '7 days', migrate_data => true, "
        "if_not_exists => true, create_default_indexes => false);"
    )


def test_identifier_escaping():
    # I7: every interpolated literal (table, time column, interval) must
    # escape ' as '': quote-bearing names must not break out of the
    # string literal
    md = MetaData()
    t = Table("weird'name", md, Column("ts'", DateTime), Column("v", Integer))
    t.info["sqlpush_hypertable"] = type(
        "H", (), {"time_column": "ts'; --", "chunk_time_interval": "1 day'"}
    )()
    ops = hypertable_operations(md)
    assert len(ops) == 1
    assert ops[0].sql == (
        "SELECT create_hypertable('weird''name', 'ts''; --', "
        "chunk_time_interval => INTERVAL '1 day''', migrate_data => true, "
        "if_not_exists => true, create_default_indexes => false);"
    )


def test_non_public_schema_relation_qualified():
    # F3a (push fire-test): create_hypertable resolves an unqualified
    # relation via the session search_path — a table in a non-default
    # schema must carry its schema or the op targets public.<name>
    # (UndefinedTable at apply time).
    md = MetaData()
    t = Table(
        "metrics",
        md,
        Column("ts", DateTime),
        Column("v", Integer),
        schema="other",
    )
    t.info["sqlpush_hypertable"] = type(
        "H", (), {"time_column": "ts", "chunk_time_interval": "1 day"}
    )()
    ops = hypertable_operations(md)
    assert len(ops) == 1
    assert "'other.metrics'" in ops[0].sql
    assert "create_hypertable" in ops[0].sql
