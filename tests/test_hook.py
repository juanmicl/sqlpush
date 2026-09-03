# tests/test_hook.py — project hook (sqlpush.py) discovery, precedence,
# typed errors and the package-shadowing pin. DB-free unless marked.
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

import sqlpush.cli
from sqlpush.cli import app
from sqlpush.types import MigrateReport, Plan, SqlpushError

runner = CliRunner()
DSN = os.environ.get(
    "SQLPUSH_TEST_DSN", "postgresql+psycopg://sqlpush:sqlpush@localhost:5433/sqlpush_test"
)
HOOK_DSN = "postgresql+psycopg://u:p@localhost:1/hookunit"
ENV_DSN = "postgresql+psycopg://u:p@localhost:1/envunit"
FLAG_DSN = "postgresql+psycopg://u:p@localhost:1/flagunit"

# a parseable-but-never-connected DSN: create_engine is faked in the
# DB-free tests; only the string value matters
FULL_HOOK = (
    "from sqlalchemy import Column, Integer, MetaData, Table\n"
    f"HOOK_DSN = {HOOK_DSN!r}\n"
    "def get_dsn():\n"
    "    return HOOK_DSN\n"
    "def get_metadata():\n"
    "    md = MetaData()\n"
    "    Table('hook_marker_tbl', md, Column('id', Integer, primary_key=True))\n"
    "    return md\n"
    "CHAIN_DIR = 'migrations/chain'\n"
)


class _FakeEngine:
    def dispose(self) -> None:
        pass


def _capture_engine(monkeypatch, seen: dict) -> None:
    def _create(dsn, **kw):
        seen["dsn"] = dsn
        return _FakeEngine()

    monkeypatch.setattr(sqlpush.cli, "create_engine", _create)


# --- the hook resolves dsn/metadata/dir -------------------------------------


def test_hook_resolves_metadata_and_dsn_for_check(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sqlpush.py").write_text(FULL_HOOK)
    seen: dict = {}
    _capture_engine(monkeypatch, seen)

    def fake_plan(md, engine, **kw):
        seen["md"] = md
        return Plan()  # clean → exit 0

    monkeypatch.setattr(sqlpush.cli.api, "plan", fake_plan)
    # NO --dsn, NO module:attribute positional, NO DATABASE_URL
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 0, r.output
    assert seen["dsn"] == HOOK_DSN
    # the metadata OBJECT came from the hook's get_metadata()
    assert "hook_marker_tbl" in seen["md"].tables


def test_hook_chain_dir_and_flag_precedence_for_migrate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sqlpush.py").write_text(FULL_HOOK)
    seen: dict = {}
    _capture_engine(monkeypatch, seen)
    monkeypatch.setattr(
        sqlpush.cli.api,
        "migrate",
        lambda *a, **k: seen.__setitem__("dir", k.get("chain_dir")) or MigrateReport(),
    )
    r = runner.invoke(app, ["migrate"])
    assert r.exit_code == 0, r.output
    assert seen["dir"] == Path("migrations/chain")  # hook CHAIN_DIR default
    explicit = tmp_path / "explicit" / "dir"
    r2 = runner.invoke(app, ["migrate", "--dir", str(explicit)])
    assert r2.exit_code == 0
    assert seen["dir"] == explicit  # explicit --dir flag beats the hook


def test_hook_revision_ref_dsn_and_chain_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sqlpush.py").write_text(FULL_HOOK)
    seen: dict = {}
    _capture_engine(monkeypatch, seen)
    monkeypatch.setattr(
        sqlpush.cli.api,
        "revision",
        lambda md, engine, **k: seen.__setitem__("out_dir", k.get("out_dir")) or Path("x.sql"),
    )
    # no --ref-dsn, no positional, no -m — everything from the hook
    r = runner.invoke(app, ["revision"])
    assert r.exit_code == 0, r.output
    assert seen["dsn"] == HOOK_DSN
    assert seen["out_dir"] == Path("migrations/chain")


# --- precedence: flag > hook > env ------------------------------------------


def test_dsn_precedence_flag_beats_hook_beats_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sqlpush.py").write_text(FULL_HOOK)
    monkeypatch.setenv("DATABASE_URL", ENV_DSN)
    seen: dict = {}
    _capture_engine(monkeypatch, seen)
    monkeypatch.setattr(sqlpush.cli.api, "plan", lambda md, engine, **kw: Plan())
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 0
    assert seen["dsn"] == HOOK_DSN  # hook > $DATABASE_URL
    r2 = runner.invoke(app, ["check", "--dsn", FLAG_DSN])
    assert r2.exit_code == 0
    assert seen["dsn"] == FLAG_DSN  # explicit flag > hook


def test_no_hook_env_fallback_unchanged(tmp_path, monkeypatch):
    # without a hook: env var behaves exactly as today (flag > env)
    monkeypatch.chdir(tmp_path)  # no sqlpush.py here
    monkeypatch.setenv("DATABASE_URL", ENV_DSN)
    seen: dict = {}
    _capture_engine(monkeypatch, seen)
    monkeypatch.setattr(sqlpush.cli.api, "plan", lambda md, engine, **kw: Plan())
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "models.py").write_text("from sqlalchemy import MetaData\nmetadata = MetaData()\n")
    r = runner.invoke(app, ["check", "models:metadata"])
    assert r.exit_code == 0
    assert seen["dsn"] == ENV_DSN


