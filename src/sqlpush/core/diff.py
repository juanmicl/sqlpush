"""The ONLY module in sqlpush that imports alembic."""

from __future__ import annotations

import fnmatch
import io
from collections.abc import Sequence

from alembic.autogenerate import produce_migrations
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations.ops import AlterColumnOp, OpContainer
from sqlalchemy import MetaData, text
from sqlalchemy.engine import Engine

from sqlpush.core.classify import classify
from sqlpush.types import Plan, PlannedOperation

_SYSTEM_SCHEMAS = ("_timescaledb%", "information_schema", "pg_%")
# sqlpush_versions: the chain engine's own bookkeeping (migrate/stamp) —
# same treatment as alembic_version (C1): a public-scoped post-migrate
# check must be clean, not report its own registry as destructive drift
_SYSTEM_TABLES = ("alembic_version", "spatial_ref_sys", "sqlpush_versions")

# Leaf op class -> sqlpush op type. Class names follow alembic 1.19.1
# autogen output as observed in docs/notes/alembic-notes.md and probes:
# table create is CreateTableOp (not "AddTableOp"), column modify is
# AlterColumnOp (not "ModifyColumnOp"), index create is CreateIndexOp,
# and constraint creates are the concrete Create*ConstraintOp subclasses
# The AddConstraintOp base class is never emitted as a leaf op.
# AlterColumnOp is absent ON PURPOSE: its label is derived per-op (see
# _alter_column_label) because default-only, nullable-only and type
# changes all arrive as the same class.
_OP_TYPE = {
    "CreateTableOp": "add_table",
    "DropTableOp": "drop_table",
    "AddColumnOp": "add_column",
    "DropColumnOp": "drop_column",
    "CreateIndexOp": "add_index",
    "DropIndexOp": "drop_index",
    "CreateUniqueConstraintOp": "add_constraint",
    "CreateForeignKeyOp": "add_constraint",
    "CreatePrimaryKeyOp": "add_constraint",
    "CreateCheckConstraintOp": "add_constraint",
    "DropConstraintOp": "drop_constraint",
}


def _alter_column_label(op: AlterColumnOp) -> str:
    """Precise op type for AlterColumnOp (alembic-notes Pattern C).

    Sentinel semantics: ``False`` and ``None`` both mean "leave
    unchanged"; any other value is the new setting. Default-only and
    nullable-only drift arrive as AlterColumnOp just like type changes,
    so disambiguate on the attributes.
    """
    if op.modify_server_default not in (False, None):
        return "modify_default"
    if op.modify_nullable not in (False, None):
        return "modify_nullable"
    return "modify_type"


def _is_system_schema(schema: str) -> bool:
    return any(fnmatch.fnmatch(schema, pat) for pat in _SYSTEM_SCHEMAS)


def _extension_owned_schemas(conn) -> frozenset[str]:
    """Namespaces owned by an installed extension (live server truth).

    Extensions that install into their own namespace (e.g.
    postgis_topology -> ``topology``) manage it: its objects are
    extension state, not user metadata. NB: extensions relocated into
    the default schema (timescaledb, postgis, pg_trgm all live in
    ``public``) do NOT own it — the default schema stays user scope.
    """
    rows = conn.execute(
        text("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace")
    ).scalars()
    return frozenset(rows)


def _search_path_schemas(conn, default_schema: str, extension_schemas: frozenset[str]) -> list[str]:
    """Derive the diff scope from the live search_path.

    Same parsing as before (``"$user"`` dropped, whitespace stripped)
    with one exclusion: extension-owned non-default schemas never enter
    the scope. They usually appear on the search_path because their
    extension ``ALTER DATABASE``d it there at install time
    (postgis_topology does exactly that), not because the user scoped
    them in. A schema the caller passes explicitly via ``schemas=`` is
    never filtered here — explicit user intent wins.
    """
    # a live PG session always reports a search_path
    search_path = conn.execute(text("SHOW search_path")).scalar()
    assert search_path is not None
    candidates = [s.strip() for s in search_path.split(",") if s.strip() and s.strip() != '"$user"']
    return [s for s in candidates if s == default_schema or s not in extension_schemas]


