#!/usr/bin/env python3
"""The two apps carry one API transport layer. Is it still one?

`web/src/api/client.ts` and `student/src/api/client.ts` are byte-identical by
intent, not by accident. ADR-0002 §9 asks for "one contract-checked client for
both", and README §student app says the app "reads the same generated client,
so a contract change breaks it in the same CI run". The generated half —
`schema.d.ts` — is regenerated from `openapi/openapi.yaml` and gated by
`make web-codegen-check` and `make student-codegen-check`, so that half cannot
drift. The hand-written half could, and nothing was looking.

It matters because the hand-written half is where the policy is. `client.ts`
carries the `ANONYMOUS` list — the endpoints that must NOT trigger a refresh —
and the refresh-once middleware: a 401 is retried after exactly one refresh,
"capped at a single attempt", because retrying in a loop turns a permission
error into a denial-of-service against our own rate limiter. A fix to that in
one copy does not reach the other, and the copy it does not reach is either the
console centre staff use or the app a candidate sits the exam in.

`dev_seed.py`'s docstring names the pattern: "two copies ... is precisely the
shape this repository keeps finding and removing." This is the cheap version of
removing it — the copies stay, and the build refuses to let them differ. The
expensive version (an npm workspace with one `packages/api`) is a refactor of
both build pipelines, both Docker stages and both codegen targets to save 97
lines that this check keeps honest for free.

## The one permitted difference

`session.ts` differs in exactly one constant. `SESSION_HINT` is the
localStorage key for the "we believe a session exists" flag, and the student
copy explains why it must differ: the two apps are separate origins in
production, but in development they are localhost on two ports, which IS one
origin for `localStorage`, and a shared key would have signing out of one
silently sign you out of the other. That line, the comment block justifying it,
and the word "console"/"app" in the hint's own description are the whole
allowed diff. Anything else — a changed refresh policy, a new field in
`Principal`, a different `SameSite` argument — fails here.
"""

from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web" / "src" / "api"
STUDENT = ROOT / "student" / "src" / "api"

#: Byte-identical, no exceptions. The generated `schema.d.ts` is deliberately
#: NOT here — both codegen-check targets already prove each copy against the
#: contract, and a third assertion of the same fact is a second place to update.
IDENTICAL = ("client.ts",)

#: Differ only in the session-hint constant and its justification.
HINT_ONLY = ("session.ts",)


def normalise_hint(text: str) -> str:
    """`session.ts` with the permitted difference removed.

    Three things are allowed to differ, and each is collapsed to the same
    form in both copies so that everything else is compared verbatim:

      * `const SESSION_HINT = "..."` — the key itself.
      * The `//` comment block immediately above it, which the student copy
        carries and the console copy does not.
      * "the console" / "the app" in the hint's own doc comment.

    Collapsing rather than deleting keeps line structure comparable, so a diff
    on failure points at the real line.
    """
    text = re.sub(r"(?:^//[^\n]*\n)*^const SESSION_HINT = \"[^\"]*\";", 'const SESSION_HINT = "<hint>";', text, flags=re.M)
    text = re.sub(r"\bthe (?:console|app)\b", "the <surface>", text)
    return text


def compare(name: str, normalise=lambda text: text) -> list[str]:
    web, student = WEB / name, STUDENT / name
    problems = []
    for path in (web, student):
        if not path.is_file():
            problems.append(f"{path.relative_to(ROOT)} is missing — has the layout changed?")
    if problems:
        return problems
    left, right = normalise(web.read_text()), normalise(student.read_text())
    if left == right:
        return []
    diff = difflib.unified_diff(
        left.splitlines(), right.splitlines(),
        fromfile=str(web.relative_to(ROOT)), tofile=str(student.relative_to(ROOT)), lineterm="", n=1,
    )
    return [f"{name} differs between the console and the student app:\n" + "\n".join(list(diff)[:30])]


def main() -> int:
    problems: list[str] = []
    for name in IDENTICAL:
        problems += compare(name)
    for name in HINT_ONLY:
        problems += compare(name, normalise_hint)

    for problem in problems:
        print(f"FAIL  {problem}")
    if problems:
        print(
            "\n      One transport layer, two copies. Make the same change in both — "
            "the refresh-once policy and the ANONYMOUS list are security-relevant "
            "and the copy you did not edit is the one a real user is on."
        )
        return 1
    print(
        f"PASS  {', '.join(IDENTICAL)} identical in both apps; "
        f"{', '.join(HINT_ONLY)} differs only in SESSION_HINT"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
