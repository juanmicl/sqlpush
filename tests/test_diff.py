from __future__ import annotations

import pytest
from sqlalchemy import Column, DateTime, Index, Integer, MetaData, String, Table, event, text

from sqlpush.core.diff import DiffEngine
from sqlpush.types import RiskClass

pytestmark = pytest.mark.pg


@pytest.fixture()
def clean_db(pg_engine):
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS hero"))
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        conn.execute(text("DROP SCHEMA IF EXISTS other CASCADE"))
    return pg_engine


@pytest.fixture()
def other_schema(clean_db):
    with clean_db.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS other"))
    yield clean_db
    with clean_db.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS other CASCADE"))


def _md() -> MetaData:
    md = MetaData()
    Table("hero", md, Column("id", Integer, primary_key=True), Column("name", String(50)))
    return md


def test_clean_when_in_sync(clean_db):
    _md().create_all(clean_db)
    plan = DiffEngine().plan(_md(), clean_db)
    assert plan.drift is False


def test_add_column_detected(clean_db):
    _md().create_all(clean_db)
    # rebuild with the extra column properly
    md3 = MetaData()
    Table(
        "hero",
        md3,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        Column("age", Integer, nullable=True),
    )
    plan = DiffEngine().plan(md3, clean_db)
    assert plan.drift is True
    assert any(op.type == "add_column" and op.risk is RiskClass.SAFE for op in plan.operations)


def test_drop_column_gated_destructive(clean_db):
    _md().create_all(clean_db)
    plan = DiffEngine().plan(_minimal_without_name(), clean_db)
    # DropColumnOp has no `.column` attribute; the plain-string
    # column_name extraction path must still yield the column
    assert any(op.risk is RiskClass.DESTRUCTIVE and op.column == "name" for op in plan.operations)


def _minimal_without_name() -> MetaData:
    md = MetaData()
    Table("hero", md, Column("id", Integer, primary_key=True))
    return md


def test_server_default_drift_detected(clean_db):
    md = MetaData()
    Table(
        "hero",
        md,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), server_default=text("'anon'")),
    )
    md.create_all(clean_db)
    md2 = MetaData()
    Table("hero", md2, Column("id", Integer, primary_key=True), Column("name", String(50)))
    plan = DiffEngine().plan(md2, clean_db)
    assert plan.drift is True  # default removal visible, drift contract


def test_alembic_version_and_system_schemas_ignored(clean_db):
    with clean_db.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
    plan = DiffEngine().plan(_md(), clean_db)
    # hero-create is the only expected op, which proves no catalog leakage
    assert plan.operations and all(op.table == "hero" for op in plan.operations)


def test_exclude_pattern(clean_db):
    _md().create_all(clean_db)
    md3 = MetaData()
    Table(
        "hero",
        md3,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        Column("age", Integer),
    )
    plan = DiffEngine().plan(md3, clean_db, exclude=("hero.age",))
    # hero.age was the only drift; excluding it empties the plan
    assert plan.operations == ()


def test_exclude_table_pattern_reaches_column_ops(clean_db):
    _md().create_all(clean_db)
    md3 = MetaData()
    Table(
        "hero",
        md3,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        Column("age", Integer),
    )
    plan = DiffEngine().plan(md3, clean_db, exclude=("hero",))
    assert plan.operations == ()


def test_metadata_table_outside_target_schemas_ignored(other_schema):
    md = MetaData()
    Table(
        "hero",
        md,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        schema="other",
    )
    # default scope is the search path (public): a table declared under
    # schema "other" must not be planned: neither create nor drop
    plan = DiffEngine().plan(md, other_schema)
    assert plan.operations == ()


def test_column_drift_in_non_default_schema_planned(other_schema):
    md = MetaData()
    Table("hero", md, Column("id", Integer, primary_key=True), schema="other")
    md.create_all(other_schema)
    md3 = MetaData()
    Table(
        "hero", md3, Column("id", Integer, primary_key=True), Column("age", Integer), schema="other"
    )
    # scope excludes the dialect default: column drift on a table inside
    # the scoped schema must still be planned (child objects resolve
    # their schema through the parent table)
    plan = DiffEngine().plan(md3, other_schema, schemas=("other",))
    assert plan.drift is True
    assert any(op.type == "add_column" and op.column == "age" for op in plan.operations)


