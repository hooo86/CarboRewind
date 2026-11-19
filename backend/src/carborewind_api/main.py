"""FastAPI application factory."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import __version__
from .api import account_list as account_list_api
from .api import accounts as accounts_api
from .api import agent as agent_api
from .api import agents as agents_api
from .api import audit as audit_api
from .api import auth as auth_api
from .api import backups as backups_api
from .api import inventory as inventory_api
from .api import mail as mail_api
from .api import migrations as migrations_api
from .api import monitoring as monitoring_api
from .api import policies as policies_api
from .api import restores as restores_api
from .api import settings_api
from .api import users as users_api
from .auth.ldap import LdapAuthenticator
from .config import Settings, get_settings
from .db.session import make_engine
from .health import Check, database_check, redis_check, run_checks
from .logging import configure_logging
from .metrics import collect as business_metrics
from .security.ratelimit import MemoryRateLimiter, RedisRateLimiter


class Health(BaseModel):
    """Liveness / readiness payload."""

    status: str
    version: str
    checks: dict[str, str] = Field(default_factory=dict)


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


def create_app(settings: Settings | None = None, checks: dict[str, Check] | None = None) -> FastAPI:
    """Build the API. ``settings`` and readiness ``checks`` can be injected for tests."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    readiness: dict[str, Check] = dict(checks or {})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = redis = None
        if settings.database_url is not None:
            engine = make_engine(settings.database_url.get_secret_value())
            if checks is None:
                readiness["database"] = database_check(engine)
        if settings.redis_url is not None:
            redis = Redis.from_url(settings.redis_url.get_secret_value())
            if checks is None:
                readiness["redis"] = redis_check(redis)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = (
            async_sessionmaker(engine, expire_on_commit=False) if engine is not None else None
        )
        app.state.rate_limiter = RedisRateLimiter(redis) if redis else MemoryRateLimiter()
        app.state.ldap = LdapAuthenticator(settings) if settings.ldap_enabled else None
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()
            if redis is not None:
                await redis.aclose()

    app = FastAPI(
        lifespan=lifespan,
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

    @app.get(
        "/readyz",
        response_model=Health,
        tags=["ops"],
        responses={503: {"model": Health, "description": "A dependency is unavailable"}},
    )
    async def readyz(response: Response) -> Health:
        """Readiness probe: PostgreSQL and Redis answer within the configured timeout."""
        results = await run_checks(readiness, settings.readiness_timeout)
        ready = all(v == "ok" for v in results.values())
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(
            status="ready" if ready else "unavailable", version=__version__, checks=results
        )

    app.include_router(auth_api.router)
    app.include_router(users_api.router)
    app.include_router(inventory_api.router)
    app.include_router(audit_api.router)
    app.include_router(agents_api.router)
    app.include_router(agent_api.router)
    app.include_router(backups_api.router)
    app.include_router(account_list_api.router)  # before /api/accounts/{id}
    app.include_router(accounts_api.router)
    app.include_router(restores_api.router)
    app.include_router(policies_api.router)
    app.include_router(settings_api.router)
    app.include_router(migrations_api.router)
    app.include_router(mail_api.router)
    app.include_router(monitoring_api.router)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        """Prometheus metrics: process metrics plus business gauges from the database.

        Through the reverse proxy (X-Forwarded-For set) the bearer token
        ``CARBOREWIND_METRICS_TOKEN`` is required; inside the Docker network it is not.
        """
        if request.headers.get("x-forwarded-for") is not None:
            token = settings.metrics_token
            sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if token is None or not secrets.compare_digest(
                sent.encode(), token.get_secret_value().encode()
            ):
                return Response(status_code=status.HTTP_404_NOT_FOUND)
        body = generate_latest().decode()
        factory = getattr(app.state, "sessionmaker", None)
        if factory is not None:
            try:
                async with factory() as db:
                    body += await business_metrics(db)
            except Exception:  # a database outage must not break the process metrics
                body += "# carborewind business metrics unavailable\n"
        return Response(body, media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
