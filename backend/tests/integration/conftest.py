"""Fixtures for tests against a real PostgreSQL (and optionally Redis).

Set ``CARBOREWIND_TEST_DATABASE_URL`` (psycopg URL of a **disposable** database) to run them;
they are skipped otherwise. The schema is dropped and recreated by the migrations.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine

BACKEND = Path(__file__).resolve().parents[2]
DB_URL = os.environ.get("CARBOREWIND_TEST_DATABASE_URL")


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.attributes["url"] = url
    return cfg


@pytest.fixture(scope="session")
def db_url() -> str:
    if not DB_URL:
        pytest.skip("CARBOREWIND_TEST_DATABASE_URL not set")
    return DB_URL


@pytest.fixture(scope="session")
def migrated(db_url: str) -> str:
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return db_url


@pytest.fixture
def engine(migrated: str) -> Iterator[Engine]:
    eng = create_engine(migrated)
    yield eng
    eng.dispose()
