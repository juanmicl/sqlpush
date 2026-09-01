from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, text

from sqlpush.api import check, migrate
from sqlpush.chain.format import MigrationFileError

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
