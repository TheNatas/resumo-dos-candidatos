"""Pytest setup: a throwaway database per test process, on the docker Postgres.

Schema is created from the ORM metadata (fast); each test runs against a truncated
DB for isolation. Requires `docker compose up -d` (Postgres on :5439).

The database name carries the **process id**. A single shared `resumo_test` breaks
the moment two pytest processes overlap — each one truncates every table and drops
the schema at session end, so a concurrent run sees its own tables vanish mid-test.
Set `RESUMO_TEST_DB` to pin a fixed name when you want to inspect the DB afterwards.
"""

from __future__ import annotations

import os

_TEST_DB = os.environ.get("RESUMO_TEST_DB") or f"resumo_test_{os.getpid()}"

# Point the app at the throwaway DB *before* importing resumo (settings are cached).
os.environ["RESUMO_DATABASE_URL"] = (
    f"postgresql+psycopg://resumo:resumo@localhost:5439/{_TEST_DB}"
)
os.environ["RESUMO_REQUEST_DELAY_SECONDS"] = "0"
# Tests build their own fixtures; a config-driven scope filter would silently drop
# rows a test just constructed. Collectors take explicit ufs=/cargo_codes= kwargs.
os.environ.setdefault("RESUMO_TARGET_UFS", "SC")
os.environ.setdefault("RESUMO_TARGET_CARGOS", "3,5,6,7")

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

import resumo.db.models  # noqa: E402,F401  (register tables)
from resumo.db.session import Base, get_engine, get_sessionmaker  # noqa: E402

ADMIN_URL = "postgresql+psycopg://resumo:resumo@localhost:5439/resumo"


@pytest.fixture(scope="session", autouse=True)
def _database():
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("select 1 from pg_database where datname = :n"), {"n": _TEST_DB}
        ).scalar()
        if not exists:
            conn.execute(text(f'create database "{_TEST_DB}"'))
    admin.dispose()

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("create extension if not exists unaccent"))
        conn.execute(text("create extension if not exists pg_trgm"))
    Base.metadata.create_all(engine)
    yield

    # Drop the whole database, not just the tables — otherwise every run leaves a
    # `resumo_test_<pid>` behind. Must disconnect first; you cannot drop a database
    # that still has open sessions.
    engine.dispose()
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'drop database if exists "{_TEST_DB}" with (force)'))
    admin.dispose()


@pytest.fixture
def session():
    s = get_sessionmaker()()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
        engine = get_engine()
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
