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
from sqlalchemy.orm import Session, undefer

from app.api.deps import (
    Idempotency,
    Principal,
    db,
    entitlements,
    exam_session,
    idempotency,
    principal,
)
from app.api.dto import iso
from app.modules.billing.entitlements import Entitlements
from app.modules.competitions import service as competitions_service
from app.modules.competitions.schedule import Timing, registration_open
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
    # `invite` is gated at least as strictly as `org`: the hosting centre's
    # members, anyone already holding an entry, and a platform admin. It used
    # to fall through with `public`, so a contest a centre had marked "invited
    # entrants only" was open to any account holding the xid — register, pull
    # the encrypted paper, take the key at T-0, and appear on the board. Nothing
    # issues an invitation yet (0014 §8), so until it does the entry row IS the
    # invitation: an entrant seeded out-of-band keeps access to a contest they
    # are already in, which is the same rule the listing's EXISTS clause
    # applies. 404 rather than 403, as for `org`: a refusal must not confirm
    # the xid exists.
    if (row["visibility"] in ("org", "invite") and row["org_id"] not in actor.org_ids
            and not actor.is_platform_admin
            and not _holds_entry(session, row["id"], actor.user_id)):
        raise NotFound("Competition not found.")
    return row


def _holds_entry(session: Session, competition_id: int, user_id: int) -> bool:
    """Any entry, withdrawn included — matching the listing, and `channels.py`'s
    reasoning that hiding a contest from someone who pulled out would not
    un-tell them the questions."""
    return session.scalar(text("""
        SELECT 1 FROM competition_entries WHERE competition_id = :c AND user_id = :u
    """).bindparams(c=competition_id, u=user_id)) is not None


