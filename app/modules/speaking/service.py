"""Running the matcher against the database.

Everything safety-critical lives in `matching.py`, which is pure. This file's job
is to load candidates honestly and to write the result — and "honestly" is doing
real work in one place:

    `is_minor` is derived from `users.adult_at`, per candidate, at match time.

Not from the slot's `age_band`, not from the booking, not from anything the
client sent. A slot mislabelled `adult` by a teacher who picked the wrong option
must not be able to put a fourteen-year-old in an adult pair, and the only way to
guarantee that is to ask the database how old each person actually is.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from .matching import Candidate, Outcome, match

log = structlog.get_logger()

# Two people paired inside this window are "recent" and the matcher avoids
# repeating them. Long enough that a regular does not get the same partner every
# evening, short enough that a small pool still resolves.
ANTI_REPEAT_WINDOW = dt.timedelta(days=7)
# A live-queue entry older than this has gone to make tea.
QUEUE_TTL = dt.timedelta(minutes=10)


def match_slot(session: Session, slot_id: int, now: dt.datetime) -> Outcome:
    """Pair everyone who CHECKED IN to a slot. Booked-but-absent is a no-show.

    Check-in rather than booking is the input because pairing someone who booked
    three days ago and forgot means their partner's first experience of the
    feature is two minutes of silence.
    """
    slot = session.execute(text("""
        SELECT id, xid, org_id, age_band, language, cue_card_set_version_id, status
        FROM speaking_slots WHERE id = :s FOR UPDATE
    """).bindparams(s=slot_id)).mappings().first()
    if slot is None or slot["status"] not in ("booking", "matching"):
        return Outcome((), ())

    rows = session.execute(text("""
        SELECT b.id AS booking_id, b.user_id, b.self_band,
               u.xid AS user_xid,
               (u.adult_at > current_date) AS is_minor,
               b.checked_in_at
        FROM speaking_slot_bookings b
        JOIN users u ON u.id = b.user_id
        WHERE b.slot_id = :s AND b.cancelled_at IS NULL
          AND b.checked_in_at IS NOT NULL AND b.pair_id IS NULL
    """).bindparams(s=slot_id)).mappings().all()

    by_user = {str(r["user_id"]): r for r in rows}
    candidates = [
        Candidate(
            user_xid=str(r["user_id"]),
            # From `adult_at`, not from `slot.age_band`. See the module docstring.
            is_minor=bool(r["is_minor"]),
            language=slot["language"],
            band=Decimal(str(r["self_band"])) if r["self_band"] is not None else None,
            waiting_since=r["checked_in_at"],
            org_xid=str(slot["org_id"]) if slot["org_id"] else None,
            recent_partners=_recent_partners(session, r["user_id"], now),
            blocked=_blocked(session, r["user_id"]),
        )
        for r in rows
    ]

    outcome = match(candidates)
    for pair in outcome.pairs:
        pair_id = _create_pair(session, pair, origin="slot_batch", slot=slot, now=now)
        for user_xid in pair.user_xids:
            session.execute(text(
                "UPDATE speaking_slot_bookings SET pair_id = :p WHERE id = :b"
            ).bindparams(p=pair_id, b=by_user[user_xid]["booking_id"]))

    session.execute(text("""
        UPDATE speaking_slot_bookings SET no_show = true
        WHERE slot_id = :s AND cancelled_at IS NULL AND checked_in_at IS NULL
    """).bindparams(s=slot_id))
    session.execute(text("UPDATE speaking_slots SET status = 'live' WHERE id = :s")
                    .bindparams(s=slot_id))

    for leftover in outcome.unmatched:
        # Told, not silently dropped. Someone who showed up and got nothing needs
        # to know it was the pool, not them, and needs the next slot offered.
        _notify(session, int(leftover.user_xid), "speaking.no_partner",
                {"slot_xid": str(slot["xid"])},
                dedupe=f"speaking_unmatched:{slot_id}:{leftover.user_xid}")

    log.info("slot_matched", slot_id=slot_id, pairs=len(outcome.pairs),
             unmatched=len(outcome.unmatched))
    return outcome


def match_queue(session: Session, now: dt.datetime) -> Outcome:
    """The live "try now" pool. Same matcher, different source."""
    session.execute(text("""
        UPDATE speaking_queue_entries SET status = 'expired', left_at = now()
        WHERE status = 'waiting' AND joined_at < :cutoff
    """).bindparams(cutoff=now - QUEUE_TTL))

    rows = session.execute(text("""
        SELECT q.id, q.user_id, q.band_min, q.band_max, q.language, q.org_only,
               q.org_id, q.joined_at, (u.adult_at > current_date) AS is_minor
        FROM speaking_queue_entries q
        JOIN users u ON u.id = q.user_id
        WHERE q.status = 'waiting'
        ORDER BY q.joined_at
        FOR UPDATE SKIP LOCKED
    """)).mappings().all()
    if len(rows) < 2:
        return Outcome((), ())

    by_user = {str(r["user_id"]): r for r in rows}
    candidates = [
        Candidate(
            user_xid=str(r["user_id"]), is_minor=bool(r["is_minor"]),
            language=r["language"],
            band=_midpoint(r["band_min"], r["band_max"]),
            waiting_since=r["joined_at"],
            org_xid=str(r["org_id"]) if r["org_id"] else None,
            org_only=bool(r["org_only"]),
            recent_partners=_recent_partners(session, r["user_id"], now),
            blocked=_blocked(session, r["user_id"]),
        )
        for r in rows
    ]

    outcome = match(candidates)
    for pair in outcome.pairs:
        pair_id = _create_pair(session, pair, origin="live_queue", slot=None, now=now)
        for user_xid in pair.user_xids:
            session.execute(text("""
                UPDATE speaking_queue_entries
                SET status = 'matched', matched_pair_id = :p, left_at = :now
                WHERE id = :id
            """).bindparams(p=pair_id, now=now, id=by_user[user_xid]["id"]))

    if outcome.pairs:
        log.info("queue_matched", pairs=len(outcome.pairs),
                 still_waiting=len(outcome.unmatched))
    return outcome


def _create_pair(session: Session, pair, *, origin: str, slot, now: dt.datetime) -> int:
    """One last age check at the point of writing.

    `matching.match` already partitions on `is_minor` and asserts before
    returning. Re-checking here costs nothing and means the invariant holds even
    if a future caller assembles pairs some other way — this INSERT is the only
    place a pair comes into existence.
    """
    if pair.a.is_minor != pair.b.is_minor:                     # pragma: no cover
        raise ValueError("refusing to write a cross-age-band speaking pair")

    return session.scalar(text("""
        INSERT INTO speaking_pairs (slot_id, origin, user_a_id, user_b_id, age_band,
                                    cue_card_set_version_id, matched_at)
        VALUES (:slot, :origin, :a, :b, :band, :cue, :now)
        RETURNING id
    """).bindparams(
        slot=slot["id"] if slot else None, origin=origin,
        a=int(pair.a.user_xid), b=int(pair.b.user_xid), band=pair.age_band,
        cue=slot["cue_card_set_version_id"] if slot else None, now=now))


def _recent_partners(session: Session, user_id: int, now: dt.datetime) -> frozenset[str]:
    rows = session.execute(text("""
        SELECT CASE WHEN user_a_id = :u THEN user_b_id ELSE user_a_id END AS peer
        FROM speaking_pairs
        WHERE (user_a_id = :u OR user_b_id = :u) AND matched_at > :since
    """).bindparams(u=user_id, since=now - ANTI_REPEAT_WINDOW)).scalars().all()
    return frozenset(str(r) for r in rows)


def _blocked(session: Session, user_id: int) -> frozenset[str]:
    """Both directions in one set. Who this user blocked, and who blocked them —
    the matcher must honour a block without revealing which way it points."""
    rows = session.execute(text("""
        SELECT blocked_user_id AS other FROM user_blocks WHERE blocker_user_id = :u
        UNION
        SELECT blocker_user_id FROM user_blocks WHERE blocked_user_id = :u
    """).bindparams(u=user_id)).scalars().all()
    return frozenset(str(r) for r in rows)


def _midpoint(low, high) -> Decimal | None:
    if low is None and high is None:
        return None
    if low is None:
        return Decimal(str(high))
    if high is None:
        return Decimal(str(low))
    return (Decimal(str(low)) + Decimal(str(high))) / 2


def _notify(session: Session, user_id: int, template: str, params: dict,
            *, dedupe: str) -> None:
    session.execute(text("""
        INSERT INTO notifications (user_id, channel, template, params, dedupe_key)
        VALUES (:u, 'in_app', :t, CAST(:p AS jsonb), :d)
        ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
    """).bindparams(u=user_id, t=template, p=json.dumps(params), d=dedupe))


def due_slots(session: Session, now: dt.datetime) -> list[int]:
    """Slots whose start time has arrived and which still hold unpaired check-ins."""
    return list(session.execute(text("""
        SELECT id FROM speaking_slots
        WHERE status IN ('booking', 'matching') AND starts_at <= :now
        ORDER BY starts_at
    """).bindparams(now=now)).scalars())
