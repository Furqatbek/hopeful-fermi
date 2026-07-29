"""FastAPI application factory.

Sync handlers by default, running in the threadpool (ADR-0001 §5.7). `async def`
is reserved for the WebSocket gateway and media streaming; a sync handler that
blocks for more than 200 ms belongs in a Dramatiq job instead.
"""

from __future__ import annotations

import time
import uuid

import structlog
from fastapi import Depends, FastAPI, Request

from app.api import deps, errors
from app.api.routers import (
    assets, auth, authoring, competitions, exam, identity, platform_ops, speaking,
    teaching, tests_authoring,
)
from app.platform.config import settings

API_PREFIX = "/api/v1"

# Every router the contract in `openapi/openapi.yaml` describes, in one list.
# `scripts/check_api_coverage.py` compares this application's generated document
# against that file and fails when the two drift — which is the only way a
# hand-written contract and an implementation stay in agreement.
ROUTERS = (
    auth.router,
    identity.router, identity.orgs,
    tests_authoring.router, authoring.router, assets.router,
    exam.router, teaching.router, teaching.regrades,
    competitions.router, speaking.router,
    platform_ops.reg_router, platform_ops.media_router, platform_ops.gov_router,
    platform_ops.safety_router, platform_ops.billing_router,
    platform_ops.analytics_router, platform_ops.realtime_router,
)

log = structlog.get_logger()


def create_app() -> FastAPI:
    app = FastAPI(
        title="IELTS Hub API",
        version="1.0.0",
        docs_url="/docs" if settings().debug else None,
        openapi_url="/openapi.json" if settings().debug else None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """A request id on every log line and every error response — it is what
        ties a user's report to the Sentry event, and it costs nothing."""
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id,
                                               path=request.url.path)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Request-ID"] = request_id
        log.info("request", method=request.method, path=request.url.path,
                 status=response.status_code,
                 duration_ms=round((time.perf_counter() - started) * 1000, 1))
        return response

    errors.install(app)
    for router in ROUTERS:
        app.include_router(router, prefix=API_PREFIX)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/metrics/workers", include_in_schema=False)
    def worker_health(session=Depends(deps.db)) -> dict:
        """Outbox lag and the queue depths, for whatever is watching.

        Deliberately OUT of the OpenAPI document: it is an operations endpoint,
        not part of the contract, and `scripts/check_api_coverage.py` would
        rightly flag an undeclared path.

        `outbox_lag_seconds` is the number to alarm on (Deliverable 5 §5) —
        green under 5 s, page over 60 s sustained. It covers regrade,
        notifications, analytics and every other asynchronous path at once,
        which is why it is one query rather than a dashboard.
        """
        from app.platform import health

        return health.snapshot(session)

    return app


app = create_app()
