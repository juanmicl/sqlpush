from __future__ import annotations

import pytest
from sqlalchemy import Column, Index, Integer, MetaData, String, Table, text

from sqlpush.api import check, migrate, revision
from sqlpush.apply.executor import advisory_key
from sqlpush.chain.format import MigrationFileError
from sqlpush.types import SqlpushError

pytestmark = pytest.mark.pg

# Tablas que este módulo ensucia en la BD de sesión: teardown antes Y después
# (nota de proceso T2 — un fixture before-only filtra drift fantasma a otros
# módulos vía el pg_engine session-scoped)
_DIRTY = ("sqlpush_versions", "mt1", "mt1x", "mt3", "ok1")


@pytest.fixture()
def migrate_db(pg_engine):
    def _clean() -> None:
        with pg_engine.begin() as conn:
            for t in _DIRTY:
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))

    _clean()
    yield pg_engine
    _clean()


def _write(chain_dir, name: str, body: str) -> None:
    chain_dir.mkdir(parents=True, exist_ok=True)
    (chain_dir / name).write_text(body)


SAFE_0001 = "-- sqlpush: revision=0001 risk=SAFE\nCREATE TABLE mt1 (id integer PRIMARY KEY);\n"
DESTRUCTIVE_0002 = "-- sqlpush: revision=0002 risk=DESTRUCTIVE\nDROP TABLE mt1;\n"


def test_migrate_applies_and_bookkeeps(migrate_db, tmp_path):
    _write(tmp_path, "0001_init.sql", SAFE_0001)
    rep = migrate(migrate_db, chain_dir=tmp_path)
    assert rep.applied == ("0001_init.sql",) and not rep.blocked
    with migrate_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sqlpush_versions")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM mt1")).scalar() == 0


def test_migrate_idempotent_skip(migrate_db, tmp_path):
    _write(tmp_path, "0001_init.sql", SAFE_0001)
    migrate(migrate_db, chain_dir=tmp_path)
    rep = migrate(migrate_db, chain_dir=tmp_path)
    assert rep.applied == () and rep.skipped == ("0001_init.sql",)


def test_migrate_destructive_gate(migrate_db, tmp_path):
    _write(tmp_path, "0001_init.sql", SAFE_0001)
    migrate(migrate_db, chain_dir=tmp_path)
    _write(tmp_path, "0002_drop.sql", DESTRUCTIVE_0002)
    rep = migrate(migrate_db, chain_dir=tmp_path)  # sin flag
    assert rep.applied == () and "0002_drop.sql" in rep.blocked
    rep2 = migrate(migrate_db, chain_dir=tmp_path, allow_destructive=True)
    assert rep2.applied == ("0002_drop.sql",)


def test_migrate_checksum_mismatch_blocks_and_stops(migrate_db, tmp_path):
    _write(tmp_path, "0001_init.sql", SAFE_0001)
    migrate(migrate_db, chain_dir=tmp_path)
    _write(
        tmp_path,
        "0003_later.sql",
        "-- sqlpush: revision=0003 risk=SAFE\nCREATE TABLE mt3 (id int);\n",
    )
    (tmp_path / "0001_init.sql").write_text(SAFE_0001.replace("mt1", "mt1x"))  # edit post-apply
    rep = migrate(migrate_db, chain_dir=tmp_path)
    assert "0001_init.sql" in rep.blocked
    assert "0003_later.sql" not in rep.applied  # orden estricto: posterior no corre


def test_migrate_partial_failure_no_bookkeeping(migrate_db, tmp_path):
    body = "-- sqlpush: revision=0001 risk=SAFE\nCREATE TABLE ok1 (id int);\nTHIS IS NOT SQL;\n"
    _write(tmp_path, "0001_bad.sql", body)
    rep = migrate(migrate_db, chain_dir=tmp_path)
    assert rep.partial_failure is True and "0001_bad.sql" in rep.blocked
    with migrate_db.connect() as conn:
        # same-txn bookkeeping: el fallo rollback también el registro
        assert conn.execute(text("SELECT count(*) FROM sqlpush_versions")).scalar() == 0
        assert (
            conn.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_name='ok1'")
            ).scalar()
            == 0
        )


def test_migrate_creates_versions_table_even_when_idle(migrate_db, tmp_path):
    tmp_path.mkdir(exist_ok=True)  # vacío
    migrate(migrate_db, chain_dir=tmp_path)
    with migrate_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sqlpush_versions")).scalar() == 0
        # y la tabla existe (implícita)


def test_migrate_missing_chain_dir_raises(migrate_db, tmp_path):
    # un --dir tipográficamente errado no puede ser un no-op silencioso
    with pytest.raises(MigrationFileError, match="chain dir not found"):
        migrate(migrate_db, chain_dir=tmp_path / "nope")


def test_migrate_accepts_dsn_string(migrate_db, tmp_path):
    _write(tmp_path, "0001_init.sql", SAFE_0001)
    # migrate_db IS pg_engine (cleaned) — .url renders the DSN string target
    dsn = migrate_db.url.render_as_string(hide_password=False)
    rep = migrate(dsn, chain_dir=tmp_path)
    assert rep.applied == ("0001_init.sql",)


