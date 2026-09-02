# src/sqlpush/directives/timescale.py
from __future__ import annotations

from sqlalchemy import MetaData, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import ProgrammingError

from sqlpush.annotations import HYPERTABLE_KEY, create_hypertable_sql
from sqlpush.types import PlannedOperation, RiskClass


def _is_hypertable(conn: Connection, schema: str, table_name: str) -> bool:
    try:
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM timescaledb_information.hypertables "
                    "WHERE hypertable_schema = :schema AND hypertable_name = :name"
                ),
                {"schema": schema, "name": table_name},
            ).scalar()
        )
    except ProgrammingError:
        # timescaledb_information views exist only where the extension
        # is installed: on a non-timescale DB nothing can be a
        # hypertable. Emit the op and let apply surface the server's
        # own error: annotated models on non-timescale DBs are user
        # error, not a state probe failure.
        return False


def hypertable_operations(
    metadata: MetaData, engine: Engine | None = None
) -> list[PlannedOperation]:
    """Plan ``create_hypertable`` ops for ``@hypertable``-annotated tables.

    ``engine=None`` emits unconditionally (DB-free preview/tests keep
    the previous behavior). With ``engine``, a table already registered
    in ``timescaledb_information.hypertables`` emits nothing: push stays
    idempotent and ``check()`` reports clean on a synced annotated
    schema: directives are state-aware like the diff.
    """
    pending = [
        table for table in metadata.tables.values() if table.info.get(HYPERTABLE_KEY) is not None
    ]
    if engine is not None and pending:
        default_schema = engine.dialect.default_schema_name or "public"
        with engine.connect() as conn:
            pending = [
                t for t in pending if not _is_hypertable(conn, t.schema or default_schema, t.name)
            ]
    ops: list[PlannedOperation] = []
    for table in pending:
        info = table.info[HYPERTABLE_KEY]
        # SQL parity with the decorator's after_create listener is by
        # construction: both render through create_hypertable_sql (the
        # directive's render adds the statement terminator).
        ops.append(
            PlannedOperation(
                type="create_hypertable",
                risk=RiskClass.SAFE,
                sql=create_hypertable_sql(table.name, table.schema, info) + ";",
                table=table.name,
            )
        )
    return ops
