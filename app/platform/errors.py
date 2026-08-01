"""Domain error hierarchy.

Each error maps to one HTTP status at the API boundary and nowhere else — domain
code raises these and never imports anything HTTP-shaped.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base. `code` is the stable machine identifier clients branch on."""

    status = 400
    code = "domain_error"

    def __init__(self, message: str, /, code: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        # A per-raise `code` narrows the class default, so a client can branch on
        # `attempt_expired` rather than the useless generic `conflict`. Taking it
        # as a keyword-only argument rather than letting it fall into **extra is
        # the difference between that working and silently doing nothing.
        if code is not None:
            self.code = code
        self.extra = extra

    def as_problem(self, instance: str | None = None) -> dict[str, Any]:
        """RFC 9457 problem document."""
        problem: dict[str, Any] = {
            "type": f"https://api.example.uz/problems/{self.code.replace('_', '-')}",
            "title": self.message,
            "status": self.status,
            "code": self.code,
        }
        if instance:
            problem["instance"] = instance
        problem.update(self.extra)
        return problem


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Forbidden(DomainError):
    status = 403
    code = "forbidden"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class Gone(DomainError):
    """It existed, it is finished, and no retry will change that.

    An expired OTP challenge, a spent invite. Distinct from 404 (the caller held
    the token, so confirming the thing was real leaks nothing they did not
    already know) and from 409, which invites a retry that cannot succeed.

    It lived as a private class inside `api/routers/auth.py`, which is why
    `/invites/accept` answered 409 while the contract declared 410 — the router
    that needed it second could not see the one the router that needed it first
    had written. `code` stays `expired` because that is what the OTP flow has
    always sent; a per-raise code narrows it.
    """

    status = 410
    code = "expired"


class PreconditionFailed(DomainError):
    status = 412
    code = "precondition_failed"


class TooEarly(DomainError):
    """Competition lobby / key release before its time."""

    status = 425
    code = "too_early"


class PaymentRequired(DomainError):
    """No entitlement covers this action."""

    status = 402
    code = "payment_required"


class RateLimited(DomainError):
    """Too many requests.

    **429, which the one rate limit in this product was not.** `auth.otp_request`
    raised a bare `DomainError(code="rate_limited")` — status 400 — while the
    contract declared `'429': RateLimited` on that very operation. Every HTTP
    client library treats 400 as a permanent client error and 429 as "back off
    and retry"; answering 400 tells a well-behaved client to give up and a badly
    behaved one nothing at all.

    `retry_after` is seconds, and `api.errors` turns it into the header. Carrying
    it on the exception rather than at each raise site is what stops the header
    and the body disagreeing.
    """

    status = 429
    code = "rate_limited"

    def __init__(self, message: str, /, retry_after: int = 60, **extra: Any) -> None:
        super().__init__(message, retry_after=retry_after, **extra)
        self.retry_after = retry_after


class ValidationFailed(DomainError):
    """Carries EVERY finding, never just the first."""

    status = 422
    code = "validation_failed"

    def __init__(self, message: str, findings: list[Any]) -> None:
        super().__init__(message, findings=[f.as_dict() for f in findings])
        self.findings = findings


class RegistryError(DomainError):
    status = 422
    code = "registry_error"
