# tests/test_annotations_createall.py
from __future__ import annotations

import pytest
from sqlalchemy import Column, DateTime, Index, Integer, MetaData, Table, text

from sqlpush.annotations import hypertable

pytestmark = [pytest.mark.pg, pytest.mark.timescale]


@pytest.fixture()
def clean_db(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS annht"))
    yield pg_engine
    # teardown: annht must not leak into other files' fixtures
    # (their clean_db variants don't know about this table)
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS annht"))


def _annotated_table() -> MetaData:
    md = MetaData()
    tbl = Table(
        "annht",
        md,
        # Composite PK (id, ts): timescaledb requires the partitioning
        # column to be part of any primary key / unique constraint
        # (same shape as tests/test_api.py md_ht).
        Column("id", Integer, primary_key=True),
        Column("ts", DateTime(timezone=True), primary_key=True),
    )

    @hypertable(time_column="ts", chunk_time_interval="1 month")
    class _Carrier:
        __table__ = tbl

    return md


def test_create_all_registers_hypertable(clean_db):
    _annotated_table().create_all(clean_db)
    with clean_db.connect() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = 'annht'"
            )
        ).scalar()
        # timescaledb_information.hypertables carries no interval column
        # in this server version; the public accessor is dimensions.
        # time_interval normalizes INTERVAL '1 month' to '30 days'.
        interval = conn.execute(
            text(
                "SELECT date_part('days', time_interval) "
                "FROM timescaledb_information.dimensions "
                "WHERE hypertable_name = 'annht'"
            )
        ).scalar()
    assert n == 1
    assert interval == 30  # INTERVAL '1 month' ≈ 30 días


def test_create_all_with_declared_index_single_index(clean_db):
    """PC-1: índice declarado + listener → UN solo índice (create_hypertable
    ve la columna indexada y no crea default)."""
    md = _annotated_table()
    Index("annht_ts_idx", md.tables["annht"].c.ts)
    md.create_all(clean_db)
    with clean_db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'annht' AND indexdef LIKE '%(ts)%'"
            )
        ).all()
    assert [r[0] for r in rows] == ["annht_ts_idx"]  # UNO, el declarado


def test_listener_idempotent_with_directive_plan(clean_db):
    """Coexistencia: create_all (listener) y luego migrate/push (directiva,
    if_not_exists) no duplican nada."""
    import sqlpush.api as sqlpush_api

    md = _annotated_table()
    md.create_all(clean_db)
    rep = sqlpush_api.check(md, clean_db)
    assert rep.clean, "post-create_all + directiva debe quedar limpio"
