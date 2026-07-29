"""Validation findings.

Shared by the publish gate, the registry validator and the import pipeline,
because all three make the same promise: return EVERY problem at once, with a
pointer the UI can deep-link to and a hint the author can act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Severity
    message: str
    path: str = ""
    fix_hint: str = ""
    subject_xid: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "path": self.path,
            "fix_hint": self.fix_hint,
            "subject_xid": self.subject_xid,
        }


@dataclass(slots=True)
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, message: str, *, path: str = "", fix_hint: str = "",
            severity: Severity = Severity.ERROR, subject_xid: str | None = None) -> None:
        self.findings.append(Finding(code, severity, message, path, fix_hint, subject_xid))

    def warn(self, code: str, message: str, **kw: Any) -> None:
        self.add(code, message, severity=Severity.WARNING, **kw)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def passed(self) -> bool:
        return not self.errors

    def codes(self) -> set[str]:
        return {f.code for f in self.findings}

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "findings": [f.as_dict() for f in self.findings],
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
        }