def test_mixed_scope_in_sync(other_schema):
    # Regression pin for the search_path blocker: with a mixed scope
    # (public + a non-default schema) the old full-scope pin let the
    # None-schema pass resolve other.hero as a public table via
    # pg_table_is_visible → false destructive DROP TABLE on an
    # in-sync DB. Pinning the session to the default schema only must
    # keep a mixed in-sync scope clean.
    md = MetaData()
    Table(
        "hero",
        md,
        Column("id", Integer, primary_key=True),
        Column("name", String(50)),
        schema="other",
    )
    md.create_all(other_schema)
    plan = DiffEngine().plan(md, other_schema, schemas=("public", "other"))
    assert plan.operations == ()
    assert plan.drift is False


def test_mixed_scope_db_only_single_qualified_drop(other_schema):
    # DB-only table in the scoped non-default schema: exactly ONE op,
    # schema-qualified. The None-schema pass must not also resolve it
    # as an unqualified default-schema duplicate drop.
    with other_schema.begin() as conn:
        conn.execute(text("CREATE TABLE other.orphan (id integer PRIMARY KEY)"))
    plan = DiffEngine().plan(MetaData(), other_schema, schemas=("public", "other"))
    assert len(plan.operations) == 1
    drop = plan.operations[0]
    assert drop.type == "drop_table"
    assert drop.sql == "DROP TABLE other.orphan"


@pytest.fixture()
def extension_schema_db(clean_db):
    """pg_trgm installed into its own namespace with a table in it, and
    the database search_path stretched over that namespace — the exact
    shape postgis_topology produces on a production DB (it ALTER
    DATABASEs ``topology`` onto the search_path at install time)."""
    dbname = clean_db.url.database
    with clean_db.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS extown.tbl"))
        conn.execute(text("DROP EXTENSION IF EXISTS pg_trgm"))
        conn.execute(text("DROP SCHEMA IF EXISTS extown"))
        conn.execute(text("CREATE SCHEMA extown"))
        conn.execute(text("CREATE EXTENSION pg_trgm WITH SCHEMA extown"))
        conn.execute(text("CREATE TABLE extown.tbl (id integer PRIMARY KEY)"))
        conn.execute(text(f'ALTER DATABASE "{dbname}" SET search_path TO public, extown'))
    yield clean_db
    with clean_db.begin() as conn:
        conn.execute(text(f'ALTER DATABASE "{dbname}" RESET search_path'))
        conn.execute(text("DROP EXTENSION IF EXISTS pg_trgm"))
        conn.execute(text("DROP SCHEMA IF EXISTS extown CASCADE"))


def test_extension_owned_schema_never_derives_into_scope(extension_schema_db):
    # Derived scope must drop the extension-owned namespace (pg_extension
    # says extown belongs to pg_trgm): its table is extension scope, so
    # no drop ops for tbl — neither the schema-qualified one nor the
    # unqualified duplicate the search_path visibility would produce.
    # NullPool: plan()'s connection is brand new and sees the DB-level
    # search_path set by the fixture. (hero-create is expected: _md()
    # declares it and the DB does not have it.)
    plan = DiffEngine().plan(_md(), extension_schema_db)
    assert all(op.table != "tbl" for op in plan.operations)
    assert all(op.type != "drop_table" for op in plan.operations)

    # Control, same layout minus the ownership: dropping the extension
    # leaves schema + table intact, extown stays on the search_path and
    # enters the derived scope, so tbl is real DB-only drift again.
    with extension_schema_db.begin() as conn:
        conn.execute(text("DROP EXTENSION pg_trgm"))
    plan = DiffEngine().plan(_md(), extension_schema_db)
    assert any(op.type == "drop_table" and op.table == "tbl" for op in plan.operations)


def test_explicit_schema_scope_includes_extension_owned(extension_schema_db):
    # Explicit user intent wins over extension-ownership pruning: extown
    # handed in via schemas= keeps its DB-only table in the plan — as a
    # single schema-qualified drop (the session stays pinned to the
    # default schema, so the unqualified visibility duplicate cannot
    # appear either).
    plan = DiffEngine().plan(MetaData(), extension_schema_db, schemas=("public", "extown"))
    drops = [op for op in plan.operations if op.type == "drop_table"]
    assert len(drops) == 1
    assert drops[0].sql == "DROP TABLE extown.tbl"


