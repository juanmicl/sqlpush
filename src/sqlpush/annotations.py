# src/sqlpush/annotations.py
from __future__ import annotations

from dataclasses import dataclass

HYPERTABLE_KEY = "sqlpush_hypertable"


@dataclass(frozen=True)
class HypertableInfo:
    time_column: str
    chunk_time_interval: str | None = None


def _lit(value: str) -> str:
    # single-quoted SQL literal escaping: ' -> '' (defense in depth
    # against quote break-out via table/column names from user metadata)
    return value.replace("'", "''")


def create_hypertable_sql(table_name: str, schema: str | None, info: HypertableInfo) -> str:
    """Render the idempotent ``create_hypertable`` statement for an annotated table.

    Single source of truth shared by the directive planner
    (``directives/timescale.py`` — push/revision) and the decorator's
    ``after_create`` listener (``create_all``): both mechanisms must
    always emit identical SQL.
    """
    # Schema-qualified relation: create_hypertable resolves an
    # unqualified name via the session search_path, so a table in a
    # non-default schema MUST carry its schema or the op lands on
    # public.<name> (UndefinedTable). Schema-less tables keep the
    # bare name: they live in the default schema, which the
    # search_path already resolves.
    relation = table_name if schema is None else f"{schema}.{table_name}"
    parts = [f"SELECT create_hypertable('{_lit(relation)}', '{_lit(info.time_column)}'"]
    if info.chunk_time_interval:
        parts.append(f", chunk_time_interval => INTERVAL '{_lit(info.chunk_time_interval)}'")
    # if_not_exists: race insurance between state checks and apply —
    # and what makes listener (create_all) + directive (push) coexistence
    # duplicate-free. migrate_data: required when the directive runs on an
    # existing populated table; a no-op on the empty table the listener
    # just created. create_default_indexes=false: timescale's implicit
    # time-column index is invisible to metadata, so the default would
    # drift forever (destructive drop_index) on a fully synced schema;
    # users declare wanted indexes in the Table instead (metadata is the
    # source of truth).
    parts.append(", migrate_data => true, if_not_exists => true, create_default_indexes => false)")
    return "".join(parts)


def _listen_after_create(table, info: HypertableInfo) -> None:
    # Dual-mechanism (0.4.1): besides the info entry (planned from
    # metadata on push/revision paths), register an after_create
    # listener so create_all paths register the hypertable too —
    # the same idempotent SQL the directive plans, executed via
    # text() exactly like the executor runs planned ops. With a
    # declared index on the time column, create_hypertable sees the
    # column indexed and create_default_indexes => false adds nothing
    # (see tests/test_annotations_createall.py).
    # Imports stay inside the function: this module must remain free of
    # heavy imports at module level (pinned by test_annotations.py).
    from sqlalchemy import event, text

    sql = create_hypertable_sql(table.name, table.schema, info)

    def _after_create(target, connection, **kw):
        connection.execute(text(sql))

    event.listen(table, "after_create", _after_create)


def hypertable(*, time_column: str, chunk_time_interval: str | None = None):
    """Record hypertable intent on the model's Table (dual-mechanism).

    Works with SQLModel, Flask-SQLAlchemy and plain declarative, anything
    whose class already carries a built ``__table__`` when decorated.

    Mechanism 1 (metadata): the annotation is recorded on ``Table.info``
    and planned as a ``create_hypertable`` op by push/revision.

    Mechanism 2 (create_all): an ``after_create`` listener runs the same
    idempotent ``create_hypertable``, so ``MetaData.create_all`` paths
    register the hypertable too; coexistence with a later push is
    duplicate-free (``if_not_exists``) and ``check()`` stays clean.

    The generated ``create_hypertable`` runs with
    ``create_default_indexes => false``: timescale's implicit time-column
    index is invisible to metadata and would drift forever. Declare it
    yourself (``Index(..., "<time_column>")`` on the model) if you rely
    on it for time-range scans.
    """

    def decorator(cls):
        table = getattr(cls, "__table__", None)
        if table is None:
            raise TypeError(f"{cls.__name__} has no __table__; decorate a mapped class")
        info = HypertableInfo(time_column=time_column, chunk_time_interval=chunk_time_interval)
        table.info[HYPERTABLE_KEY] = info
        _listen_after_create(table, info)
        return cls

    return decorator
