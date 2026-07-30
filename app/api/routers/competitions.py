"""Competitions: registration, the synchronized start, and the regrade decision.

Two things here are load-bearing and neither is obvious:

**The two-phase start.** At T-120s every registered client prefetches the test
payload — AES-GCM *encrypted*, with per-client jitter so 200 devices do not all
pull 1.6 MB in the same second. At T-0 they each fetch a ~100-byte key. The
encryption is not theatre: an unencrypted payload sitting on the device from
T-120s is a two-minute reading head start for anyone who opens devtools.

**Key freeze.** A competition captures `{key_versions, band_map_version_id}` when
it starts. A later answer-key fix regrades practice attempts freely but CANNOT
silently re-rank a finished contest — that requires a recorded human decision
(ADR-0001 §8.4). A leaderboard that changes by itself looks like fraud.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import secrets
import uuid

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import (
    Idempotency, Principal, db, entitlements, exam_session, idempotency, principal,
)
from app.api.dto import iso
from app.modules.billing.entitlements import Entitlements
from app.modules.content.models import Test, TestVersion
from app.modules.exam.models import Attempt, Outbox
from app.modules.exam.session import ExamSession
from app.platform.config import settings
from app.platform.errors import Conflict, Forbidden, NotFound, TooEarly

router = APIRouter(prefix="/competitions", tags=["competitions"])

LOBBY_LEAD = dt.timedelta(seconds=120)


class CompetitionCreate(BaseModel):
    title: str
    description: str | None = None
    test_version_xid: uuid.UUID
    org_xid: uuid.UUID | None = None
    visibility: str = Field(default="org", pattern="^(public|org|invite)$")
    starts_at: dt.datetime
    duration_seconds: int = Field(gt=0)
    lobby_opens_at: dt.datetime | None = None
    registration_closes_at: dt.datetime | None = None
    max_participants: int | None = None
    tiebreak: list[str] = ["raw_score_desc", "duration_asc", "submitted_at_asc"]


def _row(session: Session, xid: uuid.UUID, actor: Principal):
    row = session.execute(text("""
        SELECT c.*, tv.xid AS test_version_xid, tv.snapshot IS NOT NULL AS has_snapshot
        FROM competitions c JOIN test_versions tv ON tv.id = c.test_version_id
        WHERE c.xid = CAST(:x AS uuid)
    """).bindparams(x=xid)).mappings().first()
    if row is None:
        raise NotFound("Competition not found.")
    if (row["visibility"] == "org" and row["org_id"] not in actor.org_ids
            and not actor.is_platform_admin):
        raise NotFound("Competition not found.")
    return row


def competition_dto(session: Session, row, actor: Principal) -> dict:
    registered = session.scalar(text("""
        SELECT count(*) FROM competition_entries
        WHERE competition_id = :c AND status <> 'withdrawn'
    """).bindparams(c=row["id"])) or 0
    mine = session.execute(text("""
        SELECT e.status, e.registered_at, a.xid AS attempt_xid
        FROM competition_entries e LEFT JOIN attempts a ON a.id = e.attempt_id
        WHERE e.competition_id = :c AND e.user_id = :u
    """).bindparams(c=row["id"], u=actor.user_id)).mappings().first()
    org_xid = session.scalar(text("SELECT xid FROM organizations WHERE id = :o")
                             .bindparams(o=row["org_id"])) if row["org_id"] else None
    return {
        "xid": str(row["xid"]), "title": row["title"],
        "description": row["description"],
        "org_xid": str(org_xid) if org_xid else None,
        "status": row["status"], "visibility": row["visibility"],
        "lobby_opens_at": iso(row["lobby_opens_at"]), "starts_at": iso(row["starts_at"]),
        "ends_at": iso(row["ends_at"]), "duration_seconds": row["duration_seconds"],
        "max_participants": row["max_participants"], "registered_count": registered,
        "my_entry": ({"status": mine["status"],
                      "registered_at": iso(mine["registered_at"]),
                      "attempt_xid": str(mine["attempt_xid"])
                      if mine["attempt_xid"] else None} if mine else None),
        # Every countdown in the client is rendered from this delta, never from
        # the device clock.
        "server_now": iso(dt.datetime.now(dt.UTC)),
    }


@router.get("")
def list_competitions(scope: str = "visible", state: str | None = None,
                      actor: Principal = Depends(principal),
                      session: Session = Depends(db)) -> list[dict]:
    """Public contests plus the actor's own centres'. An invite-only contest never
    appears here — it reaches its participants by invitation."""
    clauses = ["c.visibility = 'public'"]
    params: dict = {"orgs": list(actor.org_ids) or [0], "u": actor.user_id}
    clauses.append("(c.visibility = 'org' AND c.org_id = ANY(:orgs))")
    clauses.append("EXISTS (SELECT 1 FROM competition_entries e "
                   "WHERE e.competition_id = c.id AND e.user_id = :u)")
    where = f"({' OR '.join(clauses)})"
    if state == "upcoming":
        where += " AND c.starts_at > now() AND c.status <> 'cancelled'"
    elif state == "live":
        where += " AND c.status IN ('lobby','live')"
    elif state == "finished":
        where += " AND c.status IN ('grading','final')"
    rows = session.execute(text(f"""
        SELECT c.* FROM competitions c WHERE {where}
        ORDER BY c.starts_at DESC LIMIT 50
    """).bindparams(**params)).mappings().all()
    return [competition_dto(session, r, actor) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_competition(body: CompetitionCreate, actor: Principal = Depends(principal),
                       session: Session = Depends(db)) -> dict:
    """Only a published version can back a contest, and the lobby window is set
    server-side. A five-second lobby would put 200 clients on the payload at once,
    which is exactly what the two-phase start exists to avoid."""
    from app.modules.authz import policy
    from app.modules.authz.policy import Action, Resource

    row = session.execute(
        select(TestVersion, Test).join(Test, Test.id == TestVersion.test_id)
        .where(TestVersion.xid == body.test_version_xid)).first()
    if row is None:
        raise NotFound("Test version not found.")
    tv, test = row
    if tv.status != "published":
        raise Conflict("Only a published version can back a competition.",
                       code="version_not_published")
    policy.require(actor, Action.READ,
                   Resource(org_id=test.org_id, owner_user_id=test.owner_user_id,
                            visibility=test.visibility))

    org_id = None
    if body.visibility != "public":
        org_id = next((o for o, r in actor.roles.items()
                       if r in ("teacher", "centre_admin")), None)
        if org_id is None and not actor.is_platform_admin:
            raise Forbidden("Only a centre can run an org competition.",
                            code="not_a_centre")
    elif not actor.is_platform_admin:
        raise Forbidden("A public competition is a platform-admin action.",
                        code="admin_only")

    lobby = body.lobby_opens_at or (body.starts_at - LOBBY_LEAD)
    if lobby > body.starts_at:
        raise Conflict("The lobby must open before the contest starts.",
                       code="invalid_lobby_window")

    created = session.execute(text("""
        INSERT INTO competitions (org_id, test_version_id, title, description,
                                  registration_closes_at, lobby_opens_at, starts_at,
                                  ends_at, duration_seconds, status, visibility,
                                  max_participants, tiebreak, payload_key_id, created_by)
        VALUES (:org, :tv, :title, :descr, :reg_close, :lobby, :starts,
                :starts + make_interval(secs => :dur), :dur, 'registration', :vis,
                :maxp, CAST(:tiebreak AS jsonb), :key_id, :by)
        RETURNING *
    """).bindparams(org=org_id, tv=tv.id, title=body.title, descr=body.description,
                    reg_close=body.registration_closes_at, lobby=lobby,
                    starts=body.starts_at, dur=body.duration_seconds,
                    vis=body.visibility, maxp=body.max_participants,
                    tiebreak=json.dumps(body.tiebreak),
                    key_id=secrets.token_hex(8), by=actor.user_id)).mappings().one()
    session.flush()
    return competition_dto(session, created, actor)


@router.post("/{xid}/register", status_code=status.HTTP_201_CREATED)
def register(xid: uuid.UUID, actor: Principal = Depends(principal),
             session: Session = Depends(db),
             ents: Entitlements = Depends(entitlements),
             idem: Idempotency = Depends(idempotency)) -> dict:
    if replayed := idem.replay(f"competitions.register:{xid}", {}):
        return replayed

    row = _row(session, xid, actor)
    now = dt.datetime.now(dt.UTC)
    if row["status"] not in ("scheduled", "registration"):
        raise Conflict("Registration for this competition has closed.",
                       code="registration_closed")
    if row["registration_closes_at"] and row["registration_closes_at"] < now:
        raise Conflict("Registration for this competition has closed.",
                       code="registration_closed")
    ents.require(user_xid=str(actor.user_id), feature="competition.entry",
                 org_xids=[str(o) for o in actor.org_ids])

    if row["max_participants"]:
        # The lock is on the COMPETITION row, not on the entries.
        #
        # This was `SELECT count(*) ... FOR UPDATE`, which PostgreSQL rejects
        # outright — "FOR UPDATE is not allowed with aggregate functions" — so
        # every registration for a capped contest returned 500. The feature was
        # not merely unenforced; it made the endpoint unusable.
        #
        # And the lock it was reaching for would not have worked either: locking
        # the rows that already exist cannot stop a concurrent INSERT of a new
        # one. Serialising on the parent row is what actually makes two
        # simultaneous registrations for the last place mutually exclusive.
        session.execute(text("SELECT id FROM competitions WHERE id = :c FOR UPDATE")
                        .bindparams(c=row["id"]))
        taken = session.scalar(text("""
            SELECT count(*) FROM competition_entries
            WHERE competition_id = :c AND status <> 'withdrawn'
        """).bindparams(c=row["id"])) or 0
        if taken >= row["max_participants"]:
            raise Conflict("This competition is full.", code="competition_full")

    entry = session.execute(text("""
        INSERT INTO competition_entries (competition_id, user_id)
        VALUES (:c, :u)
        ON CONFLICT (competition_id, user_id) DO UPDATE
        SET status = CASE WHEN competition_entries.status = 'withdrawn'
                          THEN 'registered' ELSE competition_entries.status END
        RETURNING status, registered_at, attempt_id
    """).bindparams(c=row["id"], u=actor.user_id)).mappings().one()
    session.flush()

    # Was hardcoded null. It IS null for a fresh registration, but re-registering
    # after a withdrawal returns an entry that already has an attempt, and the
    # client had no way to find it.
    attempt_xid = session.scalar(
        select(Attempt.xid).where(Attempt.id == entry["attempt_id"])
    ) if entry["attempt_id"] else None
    payload = {"status": entry["status"], "registered_at": iso(entry["registered_at"]),
               "attempt_xid": str(attempt_xid) if attempt_xid else None}
    idem.store({}, payload, status.HTTP_201_CREATED)
    return payload


@router.delete("/{xid}/register", status_code=status.HTTP_204_NO_CONTENT)
def withdraw(xid: uuid.UUID, actor: Principal = Depends(principal),
             session: Session = Depends(db)) -> Response:
    row = _row(session, xid, actor)
    if row["status"] in ("live", "grading", "final"):
        raise Conflict("You cannot withdraw once the contest has started.",
                       code="competition_started")
    session.execute(text("""
        UPDATE competition_entries SET status = 'withdrawn'
        WHERE competition_id = :c AND user_id = :u AND status = 'registered'
    """).bindparams(c=row["id"], u=actor.user_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── the synchronized start ───────────────────────────────────────────

@router.get("/{xid}/lobby")
def lobby(xid: uuid.UUID, actor: Principal = Depends(principal),
          session: Session = Depends(db)) -> dict:
    """Step one: the encrypted payload, T-120s → T-0.

    `fetch_after` is a per-client jitter derived deterministically from the user
    and the contest, so the same client always gets the same slot and a retry does
    not stampede. Spreading 200 clients across the lobby window turns a 320 Mbps
    burst into a trickle.
    """
    row = _row(session, xid, actor)
    now = dt.datetime.now(dt.UTC)
    if now < row["lobby_opens_at"]:
        raise TooEarly("The lobby has not opened yet.", code="lobby_not_open",
                       lobby_opens_at=iso(row["lobby_opens_at"]),
                       server_now=iso(now))

    entry = session.execute(text("""
        SELECT id, status FROM competition_entries
        WHERE competition_id = :c AND user_id = :u AND status <> 'withdrawn'
    """).bindparams(c=row["id"], u=actor.user_id)).mappings().first()
    if entry is None:
        raise Forbidden("You are not registered for this competition.",
                        code="not_registered")

    tv = session.get(TestVersion, row["test_version_id"])
    if tv is None or tv.snapshot is None:
        raise Conflict("This competition's test has no published payload.",
                       code="payload_missing")

    ciphertext, iv = _encrypt_payload(tv, row["payload_key_id"])
    session.execute(text("""
        UPDATE competition_entries
        SET payload_fetched_at = coalesce(payload_fetched_at, now()),
            status = CASE WHEN status = 'registered' THEN 'prefetched' ELSE status END
        WHERE id = :e
    """).bindparams(e=entry["id"]))

    window = max(1, int((row["starts_at"] - row["lobby_opens_at"]).total_seconds()) - 10)
    offset = _jitter(actor.user_id, str(row["xid"]), window)
    return {
        "ciphertext": ciphertext, "iv": iv, "key_id": row["payload_key_id"],
        "starts_at": iso(row["starts_at"]), "server_now": iso(now),
        "fetch_after": iso(row["lobby_opens_at"] + dt.timedelta(seconds=offset)),
    }


def _jitter(user_id: int, competition_xid: str, window_seconds: int) -> int:
    """Deterministic, so a client that retries lands in the same slot rather than
    rolling the dice again and possibly landing on the spike it was avoiding."""
    digest = hashlib.sha256(f"{competition_xid}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % max(1, window_seconds)


def _encrypt_payload(tv: TestVersion, key_id: str) -> tuple[str, str]:
    """AES-GCM over the published snapshot.

    In production the blob is built once per contest and cached in Redis; the key
    lives in Redis too and is never persisted, which is why a database dump of a
    scheduled contest leaks nothing. Rebuilding it here keeps the code path
    identical when the cache is cold.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _payload_key(key_id)
    iv = hashlib.sha256(f"iv:{key_id}".encode()).digest()[:12]
    plaintext = json.dumps(tv.snapshot, separators=(",", ":"), default=str).encode()
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)
    return (base64.b64encode(ciphertext).decode(), base64.b64encode(iv).decode())


