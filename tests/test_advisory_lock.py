# tests/test_advisory_lock.py
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, text

from sqlpush.apply.executor import advisory_key, fnv1a_32, with_advisory_lock
from sqlpush.core.diff import DiffEngine
from sqlpush.types import SqlpushError

pytestmark = pytest.mark.pg


def test_fnv1a_known_vector():
    # FNV-1a 32-bit test vector for "sqlpush", deterministic by design
    assert fnv1a_32(b"") == 2166136261
    assert fnv1a_32(b"a") == 0xE40C292C


def test_advisory_key_stable_per_db(pg_engine):
    with pg_engine.connect() as c1, pg_engine.connect() as c2:
        assert advisory_key(c1) == advisory_key(c2)


def test_winner_applies_loser_reverifies(pg_engine):
    md = MetaData()
    Table("adv_hero", md, Column("id", Integer, primary_key=True))
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS adv_hero"))

    report = with_advisory_lock(pg_engine, md, reverify=DiffEngine())
    import sqlalchemy as sa

    assert "adv_hero" in sa.inspect(pg_engine).get_table_names()
    assert not report.partial_failure

    # loser path: table already migrated -> re-verify returns clean
    report2 = with_advisory_lock(pg_engine, md, reverify=DiffEngine())
    assert report2.applied == ()  # nothing to do

    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS adv_hero"))


def test_wait_exhaustion_raises_sqlpush_error(pg_engine):
    # I4e: a second worker holding the same advisory lock + wait=0 must
    # raise SqlpushError promptly (bounded wait), and the holder must be
    # able to release cleanly afterwards.
    md = MetaData()
    holder = pg_engine.connect()
    key: int | None = None
    try:
        key = advisory_key(holder)
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        with pytest.raises(SqlpushError, match="advisory lock"):
            with_advisory_lock(pg_engine, md, wait=0, reverify=DiffEngine())
    finally:
        if key is not None:
            holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        holder.close()
