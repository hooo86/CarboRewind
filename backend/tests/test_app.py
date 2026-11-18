import json
import logging

import pytest
from fastapi.testclient import TestClient

from carborewind_api import __version__
from carborewind_api.config import Settings
from carborewind_api.logging import JsonFormatter
from carborewind_api.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(environment="test")))


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "checks": {}}


def test_readyz_without_dependencies(client: TestClient) -> None:
    assert client.get("/readyz").json() == {"status": "ready", "version": __version__, "checks": {}}


async def _ok() -> None:
    return None


async def _boom() -> None:
    raise ConnectionError("secret-host:5432 refused")


def test_readyz_reports_failed_dependency_without_details() -> None:
    app = create_app(Settings(environment="test"), checks={"database": _ok, "redis": _boom})
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"] == {"database": "ok", "redis": "error"}
    assert "secret-host" not in response.text


def test_readyz_times_out_slow_dependency() -> None:
    import asyncio

    async def slow() -> None:
        await asyncio.sleep(5)

    settings = Settings(environment="test", readiness_timeout=0.05)
    with TestClient(create_app(settings, checks={"database": slow})) as client:
        assert client.get("/readyz").json()["checks"] == {"database": "error"}


def test_database_url_is_secret() -> None:
    settings = Settings(database_url="postgresql+psycopg://u:hunter2@h/db")  # type: ignore[arg-type]
    assert "hunter2" not in repr(settings)


def test_security_headers(client: TestClient) -> None:
    headers = client.get("/healthz").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "max-age=" in headers["strict-transport-security"]
    assert "default-src 'none'" in headers["content-security-policy"]


def test_docs_page_keeps_its_own_csp(client: TestClient) -> None:
    response = client.get("/api/docs")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers


def test_openapi_schema(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()
    assert schema["info"]["title"] == "CarboRewind API"
    assert "/healthz" in schema["paths"]


def test_metrics(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "python_info" in response.text


def test_cors_enabled_only_when_configured() -> None:
    origin = "https://admin.example.test"
    with_cors = TestClient(create_app(Settings(environment="test", cors_origins=[origin])))
    preflight = with_cors.options(
        "/healthz",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    assert preflight.headers["access-control-allow-origin"] == origin

    without = TestClient(create_app(Settings(environment="test")))
    assert (
        "access-control-allow-origin"
        not in without.get("/healthz", headers={"Origin": origin}).headers
    )


def test_json_formatter_includes_extra_fields() -> None:
    record = logging.makeLogRecord(
        {"name": "t", "levelname": "INFO", "msg": "job %s", "args": ("42",), "job_id": "42"}
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "job 42"
    assert payload["job_id"] == "42"
    assert payload["level"] == "INFO"


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.makeLogRecord({"msg": "failed", "exc_info": sys.exc_info()})
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc"]