def _timescale_auto_indexes(conn) -> dict[str, set[str]]:
    """``{schema.index_name}`` -> set of ``{schema.hypertable}`` owners.

    TimescaleDB's implicit per-hypertable time-column index
    (``<hypertable>_<dimension>_idx``). Set-valued: distinct hypertables
    can produce the SAME index name (table ``a`` dim ``b_c`` vs table
    ``a_b`` dim ``c`` both yield ``a_b_c_idx``) — a str value would
    last-win and leak the other as false drift. Qualified keys carry
    cross-schema hypertables. Empty when the extension is absent.

    Consumed twice in ``_make_include``: DB-only prunes (the reflected
    auto index with no metadata counterpart) and the S1 both-present
    sibling (a metadata-declared index with the same qualified name and
    column sequence compares EQUAL — the default's born-DESC ordering is
    extension state, not drift).
    """
    installed = conn.execute(
        text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
    ).first()
    if installed is None:
        return {}
    rows = conn.execute(
        text(
            "SELECT hypertable_schema, hypertable_name, primary_dimension "
            "FROM timescaledb_information.hypertables"
        )
    ).all()
    out: dict[str, set[str]] = {}
    for schema, hypertable, dimension in rows:
        out.setdefault(f"{schema}.{hypertable}_{dimension}_idx", set()).add(
            f"{schema}.{hypertable}"
        )
    return out


def _spatial_auto_indexes(conn) -> frozenset[str]:
    """Qualified names of geoalchemy2-style implicit spatial indexes.

    Single-column indexes named ``<table>_<col>_idx`` on a geometry or
    geography column — what geoalchemy2 (<0.18 or ``spatial_index=True``)
    creates at table-create time and no model ever declares. Catalog-driven
    (pg_type), so detection never depends on geoalchemy2 being importable
    in this process.
    """
    rows = conn.execute(
        text(
            "SELECT n.nspname, i2.relname "
            "FROM pg_index i "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey) "
            "JOIN pg_class i2 ON i2.oid = i.indexrelid "
            "JOIN pg_type ty ON ty.oid = a.atttypid "
            "WHERE i.indnatts = 1 "
            "AND ty.typname IN ('geometry', 'geography') "
            "AND i2.relname = format('%s_%s_idx', t.relname, a.attname)"
        )
    ).all()
    return frozenset(f"{nsp}.{idx}" for nsp, idx in rows)


def _restrict_search_path(conn, default_schema: str, schemas: frozenset[str]) -> str | None:
    """Confine the default-schema reflection pass to the default schema.

    Alembic's unqualified (None-schema) reflection pass resolves table
    names via ``pg_table_is_visible``, i.e. the SESSION search_path —
    which ambient database-level settings can stretch beyond the
    declared scope (postgis_topology ``ALTER DATABASE``s its own
    namespace onto the search_path; its tables then surface both
    unqualified and schema-qualified — one object, two drop ops).
    ``pg_table_is_visible`` cannot deliver "reflection sees exactly
    what the scope says" for a multi-schema scope: pinning the full
    scope list would let the None-pass resolve NON-default tables as
    unqualified default-schema tables — false destructive drops plus
    duplicate unqualified drops in mixed scopes. So pin to the default
    schema alone, and only when it is a scope member; otherwise the
    None-pass is filtered out by ``_make_include_name`` before
    reflection runs, so there is nothing visibility-based to confine
    and no pin (nor restore) happens. Schema-qualified reflection is
    unaffected either way. Returns the original setting for
    :func:`_restore_search_path`, or ``None`` when nothing was pinned.
    """
    if default_schema not in schemas:
        return None
    original = conn.execute(text("SHOW search_path")).scalar()
    assert original is not None
    preparer = conn.dialect.identifier_preparer
    conn.exec_driver_sql(f"SET search_path TO {preparer.quote(default_schema)}")
    return original


def _restore_search_path(conn, original: str | None) -> None:
    # original None => nothing was pinned, nothing to restore.
    if original is None:
        return
    # SET is session-level and survives ROLLBACK: a pooled connection
    # must never hand the restricted path to its next borrower. The
    # value is SHOW's own output — already a valid identifier list —
    # so round-tripping it verbatim is safe.
    try:
        conn.exec_driver_sql(f"SET search_path TO {original}")
    except Exception:  # noqa: BLE001
        # A dead connection cannot be restored, and a restore failure must
        # NEVER mask the root error from produce_migrations. A LIVE pooled
        # connection whose restore failed must be discarded, not returned
        # to the pool still carrying the restricted path (SET survives
        # ROLLBACK) — hence invalidate(), which forces the pool to drop
        # it transparently. Server-side session teardown drops the
        # restricted path anyway.
        conn.invalidate()


