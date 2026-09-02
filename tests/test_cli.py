# tests/test_cli.py
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, text
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

import sqlpush.cli
from sqlpush.cli import app
from sqlpush.types import (
    AppliedOperation,
    Plan,
    PlannedOperation,
    Report,
    RiskClass,
    SqlpushError,
)

runner = CliRunner()
DSN = os.environ.get(
    "SQLPUSH_TEST_DSN", "postgresql+psycopg://sqlpush:sqlpush@localhost:5433/sqlpush_test"
)
DSN_ARG = ["--dsn", DSN]
# never connected to: api.push is monkeypatched in the unit tests below,
# and create_engine connects lazily; this DSN only has to parse.
UNIT_DSN = ["--dsn", "postgresql+psycopg://u:p@localhost:1/sqlpush_unit"]


def _unit_models(tmp_path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "unit_models.py").write_text(
        "from sqlalchemy import MetaData\nmetadata = MetaData()\n"
    )


def _drop(pg_engine, *names: str) -> None:
    with pg_engine.begin() as conn:
        for n in names:
            conn.execute(text(f"DROP TABLE IF EXISTS {n}"))


@pytest.fixture()
def hero(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS cli_hero"))
    md = MetaData()
    Table("cli_hero", md, Column("id", Integer, primary_key=True))
    yield md
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS cli_hero"))


@pytest.mark.pg
def test_check_exit_codes(tmp_path, monkeypatch, hero):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "models.py").write_text(
        "from sqlalchemy import Column, Integer, MetaData, Table\n"
        "metadata = MetaData()\n"
        "Table('cli_hero', metadata, Column('id', Integer, primary_key=True))\n"
    )
    r1 = runner.invoke(app, ["check", "models:metadata", *DSN_ARG])
    assert r1.exit_code == 2  # drift
    r2 = runner.invoke(app, ["push", "models:metadata", *DSN_ARG])
    assert r2.exit_code == 0
    r3 = runner.invoke(app, ["check", "models:metadata", *DSN_ARG])
    assert r3.exit_code == 0


@pytest.mark.pg
def test_check_destructive_exit_3(tmp_path, monkeypatch, pg_engine):
    # Deterministic drift source: a DB table absent from the (empty)
    # metadata, so every planned op is destructive. Against a live DB
    # this exits exactly 3 (drift + has_destructive).
    _drop(pg_engine, "cli_leftover")
    with pg_engine.begin() as conn:
        conn.execute(text("CREATE TABLE cli_leftover (id INTEGER PRIMARY KEY)"))
    try:
        monkeypatch.syspath_prepend(str(tmp_path))
        (tmp_path / "models2.py").write_text(
            "from sqlalchemy import MetaData\nmetadata = MetaData()\n"
        )
        r = runner.invoke(app, ["check", "models2:metadata", *DSN_ARG])
        # every table is drift/destructive against empty metadata
        assert r.exit_code == 3
    finally:
        _drop(pg_engine, "cli_leftover")


@pytest.mark.pg
def test_json_output_shape(tmp_path, monkeypatch, hero):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "models.py").write_text(
        "from sqlalchemy import Column, Integer, MetaData, Table\n"
        "metadata = MetaData()\n"
        "Table('cli_hero', metadata, Column('id', Integer, primary_key=True))\n"
    )
    r = runner.invoke(app, ["check", "models:metadata", "--json", *DSN_ARG])
    payload = json.loads(r.output)
    assert payload["version"] == 1 and payload["drift"] is True


@pytest.mark.pg
def test_bad_metadata_import_fails_cleanly():
    r = runner.invoke(app, ["diff", "no.such.module:metadata", *DSN_ARG])
    assert r.exit_code == 1


