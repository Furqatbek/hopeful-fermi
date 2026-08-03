#!/usr/bin/env python3
"""Does every admin endpoint have a screen?

`check_api_coverage.py` proves the code serves every documented operation.
`check_schema_conformance.py` proves the payloads carry every declared field.
Neither notices that an endpoint nobody can reach is an endpoint nobody uses,
and this project accumulated a lot of those: at one point 69 of 153 operations
had no call site in the console, and among them were three where the endpoint
that ACTS had been written and the endpoint that FINDS THE THING TO ACT ON had
not — `PATCH /admin/takedowns/{xid}` with no queue to read, `DELETE
/content-grants/{xid}` with no listing, `GET /media/{xid}/content` with no
grant issuer outside an exam. Each was invisible to every existing gate,
because each was individually correct.

So this gate asks the question those cannot: **is there a screen?**

Two rules, and the second is the one that keeps this file honest:

  1. Every operation must be called from `web/src`, or be listed in `EXEMPT`
     with a reason.
  2. Every `EXEMPT` entry must name a real operation that really is not called.
     A stale exemption — for an endpoint the console has since wired, or for a
     path that no longer exists — is a failure, not a shrug. Otherwise the list
     silently becomes a graveyard and the gate stops meaning anything.

The exemptions are all one of three kinds, and every one says which:

  * **student app** — the student UI is a separate mobile application, by
    explicit product decision. These endpoints have a client; it is not this
    repository.
  * **machine** — provider webhooks and server-to-server callbacks. There is no
    screen to build; they are authenticated by signature or Basic auth and no
    human ever calls them.
  * **public** — a surface deliberately outside the authenticated console.

Detection is deliberately crude in one direction and careful in the other: it
finds call sites by matching path literals in source, after stripping comments,
so a path merely *discussed* in a docstring does not count as wired. It cannot
prove a screen is good, only that one exists. That is still the check that
would have caught every gap above.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "openapi" / "openapi.yaml"
WEB = ROOT / "web" / "src"

METHODS = ("get", "post", "patch", "put", "delete")

#: Operations with no console screen, each with the reason it has none. Adding
#: an entry here is a product decision, not a way to make the build pass.
EXEMPT: dict[str, str] = {
    # ── the student mobile app ────────────────────────────────────────
    "POST /auth/telegram/verify": "student app — console staff sign in by OTP",
    "GET /me/progress": "student app — a student's own progression",
    "POST /attempts": "student app — sitting assigned or self-serve work",
    "POST /competitions/{xid}/register": "student app — entering a contest",
    "DELETE /competitions/{xid}/register": "student app — withdrawing",
    "GET /competitions/{xid}/lobby": "student app — the synchronized start",
    "POST /competitions/{xid}/key": "student app — T-0 key release, creates the attempt",
    "POST /speaking/slots/{xid}/book": "student app — booking a session",
    "DELETE /speaking/slots/{xid}/book": "student app — cancelling a booking",
    "POST /speaking/slots/{xid}/check-in": "student app — feeds the batch matcher",
    "POST /speaking/queue": "student app — the live queue",
    "DELETE /speaking/queue": "student app — leaving the queue",
    "GET /speaking/ice-servers": "student app — TURN credentials for the peer connection",
    "POST /speaking/pairs/{xid}/end": "student app — ending a live session",
    "POST /speaking/pairs/{xid}/report": "student app — reporting a session with its audio buffer",
    "POST /reports": "student app — a student reporting a peer",
    "GET /blocks": "student app — a student's own block list",
    "POST /blocks": "student app — blocking a peer",
    "DELETE /blocks/{xid}": "student app — unblocking",

    # ── machine to machine ────────────────────────────────────────────
    "POST /payments/click/prepare": "machine — Click callback, signature-verified",
    "POST /payments/click/complete": "machine — Click callback, signature-verified",
    "POST /payments/payme": "machine — Payme JSON-RPC, Basic auth with the merchant key",

    # ── deliberately outside the console ──────────────────────────────
    "POST /takedowns": "public — a rights holder must not need an account to file",
}


def source() -> str:
    """Every hand-written line of the console, with comments removed.

    Three files are excluded and all for one reason — they name paths without
    calling them:

      * `schema.d.ts` is generated and has every path in the contract as an
        object key, so including it would report perfect coverage forever.
      * `client.ts` is the transport layer and holds an `ANONYMOUS` array of
        the paths that carry no bearer token. That is a policy list, and
        counting it marked `POST /auth/telegram/verify` wired when the console
        has no Telegram sign-in at all.
      * `App.tsx` is the route table, and a browser route is not an API call.
        `<Route path="/takedowns">` is the console's own URL for the screen
        that DECIDES takedowns; `POST /takedowns` is the unauthenticated
        filing endpoint on a public page this console does not own. Same
        string, opposite meanings, and counting it hid a real exemption.

    Comments go for the same reason: this codebase explains its decisions in
    prose, and prose names endpoints — `Seats.tsx` discusses `POST /orders` in
    a comment about what it deliberately does not do.
    """
    files = subprocess.run(
        ["find", str(WEB), "(", "-name", "*.ts", "-o", "-name", "*.tsx", ")",
         "!", "-name", "schema.d.ts", "!", "-name", "client.ts",
         "!", "-name", "App.tsx"],
        capture_output=True, text=True, check=True).stdout.split()
    text = "".join(Path(f).read_text() for f in files)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def indirect_methods(src: str) -> set[str]:
    """Methods the console calls with the path held in a variable.

    `useVersionEdit(endpoint, ...)` does `api.GET(endpoint)` then
    `api.PATCH(endpoint)`, with `endpoint` one of three path literals declared
    at the call sites. So for PATCH — and only for the methods that actually
    do this — a bare literal is a real call site even though the same path is
    also addressed directly elsewhere. Without this the gate reported
    `PATCH /passage-versions/{xid}` unwired while the edit form was working.

    Deliberately narrow: it does not follow the variable, it only notices that
    indirection for that verb exists at all. A method nobody dispatches
    dynamically still needs its own explicit call.
    """
    return {m for m in METHODS
            if re.search(rf'api\.{m.upper()}\(\s*[a-z_$]', src)}


def called(method: str, path: str, src: str) -> bool:
    """Three ways the console legitimately names a path, and one trap.

    The typed client takes the path as a string literal, which covers nearly
    every call. A few pass it as a value instead —
    `useVersionEdit("/passage-versions/{xid}", ...)` takes the endpoint as a
    parameter — so a bare literal has to count as well. And a multipart upload
    or a file download cannot go through `openapi-fetch` at all, so those build
    a template string on `API_PREFIX`.

    The trap is that a bare literal carries no method, and most paths serve
    several. Counting one would mark `POST /orgs` wired because `Roster.tsx`
    calls `GET /orgs`, and `POST /band-maps` wired because `Composition.tsx`
    reads the list — which is exactly the "an endpoint nobody can reach" defect
    this gate exists to find, reported as covered.

    So: if the console addresses this path through the typed client at all,
    every method on it must show its own `api.METHOD(...)` call. The bare
    literal only counts for a path that appears nowhere else, which is what
    genuine indirection looks like.
    """
    literal = "".join(
        r"(?:\{[^}]+\}|\$\{[^}]+\})" if part.startswith("{") else re.escape(part)
        for part in re.split(r"(\{[^}]+\})", path))

    if re.search(rf'api\.{method.upper()}\(\s*"{literal}"', src):
        return True
    # A literal that is NOT sitting in an `api.METHOD(` position: it is being
    # passed somewhere as a value. Only that occurrence earns the indirection
    # allowance, and only for verbs the console really does dispatch
    # dynamically. Checking merely that the literal appears anywhere would let
    # `api.PATCH("/orgs/{xid}")` mark `GET /orgs/{xid}` wired — which it did,
    # and the gate went green on a screen that does not exist.
    verbs = "|".join(m.upper() for m in METHODS)
    passed_as_value = any(
        not re.search(rf'api\.(?:{verbs})\(\s*$', src[:hit.start()])
        for hit in re.finditer(rf'"{literal}"', src))
    if passed_as_value and method in indirect_methods(src):
        return True
    if not re.search(rf'api\.(?:{verbs})\(\s*"{literal}"', src) \
            and re.search(rf'"{literal}"', src):
        return True
    # Raw `fetch` is method-blind here: the verb sits in an options object well
    # away from the URL. There are two such call sites and both are documented
    # in `Import.tsx`; a third would be worth tightening for.
    return bool(re.search(rf'`\$\{{API_PREFIX\}}{literal}[`?]', src)
                or re.search(rf'"/api/v1{literal}"', src))


def main() -> int:
    spec = yaml.safe_load(SPEC.read_text())
    src = source()

    operations = [
        (f"{method.upper()} {path}", op)
        for path, item in spec["paths"].items()
        for method, op in item.items() if method in METHODS
    ]

    wired, missing = [], []
    for name, _ in operations:
        method, path = name.split(" ", 1)
        (wired if called(method.lower(), path, src) else missing).append(name)

    known = {name for name, _ in operations}

    uncovered = sorted(set(missing) - set(EXEMPT))
    stale = sorted(set(EXEMPT) & set(wired))
    unknown = sorted(set(EXEMPT) - known)

    print(f"operations        {len(operations)}")
    print(f"wired to a screen {len(wired)}")
    print(f"exempt            {len(set(EXEMPT) & set(missing))}")

    if uncovered:
        print(f"\nFAIL  {len(uncovered)} admin operations have no console screen.")
        print("      Build one, or add an EXEMPT entry saying why there is none.")
        for name in uncovered:
            print(f"        {name}")
    if stale:
        print(f"\nFAIL  {len(stale)} exemptions are stale — these ARE called now.")
        print("      Delete the entry; the reason it gives is no longer true.")
        for name in stale:
            print(f"        {name}  ({EXEMPT[name]})")
    if unknown:
        print(f"\nFAIL  {len(unknown)} exemptions name operations the contract "
              "does not have.")
        for name in unknown:
            print(f"        {name}")

    if uncovered or stale or unknown:
        return 1
    print("\nPASS  every admin operation has a screen; every exemption is current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