def test_versions_table_pruned_from_public_check(migrate_db, tmp_path):
    """C1: the chain's own bookkeeping must not appear as drift (alembic_version class)."""
    _write(tmp_path, "0001_init.sql", SAFE_0001)
    migrate(migrate_db, chain_dir=tmp_path)
    md = MetaData()
    Table("mt1", md, Column("id", Integer, primary_key=True))  # exactly the migrated state
    result = check(md, migrate_db)  # PUBLIC scope — no schemas=
    assert result.clean, f"sqlpush_versions leaked into the diff: {result.drift}"


def test_migrate_advisory_wait_bounded(migrate_db, tmp_path):
    # B3: the chain session's advisory lock wait is BOUNDED — a second
    # connection holding the same key plus advisory_wait=0 must raise
    # SqlpushError promptly instead of blocking on pg_advisory_lock
    # forever (mirrors with_advisory_lock's wait-exhaustion contract).
    _write(tmp_path, "0001_init.sql", SAFE_0001)
    holder = migrate_db.connect()
    key: int | None = None
    try:
        key = advisory_key(holder)
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        with pytest.raises(SqlpushError, match="advisory lock"):
            migrate(migrate_db, chain_dir=tmp_path, advisory_wait=0)
    finally:
        if key is not None:
            holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        holder.close()


# --- 0.5.0 Lane 2: hybrid CONCURRENTLY replay (B8/B11) ---------------------

_CONCURRENT_TABLES = (
    "sqlpush_versions",
    "revt2",
    "mixfail_host",
    "mixfail_new",
    "cli_xy",
    "cli_lblless",
    "migdq_tbl",
)


@pytest.fixture()
def concurrent_db(pg_engine):
    def _clean() -> None:
        with pg_engine.begin() as conn:
            for t in _CONCURRENT_TABLES:
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
            conn.execute(text("DROP FUNCTION IF EXISTS migdq()"))

    _clean()
    yield pg_engine
    _clean()


def _label_op(n: int, label: str, sql: str) -> str:
    return f"-- op {n} [{label}]\n{sql.rstrip(';')};\n"


def test_generated_file_with_concurrent_op_and_dollar_quotes_replays(concurrent_db, tmp_path):
    # T10 (pinned closed loop): revision generates a CONCURRENTLY index
    # file for an EXISTING table; a hand-ensured op with a dollar-quoted
    # body containing ';' replays per-op (labels delimit, nothing is
    # statement-split), migrate applies, the versions row lands, and a
    # re-migrate skips — idempotent.
    with concurrent_db.begin() as conn:
        conn.execute(text("CREATE TABLE revt2 (id integer PRIMARY KEY, note text)"))
    md = MetaData()
    t = Table("revt2", md, Column("id", Integer, primary_key=True), Column("note", String))
    Index("ix_revt2_note", t.c.note)
    out = revision(md, concurrent_db, out_dir=tmp_path, message="add index")
    generated = out.read_text()
    assert "CONCURRENTLY" in generated
    # ensure the dollar-quoted op rides the same file (hand-append)
    with out.open("a") as fh:
        fh.write(
            "\n"
            + _label_op(
                2,
                "SAFE] add_function migdq",
                "CREATE OR REPLACE FUNCTION migdq() RETURNS text AS $$\n"
                "BEGIN\n  RETURN 'a;b';\nEND;\n$$ LANGUAGE plpgsql;",
            )
        )
    rep = migrate(concurrent_db, chain_dir=tmp_path)
    assert rep.applied == (out.name,) and not rep.blocked, rep.notes
    with concurrent_db.connect() as conn:
        assert conn.execute(text("SELECT migdq()")).scalar() == "a;b"
        assert (
            conn.execute(
                text("SELECT count(*) FROM public.sqlpush_versions WHERE name = :n"),
                {"n": out.name},
            ).scalar()
            == 1
        )
        assert conn.execute(text("SELECT count(*) FROM revt2")).scalar() == 0  # data intact
    rep2 = migrate(concurrent_db, chain_dir=tmp_path)
    assert rep2.applied == () and rep2.skipped == (out.name,)


def test_plain_file_replays_whole_text_path(concurrent_db, tmp_path):
    # T11: NO CONCURRENTLY anywhere in the file → raw fast path. The
    # dollar-quoted body contains a line starting with '--' (and a ';'):
    # executing it verbatim is correct; a per-op replay would STRIP that
    # line (parser comment rule) and break the string literal. Guards
    # against "simplifying" to always-per-op.
    body = (
        "-- sqlpush: revision=0001 risk=SAFE\n"
        "CREATE OR REPLACE FUNCTION migdq() RETURNS text AS $$\n"
        "BEGIN\n"
        "  RETURN 'x\n"
        "-- marker; line';\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n"
    )
    _write(tmp_path, "0001_plain.sql", body)
    rep = migrate(concurrent_db, chain_dir=tmp_path)
    assert rep.applied == ("0001_plain.sql",), rep.notes
    with concurrent_db.connect() as conn:
        assert conn.execute(text("SELECT migdq()")).scalar() == "x\n-- marker; line"