@pytest.mark.pg
def test_diff_render_contract_sections(tmp_path, monkeypatch, pg_engine):
    # Contract: diff output groups SAFE before DESTRUCTIVE,
    # omits absent risk sections, and joins multi-op sections with ";\n".
    _drop(pg_engine, "cli_rdr", "cli_rdr_orphan")
    with pg_engine.begin() as conn:
        conn.execute(text("CREATE TABLE cli_rdr (id SERIAL PRIMARY KEY, note TEXT)"))
        conn.execute(text("CREATE TABLE cli_rdr_orphan (id SERIAL PRIMARY KEY)"))
    try:
        monkeypatch.syspath_prepend(str(tmp_path))
        (tmp_path / "models3.py").write_text(
            "from sqlalchemy import Column, Integer, MetaData, Table, Text\n"
            "metadata = MetaData()\n"
            "Table('cli_rdr', metadata, Column('id', Integer, "
            "primary_key=True), Column('note', Text), "
            "Column('a', Integer), Column('b', Integer))\n"
        )
        r = runner.invoke(app, ["diff", "models3:metadata", *DSN_ARG])
        assert r.exit_code == 0
        out = r.output
        # (a) safe section renders before destructive
        assert out.index("-- safe") < out.index("-- destructive")
        # (b) no risky ops => no risky section
        assert "-- risky" not in out
        # (c) the two safe ops are joined with ";\n" inside the section
        body = out.split("-- safe\n", 1)[1].split("-- destructive", 1)[0]
        stmts = body.rstrip(";\n").split(";\n")
        assert len(stmts) == 2
        assert sum("ADD COLUMN a" in s for s in stmts) == 1
        assert sum("ADD COLUMN b" in s for s in stmts) == 1
        assert all(s.startswith("ALTER TABLE cli_rdr") for s in stmts)
        # and the destructive section holds the orphan drop
        assert "DROP TABLE cli_rdr_orphan" in out.split("-- destructive")[1]
    finally:
        _drop(pg_engine, "cli_rdr", "cli_rdr_orphan")


@pytest.mark.pg
def test_push_safe_only_skipped_json_and_info(tmp_path, monkeypatch, pg_engine):
    # RISKY op (index on an existing table), no destructive: --safe-only
    # applies nothing, records the op as skipped, exits 0. JSON exposes
    # "skipped"; text mode prints an informational stderr line.
    _drop(pg_engine, "cli_sk")
    with pg_engine.begin() as conn:
        conn.execute(text("CREATE TABLE cli_sk (id SERIAL PRIMARY KEY, note TEXT)"))
    try:
        monkeypatch.syspath_prepend(str(tmp_path))
        (tmp_path / "models4.py").write_text(
            "from sqlalchemy import Column, Index, Integer, MetaData, "
            "Table, Text\n"
            "metadata = MetaData()\n"
            "t = Table('cli_sk', metadata, Column('id', Integer, "
            "primary_key=True), Column('note', Text))\n"
            "Index('ix_cli_sk_note', t.c.note)\n"
        )
        r = runner.invoke(app, ["push", "models4:metadata", "--safe-only", "--json", *DSN_ARG])
        assert r.exit_code == 0  # skipped is informational, NOT an error
        payload = json.loads(r.output)
        assert payload["version"] == 1
        assert payload["applied"] == []
        assert payload["blocked"] == []
        assert [op["type"] for op in payload["skipped"]] == ["add_index"]
        assert payload["partial_failure"] is False
        # text mode: info line on stderr, still exit 0
        r2 = runner.invoke(app, ["push", "models4:metadata", "--safe-only", *DSN_ARG])
        assert r2.exit_code == 0
        assert "skipped by --safe-only" in r2.stderr
    finally:
        _drop(pg_engine, "cli_sk")


@pytest.mark.pg
def test_push_blocked_exit_1_and_quiet(tmp_path, monkeypatch, pg_engine):
    _drop(pg_engine, "cli_orph")
    with pg_engine.begin() as conn:
        conn.execute(text("CREATE TABLE cli_orph (id SERIAL PRIMARY KEY)"))
    try:
        monkeypatch.syspath_prepend(str(tmp_path))
        (tmp_path / "models5.py").write_text(
            "from sqlalchemy import MetaData\nmetadata = MetaData()\n"
        )
        r = runner.invoke(app, ["push", "models5:metadata", *DSN_ARG])
        assert r.exit_code == 1
        assert "blocked" in r.stderr
        # --quiet suppresses the advisory output, exit code unchanged
        rq = runner.invoke(app, ["push", "models5:metadata", "--quiet", *DSN_ARG])
        assert rq.exit_code == 1
        assert rq.stderr == ""
    finally:
        _drop(pg_engine, "cli_orph")