def _make_include_name(schemas: frozenset[str], default_schema: str):
    def include_name(name, type_, parent_names):
        # Prune schemas (and everything inside them) BEFORE reflection:
        # system catalogs (timescale et al.) are then never reflected at
        # all. Schema filtering routes through include_name: alembic
        # 1.19 never calls include_object with type_ == "schema", and
        # its return value must be a real bool (falsy = exclude).
        # NB: alembic substitutes None for the default schema name in
        # the schema pass (compare/schema.py), so map None back.
        if type_ == "schema":
            schema = default_schema if name is None else name
        else:
            schema = parent_names.get("schema_name") or default_schema
        return schema in schemas and not _is_system_schema(schema)

    return include_name


def _expr_name(e) -> str | None:
    """Best-effort column name of an Index expression, order-insensitive.

    Metadata-side expressions may be bare strings (``Index(..., "col")``)
    or Column objects; reflected-side ones arrive wrapped in
    ``UnaryExpression`` (the born-DESC/ASC ordering modifier). Unwrap
    wrapper elements (UnaryExpression/Label carry ``.element``), resolve
    strings and Columns to their names. Functions fall back to ``.name``
    — a sufficient equality proxy for the suppression check below.
    """
    if isinstance(e, str):
        return e
    inner = getattr(e, "element", None)
    if inner is not None:
        return _expr_name(inner)
    return getattr(e, "name", None)


def _make_include(
    schemas: frozenset[str],
    default_schema: str,
    ts_auto_indexes: dict[str, set[str]],
    spatial_auto_indexes: frozenset[str],
):
    def include_object(obj, name, type_, reflected, compare_to):
        # Bound the diff to the target schemas on every branch: derive
        # the object's effective schema and require membership, for
        # reflected-only, metadata-only and both-present objects alike.
        # Column/Index/Constraint objects have no `.schema` of their own
        # (SQLAlchemy 2.0.52): they resolve it through the parent table.
        schema = (
            getattr(obj, "schema", None)
            or getattr(getattr(obj, "table", None), "schema", None)
            or default_schema
        )
        if schema not in schemas or _is_system_schema(schema):
            return False
        parent = getattr(obj, "table", None)
        parent_table = getattr(parent, "name", None)
        parent_schema = getattr(parent, "schema", None) or default_schema
        qualified_index = f"{parent_schema}.{name}"
        qualified_parent = f"{parent_schema}.{parent_table}"
        if reflected and compare_to is None:
            # DB-only object: skip system tables. NB: alembic also
            # auto-excludes its own version table from autogen; the
            # name check here is defense in depth.
            if name in _SYSTEM_TABLES:
                return False
            # Skip TimescaleDB's implicit time-column index: extension
            # state no model declares. Qualified name AND owner set must
            # both match, so a same-named index on another table stays
            # real drift, as does one the metadata actually declares
            # (that arrives in the both-present branches below).
            if type_ == "index" and qualified_index in spatial_auto_indexes:
                return False
            owners = ts_auto_indexes.get(qualified_index, frozenset())
            return not (type_ == "index" and qualified_parent in owners)
        if type_ == "index" and not reflected and compare_to is not None:
            # S1 both-present sibling (atlas cycle-6): alembic gates the
            # drop+add PAIR for a "changed" index behind exactly ONE
            # include_object call (metadata index, reflected=False,
            # compare_to=reflected index). TimescaleDB's default time
            # index is born DESC (pg_index.indoption DESC bit); a
            # metadata-declared index with the same name, same owning
            # hypertable and the SAME column sequence is its metadata
            # stand-in — the sort-order difference is extension-birth
            # state, not drift. Column names compared ORDER-SENSITIVELY:
            # a genuinely different declaration (reorder, extra column)
            # still reports the pair.
            owners = ts_auto_indexes.get(qualified_index, frozenset())
            if qualified_parent in owners:
                m_cols = [_expr_name(e) for e in obj.expressions]
                r_cols = [_expr_name(e) for e in compare_to.expressions]
                if m_cols == r_cols:
                    return False
        return True

    return include_object