@pytest.mark.timescale
def test_timescale_auto_time_index_not_drift(pg_engine):
    # create_hypertable leaves an implicit <table>_<dimension>_idx on
    # the parent table; models never declare it, so a matching metadata
    # must plan clean — the auto-index is extension state, not drift.
    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tsauto"))
        conn.execute(
            text("CREATE TABLE tsauto (id integer, ts timestamptz NOT NULL, PRIMARY KEY (id, ts))")
        )
        conn.execute(text("SELECT create_hypertable('tsauto', 'ts')"))
    try:
        # sanity: the implicit index really exists — the prune must not
        # pass vacuously
        with pg_engine.connect() as conn:
            auto_idx = conn.execute(
                text(
                    "SELECT 1 FROM pg_indexes "
                    "WHERE schemaname = 'public' AND indexname = 'tsauto_ts_idx'"
                )
            ).first()
        assert auto_idx is not None

        md = MetaData()
        Table(
            "tsauto",
            md,
            Column("id", Integer, primary_key=True),
            Column("ts", DateTime(timezone=True), nullable=False, primary_key=True),
        )
        plan = DiffEngine().plan(md, pg_engine)
        assert plan.drift is False
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS tsauto"))


@pytest.mark.timescale
def test_timescale_auto_index_same_name_collision_not_drift(pg_engine):
    # Dos hypertables producen el MISMO nombre de índice auto: a(b_c) y
    # a_b(c) → ambas dejan a_b_c_idx. En un MISMO schema PostgreSQL no
    # permite dos índices con el mismo nombre (sufija el segundo con
    # a_b_c_idx1), pero en schemas distintos ambos conservan el nombre
    # exacto — esa es la colisión física real. Ambas deben podarse
    # (mapa unqualified str-last-wins dejaría una como falso drift).
    with pg_engine.begin() as conn:
        for schema in ("s1", "s2"):
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(
            text("CREATE TABLE s1.a (id integer, b_c timestamptz NOT NULL, PRIMARY KEY (id, b_c))")
        )
        conn.execute(
            text("CREATE TABLE s2.a_b (id integer, c timestamptz NOT NULL, PRIMARY KEY (id, c))")
        )
        conn.execute(text("SELECT create_hypertable('s1.a', 'b_c')"))
        conn.execute(text("SELECT create_hypertable('s2.a_b', 'c')"))
    try:
        with pg_engine.connect() as conn:
            both = conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes WHERE indexname = 'a_b_c_idx' "
                    "AND schemaname IN ('s1', 's2')"
                )
            ).scalar()
        assert both == 2  # sanity: ambas existen (non-vacuous)

        md = MetaData()
        Table(
            "a",
            md,
            Column("id", Integer, primary_key=True),
            Column("b_c", DateTime(timezone=True), nullable=False, primary_key=True),
            schema="s1",
        )
        Table(
            "a_b",
            md,
            Column("id", Integer, primary_key=True),
            Column("c", DateTime(timezone=True), nullable=False, primary_key=True),
            schema="s2",
        )
        plan = DiffEngine().plan(md, pg_engine, schemas=("s1", "s2"))
        assert plan.drift is False
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS s1 CASCADE"))
            conn.execute(text("DROP SCHEMA IF EXISTS s2 CASCADE"))


@pytest.fixture()
def spatial_db(clean_db):
    """Una columna geometry con su índice GiST implícito geoalchemy2-style
    (<table>_<col>_idx), MÁS el control: mismo nombre de índice sobre una
    columna integer — el prune debe key-ar en el TIPO, no en el nombre."""
    with clean_db.begin() as conn:
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        except Exception:  # noqa: BLE001  # availability probe must catch any failure
            pytest.skip("postgis no disponible en este container")
        for tbl in ("geomtbl", "ctrltbl"):
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
        conn.execute(
            text("CREATE TABLE geomtbl (id integer PRIMARY KEY, geom geometry(Point, 4326))")
        )
        conn.execute(text("CREATE INDEX geomtbl_geom_idx ON geomtbl USING GIST (geom)"))
        conn.execute(text("CREATE TABLE ctrltbl (id integer PRIMARY KEY, geom integer)"))
        conn.execute(text("CREATE INDEX ctrltbl_geom_idx ON ctrltbl (geom)"))
    yield clean_db
    with clean_db.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS geomtbl"))
        conn.execute(text("DROP TABLE IF EXISTS ctrltbl"))