@pytest.mark.pg
def test_dsn_env_fallback_and_missing(tmp_path, monkeypatch, hero):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "models.py").write_text(
        "from sqlalchemy import Column, Integer, MetaData, Table\n"
        "metadata = MetaData()\n"
        "Table('cli_hero', metadata, Column('id', Integer, primary_key=True))\n"
    )
    monkeypatch.setenv("DATABASE_URL", DSN)
    r = runner.invoke(app, ["check", "models:metadata"])
    assert r.exit_code == 2  # env DSN reached the DB (drift detected)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r2 = runner.invoke(app, ["check", "models:metadata"])
    assert r2.exit_code == 1


# --- migrate/stamp verbs (0.4.2 hardening) ---------------------------------

CLI_SAFE_0001 = (
    "-- sqlpush: revision=0001 risk=SAFE\nCREATE TABLE cli_mt (id integer PRIMARY KEY);\n"
)


@pytest.fixture()
def cli_chain(pg_engine):
    def _clean() -> None:
        with pg_engine.begin() as conn:
            for t in ("sqlpush_versions", "cli_mt"):
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))

    _clean()
    yield pg_engine
    _clean()


@pytest.mark.pg
def test_cli_migrate_new_flags_parse_and_succeed(cli_chain, tmp_path):
    # --advisory-wait / --lock-timeout parse and route through; no
    # holder + an empty (existing) dir is a legitimate idle run -> 0
    tmp_path.mkdir(exist_ok=True)
    r = runner.invoke(
        app,
        [
            "migrate",
            "--dsn",
            DSN,
            "--dir",
            str(tmp_path),
            "--advisory-wait",
            "3",
            "--lock-timeout",
            "2",
        ],
    )
    assert r.exit_code == 0


@pytest.mark.pg
def test_cli_stamp_force_on_edited_file(cli_chain, tmp_path):
    # B4 via the verb: stamp, edit the file, re-stamp without --force ->
    # typed SqlpushError (exit 1 via main()'s fallback); with --force ->
    # exit 0 with the checksum refreshed
    (tmp_path / "0001_init.sql").write_text(CLI_SAFE_0001)
    r0 = runner.invoke(app, ["stamp", *DSN_ARG, "--dir", str(tmp_path)])
    assert r0.exit_code == 0
    (tmp_path / "0001_init.sql").write_text(CLI_SAFE_0001.replace("cli_mt", "cli_mt_ed"))
    r1 = runner.invoke(app, ["stamp", *DSN_ARG, "--dir", str(tmp_path)])
    assert r1.exit_code == 1
    assert isinstance(r1.exception, SqlpushError)
    assert "0001_init.sql" in str(r1.exception)
    r2 = runner.invoke(app, ["stamp", *DSN_ARG, "--dir", str(tmp_path), "--force"])
    assert r2.exit_code == 0


# --- 0.5.0 flags: --no-concurrently / --statement-timeout -------------------


def test_push_flags_route_to_api(tmp_path, monkeypatch):
    # flag → kwarg wiring (no live DB): --no-concurrently flips the api
    # default, --statement-timeout carries through; without the flags
    # the api defaults (concurrently=True, statement_timeout=None) hold
    _unit_models(tmp_path, monkeypatch)
    seen = {}

    def fake_push(*a, **k):
        seen.update(k)
        return Report()

    monkeypatch.setattr(sqlpush.cli.api, "push", fake_push)
    r = runner.invoke(
        app,
        [
            "push",
            "unit_models:metadata",
            "--no-concurrently",
            "--statement-timeout",
            "3.5",
            *UNIT_DSN,
        ],
    )
    assert r.exit_code == 0
    assert seen["concurrently"] is False
    assert seen["statement_timeout"] == 3.5

    seen.clear()
    r2 = runner.invoke(app, ["push", "unit_models:metadata", *UNIT_DSN])
    assert r2.exit_code == 0
    assert seen["concurrently"] is True
    assert seen["statement_timeout"] is None


def test_revision_no_concurrently_routes_to_api(tmp_path, monkeypatch):
    _unit_models(tmp_path, monkeypatch)
    seen = {}

    def fake_revision(*a, **k):
        seen.update(k)
        return Path("0001_x.sql")

    monkeypatch.setattr(sqlpush.cli.api, "revision", fake_revision)
    r = runner.invoke(
        app,
        ["revision", "unit_models:metadata", "--ref-dsn", UNIT_DSN[1], "--no-concurrently"],
    )
    assert r.exit_code == 0
    assert seen["concurrently"] is False