def _flatten(ops):
    # D1 (alembic-notes): ops targeting existing tables arrive wrapped in
    # ModifyTableOps containers; Operations.invoke crashes on containers,
    # so recurse into anything that is an OpContainer before invoking.
    for op in ops:
        if isinstance(op, OpContainer):
            yield from _flatten(op.ops)
        else:
            yield op


def _dedup_embedded_indexes(ops: list[PlannedOperation]) -> list[PlannedOperation]:
    """Drop standalone ``add_index`` ops already embedded in an ``add_table`` render.

    On alembic 1.19.1 ``CreateTableOp.from_table`` captures columns and
    constraints only, NOT indexes: a plain declared ``Index(...)`` on a
    new table never reaches the create render — it arrives
    standalone-only and is untouched here. The embedding this dedup
    targets happens when the table carries instrumentation-appended
    indexes (geoalchemy2-style listeners attaching at Table
    construction): ``to_table()`` reconstruction re-fires the
    attachment, the rebuilt table carries the index again, the offline
    create render embeds it, and autogen ALSO emits the standalone
    CreateIndexOp — executing both is a guaranteed duplicate-object
    failure (push fire-test F1/F2: the renders are byte-identical and
    the second execution collides with 42P07). Suppression side: the
    standalone op is the redundant one — its statement already runs
    inside the add_table op, whose render embeds it verbatim; the
    embedded copy has no other carrier op. Exact-statement containment
    is safe: both renders come from the same renderer over the same
    Index objects, so an embedded index matches its standalone op
    byte-for-byte while a different index's statement cannot be a
    substring of the create-table render (statement text runs to its
    own terminator). Known limitation: the keys are bare table names,
    so two NEW tables sharing a bare name across schemas under-dedup
    (last-wins in the dict) — containment is SQL-qualified either way,
    so no wrong suppression is possible.
    """
    table_renders = {op.table: op.sql for op in ops if op.type == "add_table"}
    return [
        op
        for op in ops
        if not (
            op.type == "add_index"
            and op.table in table_renders
            and op.sql.strip() in table_renders[op.table]
        )
    ]


