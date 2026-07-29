"""FastAPI application factory.

Sync handlers by default, running in the threadpool (ADR-0001 §5.7). `async def`
is reserved for the WebSocket gateway and media streaming; a sync handler that
blocks for more than 200 ms belongs in a Dramatiq job instead.
"""

from __future__ import annotations

import time
import uuid

import structlog
from fastapi import FastAPI, Request

from app.api import errors
from app.api.routers import authoring, exam
from app.platform.config import settings

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
    app.include_router(exam.router, prefix="/api/v1")
    app.include_router(authoring.router, prefix="/api/v1")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    return app


app = create_app()