def competition_dto(session: Session, row, actor: Principal, *,
                    registered: int | None = None, mine=None, has_mine: bool = False,
                    org_xid: uuid.UUID | None = None) -> dict:
    """One contest, as the client sees it.

    The three lookups — the entrant count, the actor's own entry, the hosting
    centre's xid — are resolved ONCE PER PAGE by `list_competitions` and passed
    in; the single-row queries below are the fallback for `create_competition`,
    which has exactly one row (the bare `RETURNING *`) and no page to batch
    over. The per-row shape was the only one, and at the listing's `LIMIT 50`
    that was up to 151 statements to draw a list — the pattern this codebase
    hunts everywhere else (`list_questions`: "one query for the page's burn
    scores, not per row").

    `has_mine` is separate from `mine` because "the caller resolved it and
    there is no entry" and "the caller did not resolve it" both arrive as
    `None`; only the second should fall back to a query.
    """
    if registered is None:
        registered = session.scalar(text("""
            SELECT count(*) FROM competition_entries
            WHERE competition_id = :c AND status <> 'withdrawn'
        """).bindparams(c=row["id"])) or 0
    if not has_mine:
        mine = session.execute(text("""
            SELECT e.status, e.registered_at, a.xid AS attempt_xid
            FROM competition_entries e LEFT JOIN attempts a ON a.id = e.attempt_id
            WHERE e.competition_id = :c AND e.user_id = :u
        """).bindparams(c=row["id"], u=actor.user_id)).mappings().first()
    if org_xid is None and row["org_id"]:
        org_xid = session.scalar(text("SELECT xid FROM organizations WHERE id = :o")
                                 .bindparams(o=row["org_id"]))
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
    if not rows:
        return []

    # Three page-level lookups keyed by the page's ids, in place of three per
    # row: four statements for the listing whatever its length, rather than
    # 1 + 3 × 50. The same shape `list_questions` uses for versions and burn
    # scores, chosen over folding LATERAL joins into the SELECT above so that
    # `competition_dto` still works from the bare `RETURNING *` row in
    # `create_competition`.
    ids = [r["id"] for r in rows]
    registered = {r[0]: r[1] for r in session.execute(text("""
        SELECT competition_id, count(*) FROM competition_entries
        WHERE competition_id = ANY(:ids) AND status <> 'withdrawn'
        GROUP BY competition_id
    """).bindparams(ids=ids))}
    mine = {r["competition_id"]: r for r in session.execute(text("""
        SELECT e.competition_id, e.status, e.registered_at, a.xid AS attempt_xid
        FROM competition_entries e LEFT JOIN attempts a ON a.id = e.attempt_id
        WHERE e.competition_id = ANY(:ids) AND e.user_id = :u
    """).bindparams(ids=ids, u=actor.user_id)).mappings()}
    org_ids = sorted({r["org_id"] for r in rows if r["org_id"]})
    org_xids = {r[0]: r[1] for r in session.execute(text("""
        SELECT id, xid FROM organizations WHERE id = ANY(:ids)
    """).bindparams(ids=org_ids))} if org_ids else {}
    return [competition_dto(session, r, actor,
                            registered=registered.get(r["id"], 0),
                            mine=mine.get(r["id"]), has_mine=True,
                            org_xid=org_xids.get(r["org_id"]))
            for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_competition(body: CompetitionCreate, actor: Principal = Depends(principal),
                       session: Session = Depends(db)) -> dict:
    """Only a published version can back a contest, and the lobby window is set
    server-side. A five-second lobby would put 200 clients on the payload at once,
    which is exactly what the two-phase start exists to avoid.

    **And only a paper that has not been spent.** §41 stopped `/review` handing
    one contest's answers to another running on the same test version; this is
    the other end of the same problem, and the better end. A gate on review can
    only DEFER that leak — everyone who sat the first contest already knows the
    answers, and nothing served over HTTP takes that back. Refusing the second
    contest is the only place the ranking is actually saved.
    """
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
    _require_fresh_paper(session, tv.id)

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


def _require_fresh_paper(session: Session, test_version_id: int) -> None:
    """409 rather than a warning, and no override flag.

    A contest is a RANKING. A ranking computed over a field where some entrants
    have already seen the paper is not a slightly wrong number — it is a number
    that means nothing, published under the platform's name, next to the names of
    students who will screenshot it. There is no threshold of unfairness worth
    shipping behind a `--force`.

    The organiser's alternative is cheap. Composing a fresh version is the core
    feature of this product; reusing a spent paper saves an afternoon and costs
    the contest.
    """
    verdict = competitions_service.assess_paper(session, test_version_id)
    if verdict.fresh:
        return
    if verdict.reason == "already_contested":
        raise Conflict(
            f"'{verdict.contest}' already uses this paper. A contest ranks people "
            "against each other, so a paper somebody has already sat cannot back "
            "a second one.",
            code="paper_already_contested", contest=verdict.contest)
    raise Conflict(
        f"{verdict.burned_items} of this paper's {verdict.total_items} items are "
        "burned — they have circulated far enough that a good share of any field "
        "will have met them.",
        code="paper_items_burned", burned_items=verdict.burned_items,
        total_items=verdict.total_items, worst_burn=verdict.worst_burn)


@router.post("/{xid}/register", status_code=status.HTTP_201_CREATED)
def register(xid: uuid.UUID, actor: Principal = Depends(principal),
             session: Session = Depends(db),
             ents: Entitlements = Depends(entitlements),
             idem: Idempotency = Depends(idempotency)) -> dict:
    if replayed := idem.replay(f"competitions.register:{xid}", {}):
        return replayed

    row = _row(session, xid, actor)
    now = dt.datetime.now(dt.UTC)
    # Asks the pure state machine rather than re-deriving the rule here.
    #
    # `registration_open` existed, was tested at a hundred points in time, and was
    # called from nowhere — while this endpoint carried its own copy that checked
    # two of its three conditions. The missing one was `now < starts_at`, so a
    # contest still sitting in `registration` because the scheduler was behind
    # accepted entries after it had already begun. Two implementations of one rule
    # is what `schedule.py` was extracted to prevent.
    if not registration_open(
            Timing(status=row["status"], lobby_opens_at=row["lobby_opens_at"],
                   starts_at=row["starts_at"], ends_at=row["ends_at"],
                   registration_closes_at=row["registration_closes_at"]), now):
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

    # `prior` reads the pre-statement snapshot, so it reports the status this
    # entry had BEFORE the upsert — which is what decides whether this call owes
    # an entry. A plain `RETURNING` cannot answer it: it sees the new row, and
    # "registered" looks identical whether this call caused it or it was already
    # true. Kept as one statement so the upsert keeps the concurrency property it
    # was written for; a SELECT-then-write pair reintroduces the lost-update race
    # between two simultaneous registrations.
    entry = session.execute(text("""
        WITH prior AS (
            SELECT status FROM competition_entries
            WHERE competition_id = :c AND user_id = :u
        ), upserted AS (
            INSERT INTO competition_entries (competition_id, user_id)
            VALUES (:c, :u)
            ON CONFLICT (competition_id, user_id) DO UPDATE
            SET status = CASE WHEN competition_entries.status = 'withdrawn'
                              THEN 'registered' ELSE competition_entries.status END
            RETURNING status, registered_at, attempt_id
        )
        SELECT u.status, u.registered_at, u.attempt_id,
               (SELECT status FROM prior) AS prior_status
        FROM upserted u
    """).bindparams(c=row["id"], u=actor.user_id)).mappings().one()
    session.flush()

    # **`Entitlements.consume` had no caller anywhere in the product**, so
    # `entitlements.quantity` was written honestly by `_grant_for_order` and then
    # never decremented: a `competition.entry` pack of four was four for ever.
    # `require` above already refuses at zero — `status_at` returns EXHAUSTED
    # when `remaining` hits it — so the gate was correct and only the spending
    # was missing.
    #
    # Charged HERE rather than beside that gate, because everything between the
    # two can still refuse: a full competition raises 409 above, and an entry
    # deducted before it would be spent on a registration that never happened.
    #
    # Only on the transition INTO `registered`. Registering twice while already
    # registered changes nothing and must cost nothing — the upsert is a no-op on
    # that path and this has to agree with it, or the second call silently bills
    # a student for the entry they already hold.
    if entry["prior_status"] in (None, "withdrawn"):
        spent = ents.consume(user_xid=str(actor.user_id), feature="competition.entry",
                             org_xids=[str(o) for o in actor.org_ids])
        # `consume` returns EXHAUSTED rather than raising, and the entry row is
        # already written by now. Refusing here rolls the whole request back,
        # which is the only outcome that leaves the student neither registered
        # nor charged.
        spent.raise_if_denied("competition.entry")
        if spent.entitlement is not None:
            session.execute(text("""
                UPDATE competition_entries SET entitlement_id = :e
                WHERE competition_id = :c AND user_id = :u
            """).bindparams(e=int(spent.entitlement.xid), c=row["id"],
                            u=actor.user_id))
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
             session: Session = Depends(db),
             ents: Entitlements = Depends(entitlements)) -> Response:
    """Withdrawing before the contest returns the entry to the pack.

    An entry is spent while you HOLD a registration, which is the version of
    this a payer can be told in one sentence. The alternative — burning the unit
    at registration and keeping it on withdrawal — charges a student for a
    contest they never sat.
    """
    row = _row(session, xid, actor)
    if row["status"] in ("live", "grading", "final"):
        raise Conflict("You cannot withdraw once the contest has started.",
                       code="competition_started")
    # `RETURNING` rather than a blind UPDATE, and the guard is what makes the
    # refund safe: `status = 'registered'` matches at most once, so a second
    # withdrawal updates nothing, returns nothing, and refunds nothing. Without
    # that, repeated DELETEs would credit a unit each time — free entries from
    # an endpoint whose 204 invites the client to retry.
    released = session.execute(text("""
        UPDATE competition_entries SET status = 'withdrawn'
        WHERE competition_id = :c AND user_id = :u AND status = 'registered'
        RETURNING entitlement_id
    """).bindparams(c=row["id"], u=actor.user_id)).mappings().first()
    # NULL means no consumable was spent on this entry — an unlimited plan, or a
    # registration made before entries were charged at all. Nothing to give back.
    if released and released["entitlement_id"] is not None:
        ents.refund(str(released["entitlement_id"]))
        session.flush()
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

    # The paper is what this endpoint is for; `snapshot` is deferred for every
    # reader that wants a scalar, so ask for it on the same SELECT.
    tv = session.get(TestVersion, row["test_version_id"],
                     options=[undefer(TestVersion.snapshot)])
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