def _split_statements(sql: str) -> list[str]:
    """Split a multi-statement op render on top-level ``;`` only.

    Quote-aware: ``;`` inside single-quoted SQL literals (enum labels
    can carry them) never splits; ``''`` escapes are consumed in pairs.
    """
    parts: list[str] = []
    buf: list[str] = []
    in_quote = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            if in_quote and i + 1 < len(sql) and sql[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_quote = not in_quote
            buf.append(ch)
        elif ch == ";" and not in_quote:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


def _dedup_enum_types(ops: list[PlannedOperation]) -> list[PlannedOperation]:
    """Emit each native enum's ``CREATE/DROP TYPE`` once across the plan (S2).

    SQLAlchemy's offline render embeds a native enum's DDL inside EVERY
    table op that references it (per-invoke create/drop memos — unlike
    metadata-level ``create_all``, which memoizes per metadata), so two
    tables sharing a Python enum each carry a verbatim copy of the
    ``CREATE TYPE`` in their add_table render; executing both is a
    guaranteed DuplicateObject (atlas cycle-6 finding S2 — the enum
    sibling of F1/F2's embedded-index dedup, but statement-level and
    cross-table).

    Statement-level: only VERBATIM duplicates (whitespace-normalized)
    are dropped — later byte-identical copies leave their op, the first
    copy stays embedded in its original add_table render. A metadata
    that defines one type name two DIFFERENT ways keeps both statements
    and still fails loudly at apply time: silent first-win would mask a
    genuine contradiction. Ops whose statements all survive pass
    through as the ORIGINAL object (identity) — a re-joined copy of
    unchanged SQL buys nothing.
    """
    seen: set[str] = set()

    def _is_type_stmt(norm: str) -> bool:
        low = norm.lower()
        return low.startswith(("create type ", "drop type "))

    out: list[PlannedOperation] = []
    for op in ops:
        if "type" not in op.sql.lower() or ";" not in op.sql:
            out.append(op)
            continue
        stmts = _split_statements(op.sql)
        kept: list[str] = []
        dropped = False
        for stmt in stmts:
            norm = " ".join(stmt.split())
            if _is_type_stmt(norm):
                if norm in seen:
                    dropped = True
                    continue
                seen.add(norm)
            if norm:
                kept.append(stmt)
        if not kept:
            # unreachable for table ops (their CREATE/DROP TABLE always
            # survives); guard anyway — an empty op.sql is worse than
            # the duplicate it replaced
            continue
        if not dropped:
            out.append(op)
            continue
        out.append(
            PlannedOperation(
                type=op.type,
                risk=op.risk,
                sql=";\n\n".join(stmt.strip() for stmt in kept if stmt.strip()),
                table=op.table,
                column=op.column,
            )
        )
    return out


def _render_op_sql(op, engine: Engine) -> str:
    buf = io.StringIO()
    offline = MigrationContext.configure(
        dialect=engine.dialect, opts={"as_sql": True, "output_buffer": buf}
    )
    operations = Operations(offline)
    operations.invoke(op)
    # offline render terminates each op with ";\n\n"; normalize so each
    # PlannedOperation.sql is a single clean statement
    return buf.getvalue().strip().rstrip(";")


class DiffEngine:
    def plan(
        self,
        metadata: MetaData,
        engine: Engine,
        *,
        schemas: Sequence[str] | None = None,
        exclude: Sequence[str] = (),
    ) -> Plan:
        exclude = tuple(exclude)
        # Typed `str | None` by SQLAlchemy; a dialect without a default
        # schema is meaningless for this PostgreSQL-only tool; "public"
        # matches every supported server config.
        default_schema = engine.dialect.default_schema_name or "public"

        # One connection for the whole plan: the scope derivation, the
        # catalog probes and the reflection must all see the same
        # session — the search_path pinning below would be meaningless
        # on a second connection.
        with engine.connect() as conn:
            if schemas is None:
                schemas = _search_path_schemas(conn, default_schema, _extension_owned_schemas(conn))
            # New binding rather than a param reassignment: frozenset is
            # a Set, not a Sequence, and the helpers below declare the
            # exact type they take.
            schema_set = frozenset(schemas)
            ts_auto_indexes = _timescale_auto_indexes(conn)
            spatial_auto_indexes = _spatial_auto_indexes(conn)

            opts = {
                "compare_type": True,
                "compare_server_default": True,
                "include_name": _make_include_name(schema_set, default_schema),
                "include_object": _make_include(
                    schema_set, default_schema, ts_auto_indexes, spatial_auto_indexes
                ),
                "include_schemas": True,
            }
            original_search_path = _restrict_search_path(conn, default_schema, schema_set)
            try:
                ctx = MigrationContext.configure(conn, opts=opts)
                script = produce_migrations(ctx, metadata)
            finally:
                _restore_search_path(conn, original_search_path)

        # produce_migrations always builds the upgrade bundle
        assert script.upgrade_ops is not None
        ops: list[PlannedOperation] = []
        for op in _flatten(script.upgrade_ops.ops):
            ops.extend(self._translate(op, engine, exclude))
        ops = _dedup_embedded_indexes(ops)
        ops = _dedup_enum_types(ops)
        return Plan(operations=tuple(ops))

    def _translate(self, op, engine: Engine, exclude: tuple[str, ...]) -> list[PlannedOperation]:
        op_type = _OP_TYPE.get(type(op).__name__, "raw_sql")
        if isinstance(op, AlterColumnOp):
            op_type = _alter_column_label(op)
        sql_text = _render_op_sql(op, engine)
        table = getattr(getattr(op, "table", None), "name", None) or getattr(op, "table_name", None)
        # AddColumnOp carries a real Column in `.column`; DropColumnOp and
        # AlterColumnOp only expose the plain string `column_name`
        # (alembic-notes op reference).
        column = getattr(getattr(op, "column", None), "name", None) or getattr(
            op, "column_name", None
        )
        # table-level patterns reach column-level ops too; a qualified
        # table.column pattern suppresses only that specific column op
        if table and any(fnmatch.fnmatch(table, pat) for pat in exclude):
            return []
        if table and column:
            full = f"{table}.{column}"
            if any(fnmatch.fnmatch(full, pat) for pat in exclude):
                return []
        return [
            PlannedOperation(
                type=op_type,
                risk=classify(op_type),
                sql=sql_text,
                table=table,
                column=column,
            )
        ]
