"""Pytest setup: a dedicated `resumo_test` database on the docker Postgres.

Schema is created from the ORM metadata (fast); each test runs against a truncated
DB for isolation. Requires `docker compose up -d` (Postgres on :5435).
"""

from __future__ import annotations

import os

# Point the app at a throwaway test DB *before* importing resumo (settings are cached).
os.environ["RESUMO_DATABASE_URL"] = "postgresql+psycopg://resumo:resumo@localhost:5435/resumo_test"
os.environ["RESUMO_REQUEST_DELAY_SECONDS"] = "0"

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

import resumo.db.models  # noqa: E402,F401  (register tables)
from resumo.db.session import Base, get_engine, get_sessionmaker  # noqa: E402

ADMIN_URL = "postgresql+psycopg://resumo:resumo@localhost:5435/resumo"


@pytest.fixture(scope="session", autouse=True)
def _database():
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("select 1 from pg_database where datname = 'resumo_test'")
        ).scalar()
        if not exists:
            conn.execute(text("create database resumo_test"))
    admin.dispose()

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("create extension if not exists unaccent"))
        conn.execute(text("create extension if not exists pg_trgm"))
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


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
