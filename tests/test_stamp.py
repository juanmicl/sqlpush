from __future__ import annotations

import pytest
from sqlalchemy import text

from sqlpush.api import migrate, stamp
from sqlpush.chain.format import checksum
from sqlpush.types import SqlpushError

# helpers inlined: `from tests.test_migrate import ...` doesn't resolve —
# tests/ is not a package and repo root is not on sys.path under pytest
SAFE_0001 = "-- sqlpush: revision=0001 risk=SAFE\nCREATE TABLE mt1 (id integer PRIMARY KEY);\n"


def _write(chain_dir, name: str, body: str) -> None:
    chain_dir.mkdir(parents=True, exist_ok=True)
    (chain_dir / name).write_text(body)


pytestmark = pytest.mark.pg


@pytest.fixture()
def stamp_db(pg_engine):
    def _clean() -> None:
        with pg_engine.begin() as conn:
            for t in ("sqlpush_versions", "mt1"):
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))

    _clean()
    yield pg_engine
    _clean()


def test_stamp_bootstraps_existing_db(stamp_db, tmp_path):
    # BD "existente": el schema YA está (simulado creando la tabla a mano)
    with stamp_db.begin() as conn:
        conn.execute(text("CREATE TABLE mt1 (id integer PRIMARY KEY)"))
    _write(tmp_path, "0001_init.sql", SAFE_0001)  # migración que YA está aplicada de facto
    rep = stamp(stamp_db, chain_dir=tmp_path)
    # stamp no "aplica": registra — los registrados se reportan en skipped
    assert rep.skipped == ("0001_init.sql",) and rep.applied == ()
    rep2 = migrate(stamp_db, chain_dir=tmp_path)
    assert rep2.skipped == ("0001_init.sql",) and rep2.applied == ()  # ahora migrate es no-op


def test_stamp_registers_correct_checksums(stamp_db, tmp_path):
    _write(tmp_path, "0001_init.sql", SAFE_0001)
    stamp(stamp_db, chain_dir=tmp_path)
    with stamp_db.connect() as conn:
        rows = conn.execute(text("SELECT name, sha256 FROM public.sqlpush_versions")).all()
    assert rows == [("0001_init.sql", checksum(SAFE_0001))]


def test_stamp_never_executes_sql(stamp_db, tmp_path):
    # SQL inválido en el body — solo el header debe parsear (fail-loud en ESO)
    body = "-- sqlpush: revision=0001 risk=SAFE\nTHIS IS NOT SQL;\n"
    _write(tmp_path, "0001_ghost.sql", body)
    rep = stamp(stamp_db, chain_dir=tmp_path)
    assert rep.skipped == ("0001_ghost.sql",)
    assert rep.applied == () and rep.blocked == () and not rep.partial_failure
    with stamp_db.connect() as conn:
        # registrado pero jamás ejecutado: sin error, sin cambio de schema
        assert conn.execute(text("SELECT count(*) FROM public.sqlpush_versions")).scalar() == 1


def test_stamp_fail_loud_on_bad_header(stamp_db, tmp_path):
    _write(tmp_path, "0001_bad.sql", "SELECT 1;\n")  # sin header
    _write(tmp_path, "0002_later.sql", SAFE_0001)
    rep = stamp(stamp_db, chain_dir=tmp_path)
    assert rep.blocked == ("0001_bad.sql",)
    assert rep.skipped == ()  # orden estricto: nada posterior se registra
    assert any(n.startswith("0001_bad.sql: ") for n in rep.notes)


def test_stamp_refuses_edited_file_without_force(stamp_db, tmp_path):
    # B4: re-stamping after editing an applied/stamped file must NOT
    # silently refresh the checksum — that wipes the edit-detection the
    # whole chain integrity rides on. Refusal leaves the recorded
    # checksum untouched; force=True accepts the new content.
    _write(tmp_path, "0001_init.sql", SAFE_0001)
    stamp(stamp_db, chain_dir=tmp_path)
    edited = SAFE_0001.replace("mt1", "mt1_edited")
    _write(tmp_path, "0001_init.sql", edited)
    with pytest.raises(SqlpushError, match="0001_init.sql"):
        stamp(stamp_db, chain_dir=tmp_path)
    with stamp_db.connect() as conn:
        sha = conn.execute(
            text("SELECT sha256 FROM public.sqlpush_versions WHERE name = '0001_init.sql'")
        ).scalar()
    assert sha == checksum(SAFE_0001)  # la negativa NO refrescó el registro

    rep = stamp(stamp_db, chain_dir=tmp_path, force=True)
    assert rep.skipped == ("0001_init.sql",)
    with stamp_db.connect() as conn:
        sha2 = conn.execute(
            text("SELECT sha256 FROM public.sqlpush_versions WHERE name = '0001_init.sql'")
        ).scalar()
    assert sha2 == checksum(edited)  # force acepta el contenido nuevo


def test_stamp_mismatch_stops_the_walk(stamp_db, tmp_path):
    # strict order on refusal too: the FIRST mismatch raises and nothing
    # after it registers (same contract as migrate's blocked files)
    _write(tmp_path, "0001_init.sql", SAFE_0001)
    stamp(stamp_db, chain_dir=tmp_path)
    _write(tmp_path, "0001_init.sql", SAFE_0001.replace("mt1", "mt1x"))  # edit post-stamp
    _write(
        tmp_path,
        "0002_later.sql",
        "-- sqlpush: revision=0002 risk=SAFE\nCREATE TABLE mt2 (id int);\n",
    )
    with pytest.raises(SqlpushError, match="0001_init.sql"):
        stamp(stamp_db, chain_dir=tmp_path)
    with stamp_db.connect() as conn:
        later = conn.execute(
            text("SELECT count(*) FROM public.sqlpush_versions WHERE name = '0002_later.sql'")
        ).scalar()
    assert later == 0  # nada posterior al mismatch se registra