def _payload_key(key_id: str) -> bytes:
    """Derived from the server secret and the contest's key id.

    Deriving rather than storing means the key for a contest that has not started
    exists nowhere at rest — not in Postgres, not in a backup.
    """
    return hashlib.sha256(f"{settings().jwt_secret}:payload:{key_id}".encode()).digest()


@router.post("/{xid}/key")
def release_key(xid: uuid.UUID, actor: Principal = Depends(principal),
                session: Session = Depends(db),
                exam: ExamSession = Depends(exam_session)) -> dict:
    """Step two: ~100 bytes at T-0, so 200 simultaneous requests are a rounding
    error rather than a thundering herd.

    Also creates the attempt and starts the clock from `starts_at`, NOT from when
    the client happened to ask. A student on a slow connection who asks three
    seconds late does not get three extra seconds, and one who asks early does not
    start early.
    """
    row = _row(session, xid, actor)
    now = dt.datetime.now(dt.UTC)
    if now < row["starts_at"]:
        raise TooEarly("The contest has not started yet.", code="not_started",
                       starts_at=iso(row["starts_at"]), server_now=iso(now))
    if now > row["ends_at"]:
        raise Conflict("This contest has finished.", code="competition_finished")

    entry = session.execute(text("""
        SELECT id, status, attempt_id FROM competition_entries
        WHERE competition_id = :c AND user_id = :u AND status <> 'withdrawn'
    """).bindparams(c=row["id"], u=actor.user_id)).mappings().first()
    if entry is None:
        raise Forbidden("You are not registered for this competition.",
                        code="not_registered")
    if entry["status"] == "disqualified":
        raise Forbidden("Your entry has been disqualified.", code="disqualified")

    if entry["attempt_id"]:
        attempt = session.get(Attempt, entry["attempt_id"])
    else:
        attempt = exam.start(user_id=actor.user_id,
                             test_version_id=row["test_version_id"], mode="exam")
        attempt.competition_id = row["id"]
        attempt.org_context_id = row["org_id"]
        # Everyone's deadline is the contest's, not their own start time.
        attempt.started_at = row["starts_at"]
        attempt.expires_at = min(
            row["starts_at"] + dt.timedelta(seconds=row["duration_seconds"]),
            row["ends_at"])
        session.flush()
        session.execute(text("""
            UPDATE competition_entries
            SET attempt_id = :a, status = 'started',
                key_released_at = coalesce(key_released_at, now())
            WHERE id = :e
        """).bindparams(a=attempt.id, e=entry["id"]))
        _freeze_keys(session, row)

    return {
        "key": base64.b64encode(_payload_key(row["payload_key_id"])).decode(),
        "attempt": {
            "xid": str(attempt.xid), "status": attempt.status, "mode": attempt.mode,
            "attempt_no": attempt.attempt_no,
            "expires_at": iso(attempt.expires_at), "server_now": iso(now),
            "seconds_remaining": max(0, int((attempt.expires_at - now).total_seconds()))
            if attempt.expires_at else None,
        },
        "server_now": iso(now),
    }


