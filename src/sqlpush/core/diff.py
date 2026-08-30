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
_SYSTEM_TABLES = ("alembic_version", "spatial_ref_sys")

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


def _make_include(schemas: frozenset[str], default_schema: str):
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
        if reflected and compare_to is None:
            # DB-only object: skip system tables. NB: alembic also
            # auto-excludes its own version table from autogen; the
            # name check here is defense in depth.
            return name not in _SYSTEM_TABLES
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
        if schemas is None:
            with engine.connect() as conn:
                search_path = conn.execute(text("SHOW search_path")).scalar()
                # a live PG session always reports a search_path
                assert search_path is not None
                schemas = [
                    s.strip()
                    for s in search_path.split(",")
                    if s.strip() and s.strip() != '"$user"'
                ]
        # New binding rather than a param reassignment: frozenset is a
        # Set, not a Sequence, and the helpers below declare the exact
        # type they take.
        schema_set = frozenset(schemas)
        exclude = tuple(exclude)
        # Typed `str | None` by SQLAlchemy; a dialect without a default
        # schema is meaningless for this PostgreSQL-only tool; "public"
        # matches every supported server config.
        default_schema = engine.dialect.default_schema_name or "public"

        opts = {
            "compare_type": True,
            "compare_server_default": True,
            "include_name": _make_include_name(schema_set, default_schema),
            "include_object": _make_include(schema_set, default_schema),
            "include_schemas": True,
        }
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts=opts)
            script = produce_migrations(ctx, metadata)

        # produce_migrations always builds the upgrade bundle
        assert script.upgrade_ops is not None
        ops: list[PlannedOperation] = []
        for op in _flatten(script.upgrade_ops.ops):
            ops.extend(self._translate(op, engine, exclude))
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
