"""Driving a competition through its states, and publishing its board.

Raw SQL rather than ORM models: competitions have no model file, and adding one
would mean the exam module's `attempts` and this module's `competition_entries`
are mapped in two places with a foreign key between them — which is exactly the
cross-module coupling the import contracts exist to prevent.

The interesting decision is in `materialize`: a leaderboard is written ONCE, from
frozen inputs, and rewritten only by an explicit decision. Everything else in
this file is a state machine tick.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

from .ranking import Entry, rank
from .schedule import Timing, next_status

log = structlog.get_logger()


def tick(session: Session, now: dt.datetime) -> list[dict]:
    """Advance every contest that is due. Returns what moved, for the log.

    Locked with `FOR UPDATE SKIP LOCKED` so two ticks overlapping — which happens
    the moment anyone restarts a worker — cannot both publish the same board.
    """
    rows = session.execute(text("""
        SELECT id, xid, status, lobby_opens_at, starts_at, ends_at,
               registration_closes_at, tiebreak
        FROM competitions
        WHERE status NOT IN ('final', 'cancelled')
        ORDER BY starts_at
        FOR UPDATE SKIP LOCKED
    """)).mappings().all()

    moved = []
    for row in rows:
        timing = Timing(status=row["status"], lobby_opens_at=row["lobby_opens_at"],
                        starts_at=row["starts_at"], ends_at=row["ends_at"],
                        registration_closes_at=row["registration_closes_at"])
        pending = _pending(session, row["id"]) if row["status"] == "grading" else 0
        target = next_status(timing, now, entries_pending=pending)
        if target is None:
            continue

        _on_enter(session, row, target, now)
        session.execute(text(
            "UPDATE competitions SET status = :s WHERE id = :id AND status = :from"
        ).bindparams(s=target, id=row["id"], **{"from": row["status"]}))
        moved.append({"competition_xid": str(row["xid"]),
                      "from": row["status"], "to": target})
        log.info("competition_state", competition=str(row["xid"]),
                 **{"from": row["status"], "to": target})
    return moved


def _on_enter(session: Session, row, target: str, now: dt.datetime) -> None:
    if target == "live":
        _mark_no_shows(session, row["id"])
    elif target == "grading":
        _close_entries(session, row["id"])
    elif target == "final":
        materialize(session, row["id"], tiebreak=row["tiebreak"], now=now,
                    provisional=False)


def _pending(session: Session, competition_id: int) -> int:
    """Started but not yet scored. The sweeper auto-submits expired attempts, so
    this drains on its own; `GRADING_PATIENCE` is the backstop."""
    return int(session.scalar(text("""
        SELECT count(*) FROM competition_entries e
        JOIN attempts a ON a.id = e.attempt_id
        WHERE e.competition_id = :c AND e.status = 'started' AND a.status <> 'scored'
    """).bindparams(c=competition_id)) or 0)


def _mark_no_shows(session: Session, competition_id: int) -> None:
    """Registered, never prefetched, contest started. Recorded rather than
    deleted: a centre asking "who missed the mock" needs the row."""
    session.execute(text("""
        UPDATE competition_entries SET status = 'no_show'
        WHERE competition_id = :c AND status = 'registered'
    """).bindparams(c=competition_id))


def _close_entries(session: Session, competition_id: int) -> None:
    session.execute(text("""
        UPDATE competition_entries e SET status = 'submitted'
        FROM attempts a
        WHERE a.id = e.attempt_id AND e.competition_id = :c
          AND e.status = 'started' AND a.status IN ('submitted', 'scored')
    """).bindparams(c=competition_id))


# ── the board ────────────────────────────────────────────────────────

def materialize(session: Session, competition_id: int, *, tiebreak, now: dt.datetime,
                provisional: bool) -> int:
    """Write `competition_results` from the current score runs.

    Upserted on `(competition_id, user_id)`, so the live board can be refreshed
    repeatedly during a contest and the final pass simply overwrites it with the
    same rows plus `is_provisional = false`.

    Ranking uses the contest's own `tiebreak` array, evaluated by
    `ranking.rank()`. Genuine ties share a rank — inventing a winner between two
    identical papers is the one thing a leaderboard must never do.
    """
    rows = session.execute(text("""
        SELECT e.user_id, a.xid AS attempt_xid, a.id AS attempt_id, r.id AS run_id,
               r.raw_score, r.band, a.started_at, a.submitted_at
        FROM competition_entries e
        JOIN attempts a   ON a.id = e.attempt_id
        JOIN score_runs r ON r.attempt_id = a.id AND r.is_current
        WHERE e.competition_id = :c AND a.status = 'scored'
    """).bindparams(c=competition_id)).mappings().all()
    if not rows:
        return 0

    entries = [
        Entry(user_xid=str(r["user_id"]), attempt_xid=str(r["attempt_xid"]),
              raw_score=Decimal(str(r["raw_score"])),
              band=Decimal(str(r["band"])) if r["band"] is not None else None,
              duration_ms=_duration_ms(r), submitted_at=r["submitted_at"])
        for r in rows
    ]
    ranked = rank(entries, tiebreak if isinstance(tiebreak, list) else None)
    by_user = {r["user_id"]: r for r in rows}

    for row in ranked:
        source = by_user[int(row.entry.user_xid)]
        session.execute(text("""
            INSERT INTO competition_results
                (competition_id, user_id, attempt_id, score_run_id, raw_score, band,
                 duration_ms, submitted_at, rank, tiebreak_key, is_provisional,
                 computed_at)
            VALUES (:c, :u, :a, :r, :raw, :band, :dur, :sub, :rank, :key, :prov, :now)
            ON CONFLICT (competition_id, user_id) DO UPDATE
            SET attempt_id = EXCLUDED.attempt_id, score_run_id = EXCLUDED.score_run_id,
                raw_score = EXCLUDED.raw_score, band = EXCLUDED.band,
                duration_ms = EXCLUDED.duration_ms, rank = EXCLUDED.rank,
                tiebreak_key = EXCLUDED.tiebreak_key,
                is_provisional = EXCLUDED.is_provisional, computed_at = EXCLUDED.computed_at
        """).bindparams(
            c=competition_id, u=int(row.entry.user_xid), a=source["attempt_id"],
            r=source["run_id"], raw=row.entry.raw_score, band=row.entry.band,
            dur=row.entry.duration_ms, sub=row.entry.submitted_at, rank=row.rank,
            key=row.tiebreak_key, prov=provisional, now=now))

    log.info("leaderboard_written", competition_id=competition_id,
             entries=len(ranked), provisional=provisional)
    return len(ranked)


def _duration_ms(row) -> int:
    if row["started_at"] is None or row["submitted_at"] is None:
        return 0
    return max(0, int((row["submitted_at"] - row["started_at"]).total_seconds() * 1000))


def republish(session: Session, competition_id: int, *, now: dt.datetime,
              public_notice: str) -> int:
    """Rewrite a FINAL board after a recorded decision.

    The only path by which a published ranking changes. Every affected competitor
    is notified — if a podium moved, the people on it are told, and they are told
    the same words the admin wrote down when they decided.
    """
    before = {r["user_id"]: r["rank"] for r in session.execute(text(
        "SELECT user_id, rank FROM competition_results WHERE competition_id = :c"
    ).bindparams(c=competition_id)).mappings()}

    tiebreak = session.scalar(text("SELECT tiebreak FROM competitions WHERE id = :c")
                              .bindparams(c=competition_id))
    written = materialize(session, competition_id, tiebreak=tiebreak, now=now,
                          provisional=False)

    after = session.execute(text(
        "SELECT user_id, rank FROM competition_results WHERE competition_id = :c"
    ).bindparams(c=competition_id)).mappings().all()
    xid = session.scalar(text("SELECT xid FROM competitions WHERE id = :c")
                         .bindparams(c=competition_id))

    for row in after:
        if before.get(row["user_id"]) == row["rank"]:
            continue
        session.execute(text("""
            INSERT INTO notifications (user_id, channel, template, params, dedupe_key)
            VALUES (:u, 'in_app', 'competition.rank_changed', CAST(:p AS jsonb), :d)
            ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
        """).bindparams(
            u=row["user_id"],
            p=json.dumps({"competition_xid": str(xid),
                          "old_rank": before.get(row["user_id"]),
                          "new_rank": row["rank"], "notice": public_notice}),
            d=f"competition_republish:{competition_id}:{row['user_id']}:{row['rank']}"))
    return written