# --- shadowing pin: the package always wins ---------------------------------


def test_hook_never_shadows_package(tmp_path):
    # spec point 2: a file named sqlpush.py in the CWD must never shadow
    # the installed package. The CLI APPENDS the CWD to sys.path (never
    # insert(0)); replicated here in a fresh subprocess (console-script
    # shape: the CWD is not already at sys.path[0]).
    (tmp_path / "sqlpush.py").write_text("def get_dsn():\n    return 'postgresql://x'\n")
    code = (
        "import sys, os\n"
        "sys.path[:] = [p for p in sys.path if p not in ('', os.getcwd())]\n"
        "from sqlpush.hook import load_project_hook\n"
        "hook = load_project_hook()\n"
        "assert hook is not None, 'hook must be discovered'\n"
        "assert os.getcwd() in sys.path, 'cwd appended'\n"
        "assert sys.path[-1] == os.getcwd(), 'appended LAST, never first'\n"
        "import sqlpush\n"
        "print(sqlpush.__file__)\n"
    )
    # check=False: the returncode assert below is the failure signal
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    resolved = Path(proc.stdout.strip()).resolve()
    assert resolved != (tmp_path / "sqlpush.py").resolve()
    assert resolved.name == "__init__.py" and resolved.parent.name == "sqlpush"


# --- typed errors: names the file and the member -----------------------------


def test_hook_missing_get_dsn_errors_typed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sqlpush.py").write_text("CHAIN_DIR = 'x'\n")  # no get_dsn
    r = runner.invoke(app, ["migrate"])
    assert r.exit_code == 1
    assert isinstance(r.exception, SqlpushError)
    assert "sqlpush.py: missing get_dsn()" in str(r.exception)


def test_hook_missing_get_metadata_errors_typed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sqlpush.py").write_text("def get_dsn():\n    return 'postgresql://x'\n")
    r = runner.invoke(app, ["check"])  # needs metadata, hook has none
    assert r.exit_code == 1
    assert isinstance(r.exception, SqlpushError)
    assert "sqlpush.py: missing get_metadata()" in str(r.exception)


def test_hook_get_dsn_raises_typed_with_cause(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sqlpush.py").write_text("def get_dsn():\n    raise RuntimeError('boom-dsn')\n")
    r = runner.invoke(app, ["migrate"])
    assert r.exit_code == 1
    assert isinstance(r.exception, SqlpushError)
    assert "sqlpush.py: get_dsn() raised:" in str(r.exception)
    assert "boom-dsn" in str(r.exception)
    assert isinstance(r.exception.__cause__, RuntimeError)  # not swallowed


def test_hook_get_metadata_raises_typed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sqlpush.py").write_text(
        "def get_dsn():\n    return 'postgresql://x'\n"
        "def get_metadata():\n    raise RuntimeError('boom-md')\n"
    )
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 1
    assert isinstance(r.exception, SqlpushError)
    assert "sqlpush.py: get_metadata() raised:" in str(r.exception)
    assert "boom-md" in str(r.exception)


def test_hook_import_failure_typed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sqlpush.py").write_text("def broken(:\n")  # syntax error
    r = runner.invoke(app, ["migrate"])
    assert r.exit_code == 1
    assert isinstance(r.exception, SqlpushError)
    assert "sqlpush.py" in str(r.exception)


def test_no_hook_missing_metadata_spec_exit_2(tmp_path, monkeypatch):
    # backwards compat: no hook, no positional → same class of failure
    # as today's missing-argument usage error (exit 2), message points
    # at both remedies
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r = runner.invoke(app, ["check", "--dsn", "postgresql+psycopg://u:p@localhost:1/x"])
    assert r.exit_code == 2
    assert "module:attribute" in r.stderr
    assert "sqlpush.py" in r.stderr


# --- end-to-end against the dev DB -------------------------------------------

DSN_HOOK = (
    "from sqlalchemy import Column, Integer, MetaData, String, Table\n"
    f"DSN = {DSN!r}\n"
    "def get_dsn():\n"
    "    return DSN\n"
    "def get_metadata():\n"
    "    md = MetaData()\n"
    "    Table('hook_hero', md, Column('id', Integer, primary_key=True), "
    "Column('name', String(50)))\n"
    "    return md\n"
)


@pytest.mark.pg
def test_hook_end_to_end_check_clean(tmp_path, monkeypatch, pg_engine):
    # the full contract on a real DB: hook resolves dsn AND metadata,
    # zero CLI inputs, in-sync schema → check exits 0
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sqlpush.py").write_text(DSN_HOOK)
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS hook_hero"))
        conn.execute(text("CREATE TABLE hook_hero (id integer PRIMARY KEY, name varchar(50))"))
    try:
        r = runner.invoke(app, ["check"])
        assert r.exit_code == 0, r.output
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS hook_hero"))