def test_mixed_file_plain_before_concurrent(concurrent_db, tmp_path):
    # T12: create→index dependency INSIDE one file: the plain segment
    # must run first (a concurrent-first order would fail: the index
    # targets a table that does not exist yet).
    body = (
        "-- sqlpush: revision=0001 risk=RISKY\n"
        + _label_op(1, "SAFE] add_table cli_xy", "CREATE TABLE cli_xy (id integer PRIMARY KEY)")
        + "\n"
        + _label_op(
            2, "RISKY] add_index ix_cli_xy", "CREATE INDEX CONCURRENTLY ix_cli_xy ON cli_xy (id)"
        )
    )
    _write(tmp_path, "0001_mixed.sql", body)
    rep = migrate(concurrent_db, chain_dir=tmp_path)
    assert rep.applied == ("0001_mixed.sql",), rep.notes
    with concurrent_db.connect() as conn:
        idx = conn.execute(text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_cli_xy'")).first()
        assert idx is not None


def test_concurrent_op_failure_blocks_file_no_versions_row(concurrent_db, tmp_path):
    # T13: the concurrent op fails (index name already taken by the
    # pre-existing mixfail_host index) → file blocked, partial_failure,
    # NO versions row (the plain segment of THIS file already committed —
    # reported honestly in notes), and strict order: later files
    # untouched.
    with concurrent_db.begin() as conn:
        conn.execute(text("CREATE TABLE mixfail_host (id integer)"))
        conn.execute(text("CREATE INDEX ix_mixfail ON mixfail_host (id)"))
    body = (
        "-- sqlpush: revision=0001 risk=RISKY\n"
        + _label_op(
            1, "SAFE] add_table mixfail_new", "CREATE TABLE mixfail_new (id integer PRIMARY KEY)"
        )
        + "\n"
        + _label_op(
            2,
            "RISKY] add_index ix_mixfail",
            "CREATE INDEX CONCURRENTLY ix_mixfail ON mixfail_new (id)",
        )
    )
    _write(tmp_path, "0001_fail.sql", body)
    _write(tmp_path, "0002_later.sql", SAFE_0001)
    rep = migrate(concurrent_db, chain_dir=tmp_path)
    assert "0001_fail.sql" in rep.blocked
    assert rep.partial_failure is True
    assert "0002_later.sql" not in rep.applied  # strict order holds
    assert any("already committed" in n for n in rep.notes)  # honest report
    with concurrent_db.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM public.sqlpush_versions WHERE name = '0001_fail.sql'")
            ).scalar()
            == 0
        )  # no row: the failure is never recorded as applied
        # the plain segment's table exists (committed), the index does not
        assert conn.execute(text("SELECT count(*) FROM mixfail_new")).scalar() == 0
        assert (
            conn.execute(
                text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_mixfail'")
            ).scalar()
            == 1
        )  # only the pre-existing one on mixfail_host


def test_no_if_not_exists_generated_and_rerun_fails_loud(concurrent_db, tmp_path):
    # T14: generated concurrent SQL carries no IF NOT EXISTS guard — the
    # documented crash-window semantics: with the versions row gone (crash
    # after apply, before/mid bookkeeping), a re-run fails LOUD on the
    # existing index instead of silently skipping.
    with concurrent_db.begin() as conn:
        conn.execute(text("CREATE TABLE revt2 (id integer PRIMARY KEY, note text)"))
    md = MetaData()
    t = Table("revt2", md, Column("id", Integer, primary_key=True), Column("note", String))
    Index("ix_revt2_note", t.c.note)
    out = revision(md, concurrent_db, out_dir=tmp_path, message="add index")
    generated = out.read_text()
    assert "CONCURRENTLY" in generated
    assert "IF NOT EXISTS" not in generated
    rep = migrate(concurrent_db, chain_dir=tmp_path)
    assert rep.applied == (out.name,)
    # simulate the crash window: bookkeeping lost, objects present
    with concurrent_db.begin() as conn:
        conn.execute(text("DELETE FROM public.sqlpush_versions"))
    rep2 = migrate(concurrent_db, chain_dir=tmp_path)
    assert out.name in rep2.blocked and rep2.partial_failure is True


def test_hand_edit_concurrent_in_labelless_file_routes_autocommit(concurrent_db, tmp_path):
    # T15: label-less body containing CREATE INDEX CONCURRENTLY — the
    # parser yields ONE op (whole body), which routes to the autocommit
    # lane: everything applies (documented hand-edit behavior, chain
    # spec §7).
    body = (
        "-- sqlpush: revision=0001 risk=RISKY\n"
        "CREATE TABLE cli_lblless (id integer PRIMARY KEY);\n"
        "CREATE INDEX CONCURRENTLY ix_lblless ON cli_lblless (id);\n"
    )
    _write(tmp_path, "0001_lblless.sql", body)
    rep = migrate(concurrent_db, chain_dir=tmp_path)
    assert rep.applied == ("0001_lblless.sql",), rep.notes
    with concurrent_db.connect() as conn:
        idx = conn.execute(text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_lblless'")).first()
        assert idx is not None
