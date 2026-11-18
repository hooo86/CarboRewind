"""Readiness probe against real PostgreSQL / Redis."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from carborewind_api.config import Settings
from carborewind_api.main import create_app

pytestmark = pytest.mark.integration


def test_readyz_with_real_database(db_url: str) -> None:
    redis_url = os.environ.get("CARBOREWIND_TEST_REDIS_URL")
    settings = Settings(
        environment="test",
        database_url=db_url,  # type: ignore[arg-type]
        redis_url=redis_url,  # type: ignore[arg-type]
    )
    with TestClient(create_app(settings)) as client:
        body = client.get("/readyz").json()
    assert body["checks"]["database"] == "ok"
    if redis_url:
        assert body["checks"]["redis"] == "ok"
    assert body["status"] == "ready"


def test_readyz_unreachable_database() -> None:
    settings = Settings(
        environment="test",
        database_url="postgresql+psycopg://u:p@127.0.0.1:1/none",  # type: ignore[arg-type]
        readiness_timeout=2,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "error"}