# --- unit tests below: api.push monkeypatched, no live PostgreSQL ---


def test_push_partial_failure_exits_2(tmp_path, monkeypatch):
    _unit_models(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sqlpush.cli.api,
        "push",
        lambda *a, **k: Report(
            applied=(AppliedOperation("create_table", "applied"),),
            partial_failure=True,
        ),
    )
    r = runner.invoke(app, ["push", "unit_models:metadata", *UNIT_DSN])
    assert r.exit_code == 2


def test_push_blocked_precedence_over_partial_failure(tmp_path, monkeypatch):
    # Contract: when a run is BOTH blocked and partially failed, blocked
    # wins; the actionable remediation is --allow-destructive (exit 1),
    # not the generic error exit 2. Pins the CLI's check order.
    _unit_models(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sqlpush.cli.api,
        "push",
        lambda *a, **k: Report(
            blocked=(PlannedOperation("drop_table", RiskClass.DESTRUCTIVE, "DROP TABLE x"),),
            partial_failure=True,
        ),
    )
    r = runner.invoke(app, ["push", "unit_models:metadata", *UNIT_DSN])
    assert r.exit_code == 1  # blocked precedes partial_failure
    assert "blocked" in r.stderr


UNREACHABLE_DSN = (
    # closed port on loopback: refused instantly; connect_timeout guards
    # against environments that DROP instead (would hang otherwise)
    "postgresql+psycopg://u:p@127.0.0.1:5999/nodb?connect_timeout=2"
)


def test_push_unreachable_dsn_exits_2(tmp_path, monkeypatch):
    # C1: push errors are exit 2 by contract: a connect failure must
    # surface as a typed message, never a raw SQLAlchemy traceback
    _unit_models(tmp_path, monkeypatch)
    r = runner.invoke(app, ["push", "unit_models:metadata", "--dsn", UNREACHABLE_DSN])
    assert r.exit_code == 2
    assert "could not connect" in r.stderr
    assert "Traceback" not in r.stderr


def test_diff_unreachable_dsn_exits_1(tmp_path, monkeypatch):
    # diff/check keep exit 1 for errors (contract). The typed boundary
    # is pinned via the escaping exception: a SqlpushError; main()'s
    # fallback prints it and exits 1 (see test_main_fallback_*), so a
    # raw SQLAlchemy error can never reach the user as a traceback.
    _unit_models(tmp_path, monkeypatch)
    r = runner.invoke(app, ["diff", "unit_models:metadata", "--dsn", UNREACHABLE_DSN])
    assert r.exit_code == 1
    assert isinstance(r.exception, SqlpushError)
    assert not isinstance(r.exception, OperationalError)


def test_check_unreachable_dsn_exits_1(tmp_path, monkeypatch):
    _unit_models(tmp_path, monkeypatch)
    r = runner.invoke(app, ["check", "unit_models:metadata", "--dsn", UNREACHABLE_DSN])
    assert r.exit_code == 1
    assert isinstance(r.exception, SqlpushError)
    assert not isinstance(r.exception, OperationalError)


def test_main_fallback_typed_error_exit_1(monkeypatch, capsys):
    # diff/check error contract lives in main(): typed error → red
    # message on stderr, exit 1, no traceback
    from sqlpush.types import ConnectFailed

    def boom():
        raise ConnectFailed("could not connect to the database: x")

    monkeypatch.setattr(sqlpush.cli, "app", boom)
    with pytest.raises(SystemExit) as ei:
        sqlpush.cli.main()
    assert ei.value.code == 1
    assert "could not connect" in capsys.readouterr().err


def test_malformed_dsn_exits_1(tmp_path, monkeypatch):
    # bad-DSN ArgumentError from create_engine is a config error: exit 1
    _unit_models(tmp_path, monkeypatch)
    r = runner.invoke(app, ["check", "unit_models:metadata", "--dsn", "not-a-dsn"])
    assert r.exit_code == 1
    assert "Traceback" not in r.stderr


