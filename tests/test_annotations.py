import pytest
import sqlalchemy
from sqlalchemy import Column, Integer, MetaData, Table

from sqlpush.annotations import HYPERTABLE_KEY, hypertable


def test_decorator_records_on_table_info():
    md = MetaData()

    class FakeMeta(type):
        pass

    # simulate a declarative/SQLModel class: decorator receives the class,
    # Table already exists
    table = Table("metrics", md, Column("ts", sqlalchemy.DateTime), Column("v", Integer))

    class Metrics:  # stands in for SQLModel(table=True)
        __table__ = table

    decorated = hypertable(time_column="ts", chunk_time_interval="1 day")(Metrics)
    assert decorated is Metrics
    info = table.info[HYPERTABLE_KEY]
    assert info.time_column == "ts"
    assert info.chunk_time_interval == "1 day"


def test_hypertable_without_table_raises():
    # decorating a class that has no __table__ is user error: TypeError,
    # never a silent no-op or an AttributeError later at plan time
    with pytest.raises(TypeError, match="__table__"):

        @hypertable(time_column="ts")
        class Plain:
            pass


def test_annotations_module_has_no_heavy_imports():
    import subprocess
    import sys

    # Subprocess isolation: earlier test files (test_advisory_lock,
    # test_diff) already import alembic into THIS process's sys.modules,
    # so the in-process assertion is unreliable in a shared pytest run.
    # A fresh interpreter starts with a clean sys.modules while still
    # resolving `sqlpush` from the project venv (sys.executable).
    code = (
        "import sys; import sqlpush.annotations; "
        "assert 'alembic' not in sys.modules; "
        "assert 'typer' not in sys.modules; "
        "assert 'psycopg' not in sys.modules; "
        "assert 'sqlalchemy' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
