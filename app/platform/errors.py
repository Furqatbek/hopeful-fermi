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