def test_push_json_nonempty_shapes(tmp_path, monkeypatch):
    # The safe-only pg test only ever saw empty applied/blocked lists;
    # this pins the entry shapes with all three sections non-empty.
    # --quiet keeps stderr empty so r.output is pure JSON despite blocked.
    _unit_models(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sqlpush.cli.api,
        "push",
        lambda *a, **k: Report(
            applied=(AppliedOperation("create_table", "applied"),),
            blocked=(
                PlannedOperation(
                    "drop_table",
                    RiskClass.DESTRUCTIVE,
                    "DROP TABLE x",
                    table="x",
                ),
            ),
            skipped=(
                PlannedOperation(
                    "add_index",
                    RiskClass.RISKY,
                    "CREATE INDEX ix ON x (c)",
                    table="x",
                ),
            ),
        ),
    )
    r = runner.invoke(app, ["push", "unit_models:metadata", "--json", "--quiet", *UNIT_DSN])
    assert r.exit_code == 1  # blocked present
    payload = json.loads(r.output)
    assert payload["applied"] == [{"type": "create_table", "status": "applied"}]
    assert payload["blocked"][0]["type"] == "drop_table"
    assert payload["blocked"][0]["risk"] == "destructive"
    assert payload["skipped"][0]["type"] == "add_index"
    assert payload["skipped"][0]["risk"] == "risky"
    assert payload["partial_failure"] is False


def test_check_plans_once_verbose(tmp_path, monkeypatch):
    # I3 TOCTOU: check must plan ONCE and derive its CheckResult (and
    # --verbose output) from that single plan; api.check is not used
    # and api.plan is called exactly once.
    _unit_models(tmp_path, monkeypatch)
    calls = []

    def fake_plan(*a, **k):
        calls.append(k)
        return Plan(
            operations=(
                PlannedOperation(
                    "drop_table",
                    RiskClass.DESTRUCTIVE,
                    "DROP TABLE x",
                    table="x",
                ),
            )
        )

    monkeypatch.setattr(sqlpush.cli.api, "plan", fake_plan)
    monkeypatch.setattr(sqlpush.cli.api, "check", lambda *a, **k: pytest.fail("double-plan"))
    r = runner.invoke(app, ["check", "unit_models:metadata", "--verbose", *UNIT_DSN])
    assert r.exit_code == 3
    assert len(calls) == 1
    assert "drift: 1 operation(s), 1 destructive" in r.output


def test_check_json_single_plan(tmp_path, monkeypatch):
    _unit_models(tmp_path, monkeypatch)
    calls = []

    def fake_plan(*a, **k):
        calls.append(k)
        return Plan(
            operations=(
                PlannedOperation(
                    "add_column",
                    RiskClass.SAFE,
                    "ALTER TABLE t ADD COLUMN c INT",
                    table="t",
                    column="c",
                ),
            )
        )

    monkeypatch.setattr(sqlpush.cli.api, "plan", fake_plan)
    r = runner.invoke(app, ["check", "unit_models:metadata", "--json", *UNIT_DSN])
    assert r.exit_code == 2  # drift, non-destructive
    assert len(calls) == 1
    assert json.loads(r.output)["drift"] is True


def test_diff_verbose_risk_summary(tmp_path, monkeypatch):
    # --verbose prints an op-count line per PRESENT risk class
    _unit_models(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sqlpush.cli.api,
        "plan",
        lambda *a, **k: Plan(
            operations=(
                PlannedOperation(
                    "add_column",
                    RiskClass.SAFE,
                    "ALTER TABLE t ADD COLUMN c INT",
                    table="t",
                    column="c",
                ),
                PlannedOperation(
                    "add_index",
                    RiskClass.RISKY,
                    "CREATE INDEX ix ON t (c)",
                    table="t",
                ),
                PlannedOperation(
                    "drop_table",
                    RiskClass.DESTRUCTIVE,
                    "DROP TABLE x",
                    table="x",
                ),
            )
        ),
    )
    r = runner.invoke(app, ["diff", "unit_models:metadata", "--verbose", *UNIT_DSN])
    assert r.exit_code == 0
    assert "safe: 1 operation(s)" in r.output
    assert "risky: 1 operation(s)" in r.output
    assert "destructive: 1 operation(s)" in r.output
