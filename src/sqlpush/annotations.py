# src/sqlpush/annotations.py
from __future__ import annotations

from dataclasses import dataclass

HYPERTABLE_KEY = "sqlpush_hypertable"


@dataclass(frozen=True)
class HypertableInfo:
    time_column: str
    chunk_time_interval: str | None = None


def hypertable(*, time_column: str, chunk_time_interval: str | None = None):
    """Record hypertable intent on the model's Table (MetaData level).

    Works with SQLModel, Flask-SQLAlchemy and plain declarative, anything
    whose class already carries a built ``__table__`` when decorated.

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
        table.info[HYPERTABLE_KEY] = HypertableInfo(
            time_column=time_column, chunk_time_interval=chunk_time_interval
        )
        return cls

    return decorator