def _freeze_keys(session: Session, row) -> None:
    """Capture the exact scoring inputs on first start.

    This is what makes "a key fix cannot silently re-rank a finished contest" an
    enforceable statement rather than a promise: the contest records which key
    versions it was scored against, so a later fix is visibly a *different* set of
    inputs and needs a decision.
    """
    if row["key_freeze"]:
        return
    session.execute(text("""
        UPDATE competitions SET key_freeze = jsonb_build_object(
            'key_versions', (
                SELECT coalesce(jsonb_object_agg(qv.id::text, akv.id), '{}'::jsonb)
                FROM test_version_sections s
                JOIN test_version_groups g ON g.section_id = s.id
                JOIN question_group_items i ON i.group_version_id = g.group_version_id
                JOIN question_versions qv ON qv.id = i.question_version_id
                LEFT JOIN answer_key_versions akv
                       ON akv.question_version_id = qv.id AND akv.is_current
                WHERE s.test_version_id = :tv),
            'band_map_version_id', (SELECT band_map_version_id FROM test_versions
                                    WHERE id = :tv),
            'frozen_at', to_jsonb(now())),
            status = 'live'
        WHERE id = :c AND key_freeze IS NULL
    """).bindparams(tv=row["test_version_id"], c=row["id"]))


# ── results ──────────────────────────────────────────────────────────