@pytest.mark.filterwarnings("ignore:Did not recognize type")
def test_spatial_auto_index_not_drift(spatial_db):
    md = MetaData()
    Table("geomtbl", md, Column("id", Integer, primary_key=True))
    plan = DiffEngine().plan(md, spatial_db)
    assert all(not (op.type == "drop_index" and op.table == "geomtbl") for op in plan.operations), (
        "el índice GiST implícito sobre geometry debe podarse"
    )


@pytest.mark.filterwarnings("ignore:Did not recognize type")
def test_spatial_auto_index_control_non_geometry_still_drift(spatial_db):
    md = MetaData()
    Table("ctrltbl", md, Column("id", Integer, primary_key=True))
    plan = DiffEngine().plan(md, spatial_db)
    assert any(op.type == "drop_index" and op.table == "ctrltbl" for op in plan.operations), (
        "mismo nombre sobre columna NO geometry = drift real (control)"
    )


@pytest.mark.timescale
@pytest.mark.xfail(
    strict=True,
    reason="suffix leak: PG renames the second same-name auto index to a_b_c_idx1, "
    "which the exact-name prune cannot see (documented limitation, "
    "follow-up: suffix-aware matcher)",
)
def test_same_schema_auto_index_suffix_leak_is_known_limitation(pg_engine):
    with pg_engine.begin() as conn:
        for tbl in ("a", "a_b"):
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
        conn.execute(
            text("CREATE TABLE a (id integer, b_c timestamptz NOT NULL, PRIMARY KEY (id, b_c))")
        )
        conn.execute(
            text("CREATE TABLE a_b (id integer, c timestamptz NOT NULL, PRIMARY KEY (id, c))")
        )
        conn.execute(text("SELECT create_hypertable('a', 'b_c')"))
        conn.execute(text("SELECT create_hypertable('a_b', 'c')"))
    try:
        md = MetaData()
        Table(
            "a",
            md,
            Column("id", Integer, primary_key=True),
            Column("b_c", DateTime(timezone=True), nullable=False, primary_key=True),
        )
        Table(
            "a_b",
            md,
            Column("id", Integer, primary_key=True),
            Column("c", DateTime(timezone=True), nullable=False, primary_key=True),
        )
        plan = DiffEngine().plan(md, pg_engine)
        assert plan.drift is False  # XPASS would mean the limitation is gone — update the xfail
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS a"))
            conn.execute(text("DROP TABLE IF EXISTS a_b"))


def test_declared_index_renders_once(clean_db):
    # Plain declared Index on a NEW table: on alembic 1.19.1
    # CreateTableOp.from_table captures columns+constraints only, so the
    # index is NOT embedded in the add_table render — it arrives as a
    # single standalone add_index op and the embedded-index dedup is a
    # no-op here. This pins the standalone-only path (and would stay
    # green with the dedup deleted); the actual embedded-duplicate
    # mechanism is pinned by the instrumentation tests below.
    md = MetaData()
    Table(
        "indexed",
        md,
        Column("id", Integer, primary_key=True),
        Column("email", String(50)),
        Index("ix_indexed_email", "email"),
    )
    plan = DiffEngine().plan(md, clean_db)
    occurrences = sum("CREATE INDEX ix_indexed_email" in op.sql for op in plan.operations)
    # standalone-only: exactly one render, no embedded copy to dedup
    assert occurrences == 1


def test_existing_table_index_not_deduped(clean_db):
    # The dedup must ONLY suppress add_index ops whose statement is already
    # embedded in an add_table render (same table). An index added to an
    # EXISTING table (the add_column-style path) has no add_table op to
    # nest inside — its standalone op must survive.
    md = MetaData()
    Table(
        "indexed2",
        md,
        Column("id", Integer, primary_key=True),
        Column("email", String(50)),
    )
    md.create_all(clean_db)
    try:
        md2 = MetaData()
        Table(
            "indexed2",
            md2,
            Column("id", Integer, primary_key=True),
            Column("email", String(50)),
            Index("ix_indexed2_email", "email"),
        )
        plan = DiffEngine().plan(md2, clean_db)
        standalone = [op for op in plan.operations if op.type == "add_index"]
        assert [op.table for op in standalone] == ["indexed2"]
    finally:
        with clean_db.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS indexed2"))


