"""Domain errors -> RFC 9457 problem documents.

The single place HTTP status codes are decided. Domain code raises
`DomainError` subclasses and never imports anything HTTP-shaped, which is what
lets the whole exam engine be tested without a web framework.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.platform.errors import DomainError, ValidationFailed

log = logging.getLogger(__name__)


def problem(status: int, body: dict, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=body, headers=headers,
                        media_type="application/problem+json")


def install(app: FastAPI) -> None:
    @app.exception_handler(ValidationFailed)
    async def _validation(request: Request, exc: ValidationFailed) -> JSONResponse:
        # `findings` carries EVERY problem, so an author fixes one round rather
        # than nineteen.
        return problem(exc.status, {**exc.as_problem(str(request.url.path)),
                                    "request_id": _request_id(request)})

    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        # `Retry-After` belongs here for the same reason the status code does:
        # this is the one place an exception becomes HTTP. A 429 whose body says
        # to wait and whose headers do not is a 429 that generic client
        # middleware — which reads the header, not the body — will retry
        # immediately.
        retry_after = getattr(exc, "retry_after", None)
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        return problem(exc.status, {**exc.as_problem(str(request.url.path)),
                                    "request_id": _request_id(request)}, headers)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request,
                                  exc: RequestValidationError) -> JSONResponse:
        return problem(422, {
            "type": "https://api.example.uz/problems/request-invalid",
            "title": "The request body or parameters are invalid.",
            "status": 422,
            "code": "request_invalid",
            "instance": str(request.url.path),
            "request_id": _request_id(request),
            "findings": [
                {"code": "REQUEST_INVALID", "severity": "error",
                 "path": ".".join(str(p) for p in e["loc"]), "message": e["msg"],
                 "fix_hint": "Check the field against the OpenAPI schema."}
                for e in exc.errors()
            ],
        })

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak an internal message to a client; the request id is what ties
        # the user's report to the Sentry event.
        log.exception("unhandled error", extra={"path": request.url.path})
        return problem(500, {
            "type": "https://api.example.uz/problems/internal",
            "title": "Something went wrong on our side.",
            "status": 500,
            "code": "internal_error",
            "instance": str(request.url.path),
            "request_id": _request_id(request),
        })


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")