@router.get("/{xid}/leaderboard", response_model=None)
def leaderboard(xid: uuid.UUID, response: Response, around_me: bool = False,
                limit: int = 25, if_none_match: str | None = Header(default=None),
                actor: Principal = Depends(principal),
                session: Session = Depends(db)) -> dict | Response:
    """Provisional while the contest is live.

    Display name only. A leaderboard is the most-screenshotted surface in the
    product and it must never carry an age, a phone number or a centre name.

    **Ordered by the stored `rank`, not by re-deriving the sort.** `tiebreak` is
    per-contest data ("so a centre can run 'highest score, then fastest' without a
    deploy") and `materialize()` already evaluated it. This query used to hardcode
    the DEFAULT ordering and ignore the `rank` column it was selecting, so a
    contest configured any other way returned rank numbers that contradicted the
    order of the rows they were printed beside — and under `LIMIT`, could cut the
    actual winner off the top of their own board. Ordering by `rank` is also an
    index scan (`competition_results_rank_idx`), so honouring the contest costs
    nothing.

    `around_me` windows on the viewer's own position instead of the top, which is
    the only useful view for the ~90% of a 500-person field who are not in the
    first 25 rows.
    """
    row = _row(session, xid, actor)
    limit = max(1, min(limit, 100))
    rows = session.execute(text("""
        WITH board AS (
            SELECT r.rank, r.raw_score, r.band, r.duration_ms, r.user_id,
                   u.xid AS user_xid, u.given_name, u.family_name,
                   row_number() OVER (
                       ORDER BY r.rank ASC NULLS LAST, r.raw_score DESC,
                                r.duration_ms ASC, r.submitted_at ASC) AS position
            FROM competition_results r JOIN users u ON u.id = r.user_id
            WHERE r.competition_id = :c
        ), me AS (
            SELECT position FROM board WHERE user_id = :u
        )
        SELECT * FROM board
        WHERE NOT :around
           OR (SELECT position FROM me) IS NULL
           OR position BETWEEN greatest(1, (SELECT position FROM me) - :half)
                           AND (SELECT position FROM me) + :half
        ORDER BY position
        LIMIT :lim
    """).bindparams(c=row["id"], u=actor.user_id, around=around_me,
                    half=limit // 2, lim=limit)).mappings().all()

    my_rank = session.scalar(text("""
        SELECT rank FROM competition_results
        WHERE competition_id = :c AND user_id = :u
    """).bindparams(c=row["id"], u=actor.user_id))

    entries = [{
        "rank": r["rank"], "raw_score": float(r["raw_score"]),
        "band": float(r["band"]) if r["band"] is not None else None,
        "duration_ms": r["duration_ms"],
        "user": {"xid": str(r["user_xid"]),
                 "display_name": _display_name(r["given_name"], r["family_name"])},
    } for r in rows]

    provisional = row["status"] != "final"
    # Hashed over the body rather than over `max(computed_at)`, because the tag
    # has to distinguish VIEWS as well as versions: a client switching between the
    # top and its own neighbourhood must not be served a 304 for the other one.
    # The saving is the transfer, not the query — which is the right way round on
    # a mobile network, and this path exists precisely for clients that cannot
    # hold a socket open.
    etag = _board_etag(entries, my_rank, provisional)
    response.headers["ETag"] = etag
    if if_none_match and etag in {t.strip() for t in if_none_match.split(",")}:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED,
                        headers={"ETag": etag})
    if not provisional:
        response.headers["Cache-Control"] = "public, max-age=60"
    return {
        "competition_xid": str(row["xid"]), "is_provisional": provisional,
        "generated_at": iso(dt.datetime.now(dt.UTC)), "my_rank": my_rank,
        "entries": entries,
    }


def _board_etag(entries: list[dict], my_rank: int | None, provisional: bool) -> str:
    """Excludes `generated_at`: a provisional board would otherwise mint a new tag
    on every poll, which is exactly the case the tag exists to serve."""
    material = json.dumps([entries, my_rank, provisional], separators=(",", ":"),
                          sort_keys=True)
    return f'"{hashlib.sha256(material.encode()).hexdigest()[:32]}"'


def _display_name(given: str, family: str | None) -> str:
    """First name plus an initial. Enough to recognise yourself and a classmate,
    not enough to identify a stranger from a screenshot."""
    return f"{given} {family[0]}." if family else given


@router.get("/{xid}/results", response_model=None)
def results(xid: uuid.UUID, response: Response, actor: Principal = Depends(principal),
            session: Session = Depends(db)) -> dict | Response:
    row = _row(session, xid, actor)
    if row["status"] not in ("grading", "final"):
        raise Conflict("This contest has not finished.", code="not_finished")
    # No `If-None-Match` forwarded: the final board is the archival view and is
    # already cacheable by max-age. Conditional requests are for the polling path.
    return leaderboard(xid, response, around_me=False, limit=100,
                       if_none_match=None, actor=actor, session=session)


class RegradeDecision(BaseModel):
    decision: str = Field(pattern="^(leave_as_is|regrade_and_republish)$")
    rationale: str
    public_notice: str | None = None


@router.post("/{xid}/regrade-decisions/{job_xid}")
def decide_regrade(xid: uuid.UUID, job_xid: uuid.UUID, body: RegradeDecision,
                   actor: Principal = Depends(principal),
                   session: Session = Depends(db)) -> dict:
    """Platform admin only, and an audit record names the human who chose.

    `leave_as_is` keeps the published ranking and records why. `regrade_and_
    republish` recomputes and REQUIRES a public notice — if a podium moves, the
    people on it are told, in writing, by name of the decision that moved them.
    """
    if not actor.is_platform_admin:
        raise Forbidden("Deciding a competition regrade is a platform-admin action.",
                        code="admin_only")
    if body.decision == "regrade_and_republish" and not body.public_notice:
        raise Conflict("Republishing a changed ranking requires a public notice.",
                       code="public_notice_required")

    row = _row(session, xid, actor)
    job = session.execute(text("SELECT id, xid FROM regrade_jobs WHERE xid = CAST(:x AS uuid)")
                          .bindparams(x=job_xid)).mappings().first()
    if job is None:
        raise NotFound("Regrade job not found.")

    decided = session.execute(text("""
        INSERT INTO competition_regrade_decisions
            (competition_id, regrade_job_id, impact, decision, decided_by, decided_at,
             rationale, public_notice)
        VALUES (:c, :j, coalesce((SELECT to_jsonb(competition_impact)
                                  FROM regrade_jobs WHERE id = :j), '{}'::jsonb),
                :decision, :who, now(), :rationale, :notice)
        ON CONFLICT (competition_id, regrade_job_id) DO UPDATE
        SET decision = EXCLUDED.decision, decided_by = EXCLUDED.decided_by,
            decided_at = now(), rationale = EXCLUDED.rationale,
            public_notice = EXCLUDED.public_notice
        RETURNING id, decision, impact, rationale, public_notice, decided_at
    """).bindparams(c=row["id"], j=job["id"], decision=body.decision,
                    who=actor.user_id, rationale=body.rationale,
                    notice=body.public_notice)).mappings().one()

    session.execute(text("""
        INSERT INTO audit_log (actor_kind, actor_user_id, org_id, action, subject_type,
                               subject_id, after, reason)
        VALUES ('admin', :who, :org, 'competition.regrade_decision', 'competition',
                :sid, CAST(:after AS jsonb), :reason)
    """).bindparams(who=actor.user_id, org=row["org_id"], sid=str(row["xid"]),
                    after=json.dumps(
                        {"decision": body.decision, "regrade_job_xid": str(job_xid),
                         "public_notice": body.public_notice}),
                    reason=body.rationale))

    if body.decision == "regrade_and_republish":
        session.add(Outbox(aggregate_type="competition", aggregate_id=str(row["id"]),
                           event_type="competition.regrade_approved",
                           payload={"competition_xid": str(row["xid"]),
                                    "regrade_job_xid": str(job_xid),
                                    "public_notice": body.public_notice}))
    session.flush()

    from app.api.routers.identity import user_dto
    from app.modules.identity.models import User

    who = session.get(User, actor.user_id)
    return {"xid": str(uuid.UUID(int=decided["id"])), "decision": decided["decision"],
            "impact": decided["impact"], "rationale": decided["rationale"],
            "public_notice": decided["public_notice"],
            "decided_by": user_dto(who) if who else None,
            "decided_at": iso(decided["decided_at"])}
