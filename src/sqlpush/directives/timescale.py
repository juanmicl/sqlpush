# src/sqlpush/directives/timescale.py
from __future__ import annotations

from sqlalchemy import MetaData, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import ProgrammingError

from sqlpush.annotations import HYPERTABLE_KEY
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


def _lit(value: str) -> str:
    # single-quoted SQL literal escaping: ' -> '' (defense in depth
    # against quote break-out via table/column names from user metadata)
    return value.replace("'", "''")


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
        # Schema-qualified relation: create_hypertable resolves an
        # unqualified name via the session search_path, so a table in a
        # non-default schema MUST carry its schema or the op lands on
        # public.<name> (UndefinedTable). Schema-less tables keep the
        # bare name: they live in the default schema, which the
        # search_path already resolves.
        relation = table.name if table.schema is None else f"{table.schema}.{table.name}"
        name = _lit(relation)
        time_column = _lit(info.time_column)
        parts = [f"SELECT create_hypertable('{name}', '{time_column}'"]
        if info.chunk_time_interval:
            parts.append(f", chunk_time_interval => INTERVAL '{_lit(info.chunk_time_interval)}'")
        parts.append(", migrate_data => true")
        # if_not_exists: race insurance between the state check above
        # and the apply. create_default_indexes=false: timescale's
        # implicit time-column index is invisible to metadata, so the
        # default would drift forever (destructive drop_index) on a
        # fully synced schema; users declare wanted indexes in the
        # Table instead (metadata is the source of truth).
        parts.append(", if_not_exists => true, create_default_indexes => false)")
        ops.append(
            PlannedOperation(
                type="create_hypertable",
                risk=RiskClass.SAFE,
                sql="".join(parts) + ";",
                table=table.name,
            )
        )
    return ops
