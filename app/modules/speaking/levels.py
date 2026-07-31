"""What band a student is actually at, from what they have actually scored.

The one definition of "this student's level" for speaking, and the reason it is
its own module rather than a helper next to its first caller: three places need
it — the slot list, the booking check, and the live queue — and the moment two of
them compute it differently they are two numbers that disagree the first time
somebody sits a mock.

**It replaces `users.target_band`, which was an aspiration.** A booking's
`self_band` came from the profile field a student fills in when they sign up, so
the matcher was pairing on what people *want* to score. Aspirations are
systematically optimistic and they cluster — most of a cohort writes 7.0 — so the
number carried almost no information about who could hold a fifteen-minute
conversation with whom. Worse, once a slot's band range began to filter
(`0011-ci.md` §29), a beginner who had written 9.0 on their profile would be
refused the beginners' session they belong in.

**There is no fallback to `target_band`.** A student with no scored mocks has an
unknown band, and unknown is excluded by no range and paired by waiting time. That
carve-out already exists and is tested; reaching for the aspiration instead would
put the same misleading number back, in a field whose meaning would then depend on
the row.

**The honest caveat: this is a reading and listening band.** Those are the only
papers this platform scores, so using it for speaking is a proxy. It is a much
better proxy than an aspiration — it reflects measured English rather than
ambition — but a student can read at 7.0 and speak at 5.5, and nothing here knows
that. Closing the gap needs speaking to be scored, which is a product away.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

# The mean of the last few sittings, not the latest one. A single bad morning
# should not drop somebody a band and put them in the wrong room for a month;
# three is enough to absorb that and few enough to follow real improvement.
RECENT_ATTEMPTS = 3
# How far back to look when there is nothing recent. Longer than the analytics
# window on purpose: a six-month-old band is a slightly stale pairing, and no band
# at all is no signal, which is the worse of the two.
WINDOW = dt.timedelta(days=180)


def current_bands(session: Session, user_ids: list[int], *,
                  now: dt.datetime | None = None) -> dict[int, Decimal]:
    """Measured band per user, to the nearest half band. Absent means unknown.

    One query for the whole list, because the queue asks about everyone waiting at
    once and a per-user version of this is an N+1 in a worker loop.
    """
    if not user_ids:
        return {}
    since = (now or dt.datetime.now(dt.UTC)) - WINDOW
    rows = session.execute(text("""
        WITH recent AS (
            SELECT a.user_id, r.band,
                   row_number() OVER (PARTITION BY a.user_id
                                      ORDER BY a.submitted_at DESC, r.id DESC) AS n
            FROM score_runs r
            JOIN attempts a ON a.id = r.attempt_id
            WHERE r.is_current AND r.band IS NOT NULL
              AND a.user_id = ANY(:users)
              -- An author rehearsing their own paper is not evidence about them,
              -- the same exclusion every other query over attempts makes.
              AND a.mode <> 'preview'
              AND a.submitted_at IS NOT NULL
              AND a.submitted_at >= :since
        )
        SELECT user_id, round(avg(band) * 2) / 2 AS band
        FROM recent WHERE n <= :take GROUP BY user_id
    """).bindparams(users=list(user_ids), since=since,
                    take=RECENT_ATTEMPTS)).mappings().all()
    return {r["user_id"]: r["band"] for r in rows}


def current_band(session: Session, user_id: int, *,
                 now: dt.datetime | None = None) -> float | None:
    """One student. A thin wrapper so there is still only one query shape."""
    band = current_bands(session, [user_id], now=now).get(user_id)
    return float(band) if band is not None else None
