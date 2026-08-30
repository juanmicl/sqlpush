# tests/conftest.py
from __future__ import annotations

import os

import pytest
import sqlalchemy
from sqlalchemy import create_engine, text

DSN = os.environ.get(
    "SQLPUSH_TEST_DSN",
    "postgresql+psycopg://sqlpush:sqlpush@localhost:5433/sqlpush_test",
)


def _db_available() -> bool:
    try:
        engine = create_engine(DSN, poolclass=sqlalchemy.pool.NullPool)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001  # availability probe must catch any failure
        return False


pg_available = _db_available()


def _timescale(engine: sqlalchemy.Engine) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).scalar()
        )


# memoized: the collection hook below would otherwise probe the server
# once per collected timescale-marked item
_timescale_cache: bool | None = None


def _timescale_cached() -> bool:
    global _timescale_cache
    if _timescale_cache is None:
        engine = create_engine(DSN, poolclass=sqlalchemy.pool.NullPool)
        try:
            _timescale_cache = _timescale(engine)
        finally:
            engine.dispose()
    return _timescale_cache


@pytest.fixture(scope="session")
def pg_engine():
    if not pg_available:
        pytest.skip("PostgreSQL not reachable at SQLPUSH_TEST_DSN")
    engine = create_engine(DSN, poolclass=sqlalchemy.pool.NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def is_timescale(pg_engine):
    return _timescale_cached()


def pytest_configure(config):
    config.addinivalue_line("markers", "pg: requires a live PostgreSQL")
    config.addinivalue_line("markers", "timescale: requires PostgreSQL with timescaledb")


def pytest_collection_modifyitems(config, items):
    skip_pg = pytest.mark.skip(reason="PostgreSQL not reachable")
    skip_ts = pytest.mark.skip(reason="timescaledb not installed")
    for item in items:
        if "pg" in item.keywords and not pg_available:
            item.add_marker(skip_pg)
        if "timescale" in item.keywords and (not pg_available or not _timescale_cached()):
            item.add_marker(skip_ts)
