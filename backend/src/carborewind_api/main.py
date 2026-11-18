"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from . import __version__
from .config import Settings, get_settings
from .logging import configure_logging


class Health(BaseModel):
    """Liveness / readiness payload."""

    status: str
    version: str


def _security_headers(hsts_max_age: int) -> dict[str, str]:
    return {
        "Strict-Transport-Security": f"max-age={hsts_max_age}; includeSubDomains",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cache-Control": "no-store",
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. ``settings`` can be injected for tests."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="CarboRewind API",
        version=__version__,
        description="Hot, point-in-time backup and restore for Carbonio mail servers.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
        )

    headers = _security_headers(settings.hsts_max_age)

    @app.middleware("http")
    async def add_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in headers.items():
            # The interactive docs page needs its own CSP (inline scripts from the CDN).
            if name == "Content-Security-Policy" and request.url.path == "/api/docs":
                continue
            response.headers.setdefault(name, value)
        return response

    @app.get("/healthz", response_model=Health, tags=["ops"])
    async def healthz() -> Health:
        """Liveness probe: the process is up."""
        return Health(status="ok", version=__version__)

    @app.get("/readyz", response_model=Health, tags=["ops"])
    async def readyz() -> Health:
        """Readiness probe. Dependency checks (PostgreSQL, Redis) are added in M1."""
        return Health(status="ready", version=__version__)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        """Prometheus metrics."""
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
