"""Realtime channel authorization: who may read a topic, and what may travel on it.

`policy.py` answers *may this actor act on this resource*. A WebSocket asks a
different question with the same stakes — *may this connection READ this stream*
— and the tempting answer is a handful of `if` statements in the gateway. That is
exactly how two permission models come into existence: the HTTP side refuses a
competitor centre, and the socket, six months later, does not, because nobody
remembered there were two places.

So the channel matrix lives here, beside the resource matrix, as data, and the
org-scoped half of it delegates to `policy.check` rather than re-deriving what a
membership means. One engine, two surfaces.

Four properties are load-bearing, and every one of them is verified by breaking
it and watching a test fail:

  * **A channel's subject is looked up, never asserted.** The client sends
    `attempt:<xid>`; the server asks the database who owns that attempt. Nothing
    in the frame is trusted, because the frame is written by the attacker.
  * **A refusal tells the client `forbidden_channel` and nothing more.** The
    specific reason is returned for the log and never for the wire — otherwise
    the difference between "no such competition" and "not your competition" is
    an oracle that maps which xids exist and which centre owns them.
  * **A malformed subject never reaches SQL.** `CAST('nonsense' AS uuid)` raises
    inside the transaction, and in this codebase an aborted transaction takes
    every later statement on that session down with it. Subjects are parsed as
    UUIDs here, before a query is built.
  * **An event may only travel on the family that declares it.** `EVENTS` below
    is the whole list; publishing `assignment.progress` — a teacher's view of
    forty students — onto a personal `user:` channel is refused at the publish
    call, not caught in review.

The channel grammar and the seven subject families come from
`docs/design/0003-api-contract.md` §4.4. `safety` is the one addition, and it is
there because a moderation queue that any authenticated socket could subscribe to
would be the single worst leak in the product.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.authz import policy
from app.modules.authz.policy import Action, Resource, Role

# The two `RtError.code` values a subscribe refusal may carry. The contract
# declares both; which one is sent is decided here and only here.
UNKNOWN_CHANNEL = "unknown_channel"
FORBIDDEN_CHANNEL = "forbidden_channel"

# PostgreSQL `bigint`. A queue channel names a row id, and an id past this is not
# a row that can exist — checked here so it never becomes a numeric overflow
# inside a transaction other statements are sharing.
_MAX_BIGINT = 2**63 - 1


class Actor(Protocol):
    """`api.deps.Principal`, structurally. `user_xid` is the addition over
    `policy.Actor`: the personal channel is named by the public id, and comparing
    it to an internal integer would silently never match."""

    user_id: int
    user_xid: str
    org_ids: tuple[int, ...]
    roles: dict[int, str]
    platform_roles: tuple[str, ...]

    @property
    def is_platform_admin(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Channel:
    family: str
    subject: str = ""

    def __str__(self) -> str:
        return f"{self.family}:{self.subject}" if self.subject else self.family


@dataclass(frozen=True, slots=True)
class Subject:
    """What the database says about the thing a channel names.

    Four fields, because four are all any of the eight families need: who is a
    party to it, which organization owns it, who created it — and, for the
    personal channel alone, the PUBLIC id it is named by, since comparing an xid
    to an internal integer would silently never match.
    """

    member_user_ids: frozenset[int] = frozenset()
    org_id: int | None = None
    owner_user_id: int | None = None
    owner_user_xid: str | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    """`code` goes to the client, `reason` goes to the log. Never the other way."""

    allowed: bool
    code: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Family:
    """One channel family: how to load its subject, and who may read it."""

    name: str
    events: frozenset[str]
    #: `None` for a family with no subject to look up (`safety`).
    load: Callable[[Session, Channel], Subject | None] | None
    rule: Callable[[Actor, Subject], Verdict]
    #: False only for `safety`, which is a single global topic.
    has_subject: bool = True


def _allow(reason: str) -> Verdict:
    return Verdict(True, reason=reason)


def _deny(reason: str) -> Verdict:
    return Verdict(False, FORBIDDEN_CHANNEL, reason)


# ── the rules ────────────────────────────────────────────────────────

def _self_only(actor: Actor, subject: Subject) -> Verdict:
    """The personal channel, and the one place a platform admin gets NO bypass.

    `user:` carries `session.revoked` and `notification` — a ban landing, a band
    changing, a booking reminder. There is no operational task that requires
    reading another person's copy of those in real time, and the admin bypass
    that is right for content (`policy.check` grants it) would here be a
    supervisor silently tailing a student's stream.
    """
    if subject.owner_user_xid == actor.user_xid:
        return _allow("self")
    return _deny("not_self")


def _party_to_it(actor: Actor, subject: Subject) -> Verdict:
    """Attempt owner, pair peer, slot check-in, queue entry, contest entrant.

    Membership of the subject, resolved from the database. A platform admin is
    admitted because every one of these is a support or safety investigation
    surface — "which two people were in that call" is the first question a
    grooming report raises.
    """
    if actor.user_id in subject.member_user_ids:
        return _allow("party")
    if actor.is_platform_admin:
        return _allow("platform_admin")
    return _deny("not_a_party")


def _centre_staff(actor: Actor, subject: Subject) -> Verdict:
    """The invigilation channel, and the contractual promise it protects.

    `assignment.progress` is one centre's live view of its own students sitting
    its own paper. Two refusals, in this order:

      1. `policy.check(READ, org_private)` — the SAME call the HTTP side makes,
         which is what stops a competitor centre. Delegated rather than rewritten
         as `org_id in actor.org_ids`, because the day that rule gains a case
         (a content grant, a franchise group) the socket must gain it too.
      2. the role, because org membership includes the STUDENTS. A student at the
         centre passing check 1 would otherwise read every classmate's progress.

    The teacher who set the assignment is admitted regardless of the current role
    table: they may have been moved to another centre since, and it is still
    their assignment.
    """
    if subject.owner_user_id == actor.user_id:
        return _allow("assigner")
    if not policy.check(actor, Action.READ,
                        Resource(org_id=subject.org_id, visibility="org_private")).allowed:
        return _deny("not_this_centre")
    if policy.role_of(actor, subject.org_id) not in (
            Role.TEACHER, Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN):
        return _deny("not_centre_staff")
    return _allow("centre_staff")


def _staff_only(actor: Actor, subject: Subject) -> Verdict:
    """The moderation stream. Platform admin, and nothing else.

    Reports name a reporter, a subject and a category, and the highest-priority
    ones name minors. This is the channel where a mistaken `True` is a
    child-safety incident rather than a bug, so it is the one family whose rule
    reads a single flag with no path around it.
    """
    if actor.is_platform_admin:
        return _allow("platform_admin")
    return _deny("not_staff")


# ── loading the subject ──────────────────────────────────────────────

def _load_user(session: Session, channel: Channel) -> Subject | None:
    """No query, on purpose.

    The only readable `user:` channel is the actor's own and the actor's xid is
    already resolved, so the personal channel costs nothing — and, more usefully,
    it cannot be turned into a "does this account exist" probe by naming other
    people's xids, because the database is never asked about them.
    """
    return Subject(owner_user_xid=channel.subject)


def _load_attempt(session: Session, channel: Channel) -> Subject | None:
    row = session.execute(text("""
        SELECT user_id, org_context_id FROM attempts WHERE xid = CAST(:x AS uuid)
    """).bindparams(x=channel.subject)).mappings().first()
    if row is None:
        return None
    return Subject(member_user_ids=frozenset({row["user_id"]}),
                   org_id=row["org_context_id"], owner_user_id=row["user_id"])


def _load_pair(session: Session, channel: Channel) -> Subject | None:
    row = session.execute(text("""
        SELECT user_a_id, user_b_id FROM speaking_pairs WHERE xid = CAST(:x AS uuid)
    """).bindparams(x=channel.subject)).mappings().first()
    if row is None:
        return None
    return Subject(member_user_ids=frozenset({row["user_a_id"], row["user_b_id"]}))


def _load_slot(session: Session, channel: Channel) -> Subject | None:
    """Booked and not cancelled — the same set `slot_dto` counts as `booked_count`.

    Not "checked in": the slot channel is how a client learns the slot is
    matching, which is information a booked student needs BEFORE they check in.
    """
    row = session.execute(text("""
        SELECT s.org_id,
               coalesce(array_agg(b.user_id) FILTER (WHERE b.id IS NOT NULL),
                        '{}') AS members
        FROM speaking_slots s
        LEFT JOIN speaking_slot_bookings b
               ON b.slot_id = s.id AND b.cancelled_at IS NULL
        WHERE s.xid = CAST(:x AS uuid)
        GROUP BY s.id, s.org_id
    """).bindparams(x=channel.subject)).mappings().first()
    if row is None:
        return None
    return Subject(member_user_ids=frozenset(row["members"]), org_id=row["org_id"])


def _load_queue(session: Session, channel: Channel) -> Subject | None:
    """`speaking_queue_entries` has no `xid` column.

    `POST /speaking/queue` publishes `uuid.UUID(int=id)` as the entry's public
    handle, so that is what a client can name here and this reverses it. Ugly,
    and deliberately not fixed by adding a second identifier scheme: the honest
    fix is an `xid` column and a migration, which is a schema change this task
    is not.
    """
    entry_id = uuid.UUID(channel.subject).int
    if entry_id > _MAX_BIGINT:
        return None
    row = session.execute(text("""
        SELECT user_id, org_id FROM speaking_queue_entries WHERE id = :id
    """).bindparams(id=entry_id)).mappings().first()
    if row is None:
        return None
    return Subject(member_user_ids=frozenset({row["user_id"]}), org_id=row["org_id"])


def _load_competition(session: Session, channel: Channel) -> Subject | None:
    """Entrants, per §4.4, and deliberately not "anyone at the hosting centre".

    A withdrawn entry keeps its row and keeps its channel: someone who pulled out
    still watches the board, and hiding it would not un-tell them the questions.
    """
    row = session.execute(text("""
        SELECT c.org_id,
               coalesce(array_agg(e.user_id) FILTER (WHERE e.id IS NOT NULL),
                        '{}') AS members
        FROM competitions c
        LEFT JOIN competition_entries e ON e.competition_id = c.id
        WHERE c.xid = CAST(:x AS uuid)
        GROUP BY c.id, c.org_id
    """).bindparams(x=channel.subject)).mappings().first()
    if row is None:
        return None
    return Subject(member_user_ids=frozenset(row["members"]), org_id=row["org_id"])


def _load_assignment(session: Session, channel: Channel) -> Subject | None:
    row = session.execute(text("""
        SELECT org_id, assigned_by FROM assignments WHERE xid = CAST(:x AS uuid)
    """).bindparams(x=channel.subject)).mappings().first()
    if row is None:
        return None
    return Subject(org_id=row["org_id"], owner_user_id=row["assigned_by"])


# ── the matrix, as data ──────────────────────────────────────────────

#: Channel family -> what it carries and who may read it. The events column is
#: the contract's §4.4 table; nothing outside it is publishable.
FAMILIES: dict[str, Family] = {
    "user": Family(
        "user",
        # `slot.matched` and `queue.matched` are here as well as on their own
        # families because `RtQueueMatched.initiator` is per-PEER — "exactly one
        # peer is told to create the offer" — so the frame cannot be broadcast on
        # a channel more than one person reads without either telling a whole
        # slot who was paired with whom, or moving recipient filtering into the
        # bus, which is the second permission model this module exists to avoid.
        frozenset({"notification", "session.revoked", "slot.matched",
                   "queue.matched", "queue.expired"}),
        _load_user, _self_only),
    "queue": Family(
        "queue",
        frozenset({"queue.position", "queue.matched", "queue.expired"}),
        _load_queue, _party_to_it),
    "slot": Family(
        "slot",
        # Slot STATE only. The pairing itself goes to each peer's `user:` channel
        # for the reason above.
        frozenset({"slot.opened", "slot.matching", "slot.cancelled"}),
        _load_slot, _party_to_it),
    "pair": Family(
        "pair",
        frozenset({"pair.peer_joined", "pair.peer_left", "pair.ended",
                   "signal.offer", "signal.answer", "signal.ice", "signal.bye"}),
        _load_pair, _party_to_it),
    "competition": Family(
        "competition",
        frozenset({"competition.state", "competition.key_released",
                   "leaderboard.snapshot", "leaderboard.delta",
                   "competition.finalized"}),
        _load_competition, _party_to_it),
    "assignment": Family(
        "assignment", frozenset({"assignment.progress"}),
        _load_assignment, _centre_staff),
    "attempt": Family(
        "attempt", frozenset({"attempt.clock", "attempt.force_submit"}),
        _load_attempt, _party_to_it),
    "safety": Family(
        "safety", frozenset({"safety.report_filed", "safety.action_taken"}),
        None, _staff_only, has_subject=False),
}

#: Every event type any family carries. The gateway's own protocol frames
#: (`hello`, `error`, `resync`, `ping`) are not in here: they are channel-less by
#: construction and never pass through the bus.
EVENT_TYPES: frozenset[str] = frozenset(
    event for family in FAMILIES.values() for event in family.events)


def parse(name: str) -> Channel | None:
    """`family:subject`, or `None` if this is not a channel at all.

    The subject is validated as a UUID here rather than in each loader. A client
    controls this string completely, and `CAST(:x AS uuid)` on rubbish raises
    inside whatever transaction is open — which in this codebase means every
    later statement on that session fails too and the real cause is three errors
    back (`platform_ops.LexiconEntry` has the same note for the same reason).
    """
    if not isinstance(name, str) or not name or len(name) > 128:
        return None
    head, _, subject = name.partition(":")
    family = FAMILIES.get(head)
    if family is None:
        return None
    if not family.has_subject:
        return Channel(head) if not subject else None
    try:
        # `str(UUID(...))` normalises case and braces, so `USER:{ABC…}` and
        # `user:abc…` are one channel rather than two half-populated ones.
        return Channel(head, str(uuid.UUID(subject)))
    except (ValueError, AttributeError):
        return None


class UndeclaredEvent(ValueError):
    """An event type was published onto a family that does not carry it."""


def carries(family: str, event_type: str) -> bool:
    """May this event type travel on this family? Asked at every publish."""
    known = FAMILIES.get(family)
    return known is not None and event_type in known.events


def assert_carries(channel_name: str, event_type: str) -> None:
    """Refuse to publish an event onto a channel that does not declare it.

    This is an authorization guard wearing a routing hat, which is why it lives
    here rather than in the transport. The subscribe path decides WHO may read a
    channel; without this, that decision can still be defeated from the other
    end — `assignment.progress` is one teacher's view of forty students, and a
    publisher that addressed it to `user:{someone}` would deliver it to a reader
    the subscribe path correctly let in.

    Raises rather than returning a bool: a routing table with a typo in it must
    fail where the typo is, not deliver quietly to a channel nobody reads.
    """
    family = channel_name.partition(":")[0]
    if not carries(family, event_type):
        raise UndeclaredEvent(
            f"{event_type!r} is not declared on the {family!r} channel family")


def decide(actor: Actor, name: str, session: Session) -> tuple[Channel | None, Verdict]:
    """The one entry point. Parse, load the subject, apply the family's rule.

    `session` is a live, short-lived read transaction owned by the caller. It is
    passed in rather than opened here because the caller is an event loop and the
    session is blocking: `app/api/realtime.py` runs this on a worker thread and
    closes the transaction before the connection goes back to waiting, so a
    socket held open for an hour holds no database connection at all.
    """
    channel = parse(name)
    if channel is None:
        return None, Verdict(False, UNKNOWN_CHANNEL, "unparseable")

    family = FAMILIES[channel.family]
    if family.load is None:
        return channel, family.rule(actor, Subject())

    subject = family.load(session, channel)
    if subject is None:
        # Deliberately the same answer as "not yours". Distinguishing the two
        # would let a client enumerate which xids are real, one refusal at a
        # time; `unknown_channel` is reserved for a family that does not exist,
        # which leaks nothing because the families are published in the contract.
        return channel, _deny("no_such_subject")
    return channel, family.rule(actor, subject)


def describe(actor: Actor) -> dict[str, Any]:
    """The channels a principal always has, for `hello`.

    Only the ones needing no lookup: their own personal channel, and the
    moderation stream if they are staff. Everything else is subscribed to by
    name, because the server does not know which contest a student is watching.
    """
    implicit = [f"user:{actor.user_xid}"]
    if actor.is_platform_admin:
        implicit.append("safety")
    return {"implicit_channels": implicit}