def _instrumented_geo_index(target, parent):
    # mimic geoalchemy2 <0.18 / spatial_index=True: the implicit spatial
    # index is appended by a listener at Table construction time. The
    # guard keys on the column name, so unrelated (and reflected)
    # tables pass through untouched.
    if "geo_col" in target.c:
        Index(f"{target.name}_geo_col_idx", target.c.geo_col)


def test_instrumented_index_renders_once(clean_db):
    # The REAL duplication mechanism (fire-test F1/F2): on alembic
    # 1.19.1 CreateTableOp.from_table captures columns+constraints only,
    # so a plain declared Index renders standalone-only — but an index
    # appended by construction-time instrumentation re-attaches when
    # to_table() rebuilds the table (after_parent_attach fires again),
    # the offline add_table render embeds it, AND autogen emits the
    # standalone CreateIndexOp: byte-identical statements, twice. The
    # plan must carry the statement exactly once (the dedup drops the
    # standalone copy). Without the dedup this fails with
    # occurrences == 2 — the declared-index tests above stay green even
    # with the dedup deleted, so they cannot pin it.
    event.listen(Table, "after_parent_attach", _instrumented_geo_index)
    try:
        md = MetaData()
        Table(
            "geotbl",
            md,
            Column("id", Integer, primary_key=True),
            Column("geo_col", String(30)),
        )
        plan = DiffEngine().plan(md, clean_db)
    finally:
        event.remove(Table, "after_parent_attach", _instrumented_geo_index)
    occurrences = sum("CREATE INDEX geotbl_geo_col_idx" in op.sql for op in plan.operations)
    assert occurrences == 1
    # the standalone copy is the suppressed one; the embedded copy rides
    # inside the add_table render
    assert not any(op.type == "add_index" and op.table == "geotbl" for op in plan.operations)


def test_instrumented_index_renders_once_schema_qual(other_schema):
    # Same mechanism on a schema'd table: both ops carry the BARE table
    # name in op.table while their SQL is schema-qualified — this pins
    # the dedup's cross-schema key match (bare-name key + qualified-SQL
    # containment must still collapse the pair to one occurrence).
    event.listen(Table, "after_parent_attach", _instrumented_geo_index)
    try:
        md = MetaData()
        Table(
            "geotbl",
            md,
            Column("id", Integer, primary_key=True),
            Column("geo_col", String(30)),
            schema="other",
        )
        plan = DiffEngine().plan(md, other_schema, schemas=("public", "other"))
    finally:
        event.remove(Table, "after_parent_attach", _instrumented_geo_index)
    occurrences = sum("CREATE INDEX geotbl_geo_col_idx" in op.sql for op in plan.operations)
    assert occurrences == 1
    # the qualified add_table render (with the embedded index) survives;
    # the standalone qualified copy is gone
    assert any(
        op.type == "add_table" and "CREATE TABLE other.geotbl" in op.sql for op in plan.operations
    )
    assert not any(op.type == "add_index" and op.table == "geotbl" for op in plan.operations)


@pytest.mark.timescale
def test_timescale_born_time_index_equals_declared(pg_engine):
    # S1 (atlas cycle-6): dev-era create_hypertable defaults are born DESC
    # (pg_index.indoption DESC bit; column_sorting in reflection). A
    # metadata index declared with the SAME name and SAME column set but
    # plain ASC must compare EQUAL — the pair (drop+add) was pure
    # timescale-birth noise, the both-present sibling of the DB-only
    # auto-index prune.
    from sqlalchemy import Index

    with pg_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tsborn"))
        conn.execute(
            text("CREATE TABLE tsborn (id integer, ts timestamptz NOT NULL, PRIMARY KEY (id, ts))")
        )
        conn.execute(text("SELECT create_hypertable('tsborn', 'ts')"))
    try:
        with pg_engine.connect() as conn:
            idxdef = conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'tsborn_ts_idx'")
            ).scalar()
        # sanity: the birth property this test pins — timescale's default
        # time index carries DESC ordering
        assert idxdef is not None and "DESC" in idxdef, idxdef

        md = MetaData()
        Table(
            "tsborn",
            md,
            Column("id", Integer, primary_key=True),
            Column("ts", DateTime(timezone=True), nullable=False, primary_key=True),
            Index("tsborn_ts_idx", "ts"),  # declared plain (ASC)
        )
        plan = DiffEngine().plan(md, pg_engine)
        assert plan.drift is False, [f"{op.type} {op.table}: {op.sql}" for op in plan.operations]
    finally:
        with pg_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS tsborn"))
