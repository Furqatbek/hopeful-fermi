"""Request budgets, per route, per caller.

The brief asks for "rate limits on content endpoints" and there were none. Sixty
consecutive catalogue reads returned sixty 200s; thirty attempt starts, thirty
201s; forty pulls of the same paper, forty 200s.

## One gate, not a decoration on each route

Applied as a router-level dependency in `main.create_app`, so **every route under
`/api/v1` is covered the moment it exists**. The table below names only the
endpoints where the default is wrong. That is the same shape as `authz.Policy`
and for the same stated reason — "enforce centrally, not with scattered role
checks" — because a limiter you have to remember to attach is a limiter that is
missing from the route somebody added last week, which is the route worth
attacking.

## Choosing the numbers

Two failure modes, and they pull in opposite directions.

**A scraper** wants many different papers. Every paper costs one `POST /attempts`
and one `GET .../payload`, so those two are the bound that matters, and they are
the tightest here. Twenty a minute is already far past a person reading.

**A student in a timed exam** must never be refused. The autosave flushes every
5–10 s and retries on a flaky link; the clock syncs; sections are entered; audio
is granted. Those live on the default, and the default is set high enough that no
plausible client reaches it — because being rate-limited mid-paper with the clock
running is a support call about a lost exam, and no amount of scraping is worth
one of those. When in doubt the number here goes UP.

## Identity

The user xid from the bearer token, or the client IP when there is none. The
token is decoded WITHOUT the database hit `principal` does — this runs on every
request, and a limiter that costs a query is a limiter that adds load under
exactly the traffic it exists to shed. A token that does not verify falls through
to the IP rather than erroring: rejecting it is `principal`'s job and it does it
one dependency later.
"""

from __future__ import annotations

import jwt
from fastapi import Request

from app.platform.config import settings
from app.platform.errors import RateLimited
from app.platform.ratelimit import Budget, check

DEFAULT = Budget(limit=120)

#: Only where the default is wrong. Keyed by `(method, router-relative path)`.
BUDGETS: dict[tuple[str, str], Budget] = {
    # The scrape bound. A client fetches a paper once per attempt and then
    # revalidates with `If-None-Match`, so twenty distinct papers a minute is not
    # a person reading one.
    ("GET", "/attempts/{xid}/payload"): Budget(20),
    # The other half of the same bound: you cannot read a paper you have not
    # started an attempt against.
    ("POST", "/attempts"): Budget(20),
    # Catalogue listing. Paging through a centre's library is legitimate and
    # bursty; enumerating it is the same request repeated.
    ("GET", "/tests"): Budget(60),
    ("GET", "/questions"): Budget(60),
    ("GET", "/passages"): Budget(60),
    ("GET", "/question-groups"): Budget(60),
    ("GET", "/audio-tracks"): Budget(60),
    # Whole-test reads, which is what an exporter or an importer-of-someone-
    # else's-work does in a loop.
    ("GET", "/test-versions/{xid}/export"): Budget(20),
    ("GET", "/test-versions/{xid}/sections"): Budget(60),
    # Media bytes. Signed, short-TTL and per-user already; this bounds how fast
    # one account can walk the library behind them.
    ("GET", "/media/{xid}/content"): Budget(60),
    # Answer keys. Not student-reachable — `Action.VIEW_EXPOSURE` and friends
    # gate them — but the blast radius if authorization ever slips is the whole
    # product, so the budget is small enough to notice.
    ("GET", "/question-versions/{xid}/keys"): Budget(30),
    ("GET", "/audio-tracks/{xid}/transcript"): Budget(30),
}

#: Routes this gate must not touch.
EXEMPT: frozenset[tuple[str, str]] = frozenset({
    # Payme and Click drive these; they are inbound, unauthenticated, and keyed
    # by provider IP rather than by any user. Rate-limiting a provider callback
    # turns a burst of legitimate settlements into unpaid orders and a
    # reconciliation job to explain it. Their abuse control is the signature
    # check, which is a better one.
    ("POST", "/payments/payme"),
    ("POST", "/payments/click/prepare"),
    ("POST", "/payments/click/complete"),
    # Already limited where it must be transactional and durable: `otp_request`
    # counts `otp_challenges` rows in PostgreSQL, per PHONE and per IP, both
    # fail-closed, because it spends real money per send and the count must
    # survive a Redis restart. A fail-open window here would be a third counter
    # that is off exactly when the durable two are doing the work, and the
    # per-IP half used to be the missing one: this comment said "per PHONE"
    # while ADR-0001 §5.2 and the contract promised both.
    ("POST", "/auth/otp/request"),
})


def _identity(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        try:
            claims = jwt.decode(header[7:], settings().jwt_secret,
                                algorithms=["HS256"])
            return f"u:{claims['sub']}"
        except (jwt.PyJWTError, KeyError):
            pass
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


def budget_for(method: str, path: str) -> Budget | None:
    """`None` means exempt. Unknown routes get the default, deliberately: a new
    endpoint is covered on the day it is written, not on the day somebody
    remembers it."""
    if (method, path) in EXEMPT:
        return None
    return BUDGETS.get((method, path), DEFAULT)


def enforce(request: Request) -> None:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path is None:                                          # pragma: no cover
        # Starlette sets `scope["route"]` before solving dependencies, so this is
        # unreachable from a matched route. Allowing rather than raising is the
        # same call the module makes about Redis being down: an unidentifiable
        # request is not worth a 429 aimed at a student.
        return

    budget = budget_for(request.method, path)
    if budget is None:
        return

    verdict = check(f"{request.method}:{path}:{_identity(request)}", budget)
    if not verdict.allowed:
        raise RateLimited(
            "Too many requests. Slow down and try again shortly.",
            retry_after=verdict.retry_after,
            limit=budget.limit, window_seconds=budget.window_seconds)
