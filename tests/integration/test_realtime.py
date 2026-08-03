"""The WebSocket gateway, against a real socket, a real Redis and a real database.

The contract has described this channel since before there was code behind it,
which is why `POST /speaking/slots/{xid}/check-in` documents "Checked in; await
`slot.matched` on the realtime channel" — a message nothing in the process could
send. `test_a_slot_match_reaches_both_peers` is the end-to-end proof that it now
arrives: domain change, outbox row, relay, bus, socket.

Everything else here is about refusals, because that is what a subscribe
primitive is. The four the product cannot survive being wrong about are one test
class each, and every one of them was verified by sabotage — the guard broken on
purpose, the test watched to fail, the guard restored.

Two mechanical notes:

  * These tests use the REAL `deps.db`. The gateway opens its own session on a
    worker thread and cannot be reached by a dependency override, so the suite's
    session is committed and `DATABASE_URL` is repointed at the scratch database,
    the same way `test_transaction_boundary.py` does it.
  * `HEARTBEAT_SECONDS` is shortened to a second. That is not decoration: it is
    what bounds every `receive`. A gateway bug that delivered nothing would
    otherwise hang the suite forever instead of failing it, and a hanging CI job
    is worse than a red one.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect

from app.api import realtime as gateway
from app.api.deps import issue_access_token
from app.platform import realtime

ADULT_DOB = "1998-01-01"


# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch):
    """One second between pings, and a reaper that effectively never fires.

    The ping is the clock every `_until` in this file runs on: any receive that
    is waiting for a frame that will never come sees pings instead and gives up
    after a bounded number of them. The reaper is pushed out of the way because
    these tests are idle for seconds at a time by design; the one test that
    cares about it sets its own value.
    """
    monkeypatch.setattr(gateway, "HEARTBEAT_SECONDS", 1.0)
    monkeypatch.setattr(gateway, "MISSED_HEARTBEATS_BEFORE_CLOSE", 3600)


@pytest.fixture
def live(database_url, db):
    """A TestClient running the real `deps.db` against the scratch database.

    Callers commit their own rows before connecting: the gateway's session is a
    different one and sees nothing that has only been flushed.
    """
    from app.api.main import create_app
    from app.platform import db as platform_db
    from app.platform.config import settings

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    settings.cache_clear()
    platform_db.reset_engine()
    realtime.reset()
    try:
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            yield client
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        settings.cache_clear()
        platform_db.reset_engine()
        realtime.reset()
        db.rollback()


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


@contextlib.contextmanager
def socket(client, user_xid, *, ticket: str | None = None):
    """Open a connection and swallow the two frames every connection starts with."""
    token = ticket if ticket is not None else realtime.mint(str(user_xid)).token
    with client.websocket_connect(f"/realtime?ticket={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello", hello
        assert ws.receive_json()["type"] == "subscribed"
        yield ws


def _until(ws, *types, limit=6):
    """The next frame of one of `types`, ignoring heartbeats.

    `limit` is the bound that turns "the gateway never sent it" into a failure
    instead of a hang.
    """
    seen = []
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["type"] == "ping":
            continue
        seen.append(frame)
        if frame["type"] in types:
            return frame
    raise AssertionError(f"no {types} frame; saw {seen}")


def subscribe(ws, *names, since_seq=None):
    """Send a subscribe and collect everything up to the acknowledgement.

    The ack is emitted by the pump AFTER Redis has confirmed the subscription, so
    a test that returns from here may publish without racing it.
    """
    body = {"id": "s-1", "type": "subscribe", "channels": list(names)}
    if since_seq is not None:
        body["since_seq"] = since_seq
    ws.send_json(body)
    frames = []
    for _ in range(12):
        frame = ws.receive_json()
        if frame["type"] == "ping":
            continue
        frames.append(frame)
        if frame["type"] == "subscribed":
            return frames
    raise AssertionError(f"no acknowledgement; saw {frames}")


def refusal(frames, channel):
    for frame in frames:
        if frame["type"] == "error" and frame["channel"] == channel:
            return frame["data"]
    raise AssertionError(f"{channel} was not refused: {frames}")


def accepted(frames):
    return next(f["data"]["channels"] for f in frames if f["type"] == "subscribed")


def closes(ws):
    """Drive the socket until the server closes it, and return the close code."""
    with pytest.raises(WebSocketDisconnect) as caught:
        for _ in range(12):
            ws.receive_json()
    return caught.value.code


def _user(db, phone, name, *, org_id=None, role="student", dob=ADULT_DOB):
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES (:p, :n, CAST(:d AS date), 'active') RETURNING id, xid
    """).bindparams(p=phone, n=name, d=dob)).mappings().one()
    if org_id:
        db.execute(text("""
            INSERT INTO org_memberships (org_id, user_id, role, status)
            VALUES (:o, :u, :r, 'active')
        """).bindparams(o=org_id, u=row["id"], r=role))
    db.flush()
    return row


def _org(db, name):
    return db.execute(text("""
        INSERT INTO organizations (name, slug, status)
        VALUES (:n, :s, 'active') RETURNING id, xid
    """).bindparams(n=name, s=f"{name}-{uuid.uuid4().hex[:6]}")).mappings().one()


def _attempt(db, seed, user_id):
    return db.execute(text("""
        INSERT INTO attempts (user_id, test_version_id, mode, status)
        VALUES (:u, :v, 'exam', 'in_progress') RETURNING id, xid
    """).bindparams(u=user_id, v=seed["test_version"].id)).mappings().one()


# ── the handshake ────────────────────────────────────────────────────

class TestTheTicket:
    def test_the_endpoint_mints_a_ticket_the_gateway_accepts(self, live, db, seed):
        """End to end through HTTP, because the endpoint used to return a random
        string it stored nowhere — verifiable only by trying to redeem it."""
        db.commit()
        minted = live.post("/api/v1/realtime/ticket",
                           headers=auth(seed["student"].xid))
        assert minted.status_code == 200, minted.text
        body = minted.json()
        assert body["url"].startswith("wss://")
        with socket(live, seed["student"].xid, ticket=body["ticket"]) as ws:
            assert ws is not None

    def test_a_ticket_lives_for_thirty_seconds_and_not_longer(self, live, db, seed):
        """A bearer credential in a URL lands in proxy logs, browser history and
        `Referer`. The TTL is what makes a harvested one worthless."""
        db.commit()
        token = live.post("/api/v1/realtime/ticket",
                          headers=auth(seed["student"].xid)).json()["ticket"]
        ttl = realtime.client().ttl("rt:ticket:" + token)
        assert 0 < ttl <= realtime.TICKET_TTL_SECONDS == 30

    def test_a_ticket_is_burned_on_use(self, live, db, seed):
        db.commit()
        token = realtime.mint(str(seed["student"].xid)).token
        with socket(live, seed["student"].xid, ticket=token):
            pass
        with live.websocket_connect(f"/realtime?ticket={token}") as ws:
            assert closes(ws) == gateway.CLOSE_UNAUTHORIZED

    @pytest.mark.parametrize("token", ["", "not-a-ticket", "x" * 200])
    def test_a_ticket_nobody_minted_is_refused(self, live, db, seed, token):
        db.commit()
        with live.websocket_connect(f"/realtime?ticket={token}") as ws:
            assert closes(ws) == gateway.CLOSE_UNAUTHORIZED

    def test_a_colliding_token_never_rebinds_a_live_ticket(self, live, db, seed, monkeypatch):
        """`SET ... NX`. Without it a second mint would overwrite the first
        binding, and the user holding the earlier ticket would open a socket
        authenticated as somebody else."""
        import secrets

        db.commit()
        monkeypatch.setattr(secrets, "token_urlsafe", lambda _n: "fixed-token")
        realtime.mint(str(seed["student"].xid))
        with pytest.raises(realtime.BusUnavailable):
            realtime.mint(str(seed["author"].xid))

    def test_a_ban_landing_between_mint_and_connect_is_honoured(self, live, db, seed):
        """The ticket says who you are; the database says whether you may be
        here. Thirty seconds is long enough for a suspension to land inside it,
        which is exactly the window a moderator acts in."""
        db.commit()
        token = realtime.mint(str(seed["student"].xid)).token
        db.execute(text("UPDATE users SET status = 'suspended' WHERE id = :u")
                   .bindparams(u=seed["student"].id))
        db.commit()
        with live.websocket_connect(f"/realtime?ticket={token}") as ws:
            assert closes(ws) == gateway.CLOSE_UNAUTHORIZED


# ── authorization, which is the whole point ──────────────────────────

class TestAttemptChannels:
    def test_the_owner_may_read_their_own_attempt(self, live, db, seed):
        attempt = _attempt(db, seed, seed["student"].id)
        db.commit()
        with socket(live, seed["student"].xid) as ws:
            assert accepted(subscribe(ws, f"attempt:{attempt['xid']}")) == [
                f"attempt:{attempt['xid']}"]

    def test_another_student_may_not(self, live, db, seed):
        """A student's attempt channel carries their clock and their forced
        submit. Naming someone else's xid must refuse, and — the half a
        subscribe test usually forgets — must deliver nothing afterwards."""
        other = _user(db, "+998911000001", "Bekzod")
        attempt = _attempt(db, seed, seed["student"].id)
        db.commit()
        channel = f"attempt:{attempt['xid']}"

        with socket(live, other["xid"]) as ws:
            frames = subscribe(ws, channel)
            assert refusal(frames, channel)["code"] == "forbidden_channel"
            assert accepted(frames) == []

            realtime.publish(channel, "attempt.force_submit",
                             {"attempt_xid": str(attempt["xid"]), "reason": "expired"})
            with pytest.raises(AssertionError):
                _until(ws, "attempt.force_submit", limit=3)

    def test_an_attempt_that_does_not_exist_reads_as_forbidden(self, live, db, seed):
        """Not `unknown_channel`: telling a caller which xids are real is an
        enumeration oracle, one refusal at a time."""
        db.commit()
        channel = f"attempt:{uuid.uuid4()}"
        with socket(live, seed["student"].xid) as ws:
            assert refusal(subscribe(ws, channel), channel)["code"] == \
                "forbidden_channel"

    def test_a_real_attempt_and_an_imaginary_one_refuse_identically(self, live, db, seed):
        """`not_a_party` and `no_such_subject` are logged, never sent.

        On the wire the two are byte-identical, which is what stops a refusal
        being an oracle: told apart, they map which xids exist and who owns them.
        """
        other = _user(db, "+998911000002", "Kamola")
        attempt = _attempt(db, seed, seed["student"].id)
        imaginary = f"attempt:{uuid.uuid4()}"
        db.commit()
        with socket(live, other["xid"]) as ws:
            real = refusal(subscribe(ws, f"attempt:{attempt['xid']}"),
                           f"attempt:{attempt['xid']}")
            absent = refusal(subscribe(ws, imaginary), imaginary)
        assert real["code"] == absent["code"]
        assert real["message"] == absent["message"]
        assert "party" not in real["message"]


class TestCentreChannels:
    """A centre's events must not reach a competitor centre. Contractual."""

    @pytest.fixture
    def two_centres(self, db, seed):
        rival = _org(db, "rival")
        mine = _user(db, "+998912000001", "Dilnoza", org_id=seed["org"].id,
                     role="teacher")
        theirs = _user(db, "+998912000002", "Sardor", org_id=rival["id"],
                       role="teacher")
        pupil = _user(db, "+998912000003", "Aziza", org_id=seed["org"].id)
        assignment = db.execute(text("""
            INSERT INTO assignments (org_id, test_version_id, assigned_by,
                                     target_kind, opens_at, closes_at)
            VALUES (:o, :v, :by, 'cohort', now(), now() + interval '1 day')
            RETURNING id, xid
        """).bindparams(o=seed["org"].id, v=seed["test_version"].id,
                        by=mine["id"])).mappings().one()
        db.commit()
        return {"mine": mine, "theirs": theirs, "pupil": pupil,
                "assignment": assignment}

    def test_the_assigning_teacher_may_watch_their_own(self, live, two_centres):
        channel = f"assignment:{two_centres['assignment']['xid']}"
        with socket(live, two_centres["mine"]["xid"]) as ws:
            assert accepted(subscribe(ws, channel)) == [channel]

    def test_a_competitor_centres_teacher_may_not(self, live, two_centres):
        """The refusal the organization-private promise is made of, on the
        socket rather than on HTTP. It runs through `policy.check` — the same
        call the HTTP side makes — so it cannot drift away from it."""
        channel = f"assignment:{two_centres['assignment']['xid']}"
        with socket(live, two_centres["theirs"]["xid"]) as ws:
            frames = subscribe(ws, channel)
            assert refusal(frames, channel)["code"] == "forbidden_channel"
            assert accepted(frames) == []

            realtime.publish(channel, "assignment.progress", {"submitted": 12})
            with pytest.raises(AssertionError):
                _until(ws, "assignment.progress", limit=3)

    def test_a_student_at_the_right_centre_may_not_either(self, live, two_centres):
        """Membership is necessary and not sufficient. The roster includes the
        very students whose progress this channel reports."""
        channel = f"assignment:{two_centres['assignment']['xid']}"
        with socket(live, two_centres["pupil"]["xid"]) as ws:
            assert refusal(subscribe(ws, channel), channel)["code"] == \
                "forbidden_channel"


class TestCompetitionChannels:
    @pytest.fixture
    def contest(self, db, seed):
        row = db.execute(text("""
            INSERT INTO competitions (org_id, test_version_id, title, lobby_opens_at,
                                      starts_at, ends_at, duration_seconds, tiebreak,
                                      created_by)
            VALUES (:o, :v, 'March Cup', now(), now() + interval '1 hour',
                    now() + interval '2 hours', 3600, '[]'::jsonb, :by)
            RETURNING id, xid
        """).bindparams(o=seed["org"].id, v=seed["test_version"].id,
                        by=seed["author"].id)).mappings().one()
        db.execute(text("""
            INSERT INTO competition_entries (competition_id, user_id)
            VALUES (:c, :u)
        """).bindparams(c=row["id"], u=seed["student"].id))
        db.commit()
        return row

    def test_an_entrant_may_watch_the_board(self, live, contest, seed):
        channel = f"competition:{contest['xid']}"
        with socket(live, seed["student"].xid) as ws:
            assert accepted(subscribe(ws, channel)) == [channel]
            realtime.publish(channel, "leaderboard.delta",
                             {"my_rank": 4, "submitted_count": 12})
            assert _until(ws, "leaderboard.delta")["data"]["my_rank"] == 4

    def test_someone_who_never_registered_may_not(self, live, db, contest, seed):
        """Per-contest, not per-platform. The lobby payload and the key release
        travel here, and both are fairness-critical."""
        outsider = _user(db, "+998913000001", "Jasur", org_id=seed["org"].id)
        db.commit()
        channel = f"competition:{contest['xid']}"
        with socket(live, outsider["xid"]) as ws:
            assert refusal(subscribe(ws, channel), channel)["code"] == \
                "forbidden_channel"

    def test_a_state_change_from_the_scheduler_reaches_an_entrant(
            self, live, contest, seed):
        """`tick_competitions` publishes after its transaction commits. This is
        the frame two hundred people watch a countdown against."""
        from app.workers import actors

        channel = f"competition:{contest['xid']}"
        with socket(live, seed["student"].xid) as ws:
            subscribe(ws, channel)
            actors._publish(channel, "competition.state",
                            {"status": "lobby", "server_now": "now",
                             "seconds_to_start": 90})
            assert _until(ws, "competition.state")["data"]["status"] == "lobby"


class TestTheSafetyChannel:
    def test_staff_only(self, live, db, seed):
        db.execute(text("""
            INSERT INTO platform_role_grants (user_id, role, granted_by)
            VALUES (:u, 'platform_admin', :u)
        """).bindparams(u=seed["author"].id))
        db.commit()
        with socket(live, seed["author"].xid) as ws:
            # Implicit for staff: it is in `hello`, and already subscribed.
            assert accepted(subscribe(ws, "safety")) == ["safety"]
            realtime.publish("safety", "safety.report_filed", {"priority": "critical"})
            assert _until(ws, "safety.report_filed")["data"]["priority"] == "critical"

    def test_an_ordinary_student_is_refused(self, live, db, seed):
        db.commit()
        with socket(live, seed["student"].xid) as ws:
            frames = subscribe(ws, "safety")
            assert refusal(frames, "safety")["code"] == "forbidden_channel"
            realtime.publish("safety", "safety.report_filed", {"priority": "critical"})
            with pytest.raises(AssertionError):
                _until(ws, "safety.report_filed", limit=3)


class TestThePersonalChannel:
    def test_it_is_subscribed_before_the_client_asks(self, live, db, seed):
        """§4.4 calls it implicit and it has to be: `session.revoked` and
        `slot.matched` are the two frames a client would otherwise race."""
        db.commit()
        with socket(live, seed["student"].xid) as ws:
            realtime.publish(f"user:{seed['student'].xid}", "notification",
                             {"template": "regrade.band_changed"})
            assert _until(ws, "notification")["data"]["template"] == \
                "regrade.band_changed"

    def test_another_users_personal_channel_is_refused(self, live, db, seed):
        db.commit()
        channel = f"user:{seed['author'].xid}"
        with socket(live, seed["student"].xid) as ws:
            assert refusal(subscribe(ws, channel), channel)["code"] == \
                "forbidden_channel"


# ── the product's first pillar ───────────────────────────────────────

class TestSpeakingMatch:
    def test_a_slot_match_reaches_both_peers(self, live, db, seed):
        """The promise `check-in` has been making with nothing behind it.

        Domain change -> outbox row in the same transaction -> relay ->
        `actors.dispatch_all` -> the bus -> two sockets. Nothing here is
        simulated except the scheduler tick, which is one function call.
        """
        from app.modules.speaking import service
        from app.workers import actors, relay

        first = _user(db, "+998914000001", "Aziza", org_id=seed["org"].id)
        second = _user(db, "+998914000002", "Bekzod", org_id=seed["org"].id)
        slot = db.execute(text("""
            INSERT INTO speaking_slots (org_id, starts_at, age_band, status, created_by)
            VALUES (:o, now() - interval '1 minute', 'adult', 'booking', :by)
            RETURNING id, xid
        """).bindparams(o=seed["org"].id, by=seed["author"].id)).mappings().one()
        for who in (first, second):
            db.execute(text("""
                INSERT INTO speaking_slot_bookings (slot_id, user_id, checked_in_at)
                VALUES (:s, :u, now())
            """).bindparams(s=slot["id"], u=who["id"]))
        db.commit()

        with socket(live, first["xid"]) as one, socket(live, second["xid"]) as two:
            service.match_slot(db, slot["id"], dt.datetime.now(dt.UTC))
            db.commit()
            relay.drain(db, actors.dispatch_all)
            db.commit()

            frames = [_until(one, "slot.matched"), _until(two, "slot.matched")]

        assert {f["channel"] for f in frames} == {
            f"user:{first['xid']}", f"user:{second['xid']}"}
        assert {f["data"]["pair"]["xid"] for f in frames} == {
            f["data"]["pair"]["xid"] for f in frames}
        # "Exactly one peer is told to create the offer." Two offers is a call
        # that fails; zero is a call that never starts.
        assert sum(f["data"]["initiator"] for f in frames) == 1

    def test_a_third_student_on_the_same_slot_learns_nothing(self, live, db, seed):
        """The pairing goes to each peer's own channel precisely so that being
        booked on a slot is not the same as being told who was paired."""
        from app.workers import actors

        onlooker = _user(db, "+998914000003", "Kamola", org_id=seed["org"].id)
        first = _user(db, "+998914000004", "Nodira", org_id=seed["org"].id)
        db.commit()
        with socket(live, onlooker["xid"]) as ws:
            actors.broadcast("speaking.matched", {
                "pair_xid": str(uuid.uuid4()), "slot_xid": str(uuid.uuid4()),
                "origin": "slot_batch", "age_band": "adult",
                "user_xids": {"1": str(first["xid"])}, "initiator_user_id": 1,
            }, "agg")
            with pytest.raises(AssertionError):
                _until(ws, "slot.matched", limit=3)


class TestForcedSubmit:
    def test_an_auto_submitted_attempt_tells_its_owner(self, live, db, seed):
        """`RtAttemptForceSubmit` had no producer at all: `attempt.expired` was
        listed in the worker routing table with a comment saying it was emitted,
        and nothing emitted it."""
        from app.modules.exam.session import ExamSession
        from app.platform.clock import SystemClock
        from app.workers import actors, relay

        attempt = _attempt(db, seed, seed["student"].id)
        db.execute(text("UPDATE attempts SET expires_at = now() - interval '1 hour' "
                        "WHERE id = :a").bindparams(a=attempt["id"]))
        db.commit()

        with socket(live, seed["student"].xid) as ws:
            subscribe(ws, f"attempt:{attempt['xid']}")
            from app.modules.qtypes.registry import default_scorer

            exam = ExamSession(db, default_scorer(), SystemClock(), grace_seconds=30)
            exam.auto_submit_expired()
            db.commit()
            relay.drain(db, actors.dispatch_all)
            db.commit()

            frame = _until(ws, "attempt.force_submit")
        assert frame["data"] == {"attempt_xid": str(attempt["xid"]),
                                 "reason": "expired"}


class TestSessionRevoked:
    def test_a_ban_pushes_session_revoked_to_the_banned_user(self, live, db, seed):
        """"Pushed immediately because refresh tokens are stored and revocable —
        a stateless JWT could not deliver this." The refresh token dies with the
        ban; the access token has fifteen minutes left and the socket outlives
        both."""
        from app.workers import actors, relay

        db.execute(text("""
            INSERT INTO platform_role_grants (user_id, role, granted_by)
            VALUES (:u, 'platform_admin', :u)
        """).bindparams(u=seed["author"].id))
        db.commit()

        with socket(live, seed["student"].xid) as ws:
            response = live.post("/api/v1/admin/moderation-actions",
                                 headers=auth(seed["author"].xid),
                                 json={"action": "ban", "reason": "grooming",
                                       "target_user_xid": str(seed["student"].xid)})
            assert response.status_code == 201, response.text
            db.rollback()
            relay.drain(db, actors.dispatch_all)
            db.commit()
            frame = _until(ws, "session.revoked")
        assert frame["data"]["reason"] == "banned"


# ── signalling stays opaque ──────────────────────────────────────────

class TestSignalling:
    @pytest.fixture
    def pair(self, db, seed):
        one = _user(db, "+998915000001", "Aziza", org_id=seed["org"].id)
        two = _user(db, "+998915000002", "Bekzod", org_id=seed["org"].id)
        row = db.execute(text("""
            INSERT INTO speaking_pairs (origin, user_a_id, user_b_id, age_band)
            VALUES ('live_queue', :a, :b, 'adult') RETURNING id, xid
        """).bindparams(a=one["id"], b=two["id"])).mappings().one()
        db.commit()
        return {"one": one, "two": two, "row": row}

    def test_an_offer_is_relayed_byte_for_byte(self, live, pair):
        """The payload is deliberately NOT valid SDP.

        If anything on this path parsed it the frame would not survive the trip,
        and the server would have become a media component — which is the line
        that keeps audio off these servers entirely.
        """
        channel = f"pair:{pair['row']['xid']}"
        opaque = {"sdp": "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\n\u00ff not sdp at all\r\n",
                  "nested": [1, {"m": None}], "": ""}
        with socket(live, pair["one"]["xid"]) as a, socket(live, pair["two"]["xid"]) as b:
            subscribe(a, channel)
            subscribe(b, channel)
            a.send_json({"type": "signal", "channel": channel,
                         "data": {"kind": "offer", "payload": opaque}})
            frame = _until(b, "signal.offer")
        assert frame["data"]["payload"] == opaque
        assert frame["data"]["from"] == str(pair["one"]["xid"])

    def test_a_stranger_may_not_signal_into_a_call(self, live, db, pair, seed):
        channel = f"pair:{pair['row']['xid']}"
        with socket(live, seed["student"].xid) as ws:
            frames = subscribe(ws, channel)
            assert refusal(frames, channel)["code"] == "forbidden_channel"
            ws.send_json({"type": "signal", "channel": channel,
                          "data": {"kind": "offer", "payload": {}}})
            assert _until(ws, "error")["data"]["code"] == "forbidden_channel"

    def test_an_unknown_signal_kind_is_refused(self, live, pair):
        channel = f"pair:{pair['row']['xid']}"
        with socket(live, pair["one"]["xid"]) as ws:
            subscribe(ws, channel)
            ws.send_json({"type": "signal", "channel": channel,
                          "data": {"kind": "renegotiate", "payload": {}}})
            assert _until(ws, "error")["data"]["code"] == "bad_frame"

    def test_signalling_may_not_be_addressed_to_another_family(self, live, db, seed):
        """`signal.*` is declared on `pair:` and nowhere else, so a client that
        is legitimately subscribed to its own attempt still cannot use it as a
        relay."""
        attempt = _attempt(db, seed, seed["student"].id)
        db.commit()
        channel = f"attempt:{attempt['xid']}"
        with socket(live, seed["student"].xid) as ws:
            subscribe(ws, channel)
            ws.send_json({"type": "signal", "channel": channel,
                          "data": {"kind": "ice", "payload": {}}})
            assert _until(ws, "error")["data"]["code"] == "forbidden_channel"


# ── resume ───────────────────────────────────────────────────────────

class TestResume:
    def test_a_resume_inside_the_buffer_replays_only_what_was_missed(
            self, live, db, seed):
        db.commit()
        channel = f"user:{seed['student'].xid}"
        for index in range(4):
            realtime.publish(channel, "notification", {"n": index})

        with socket(live, seed["student"].xid) as ws:
            # Re-subscribing an already-held channel is what a reconnecting
            # client does: it names its channels and the seq it last saw.
            frames = subscribe(ws, channel, since_seq=2)
        replayed = [f["data"]["n"] for f in frames if f["type"] == "notification"]
        assert replayed == [2, 3]

    def test_a_resume_past_the_buffer_asks_the_client_to_refetch(
            self, live, db, seed, monkeypatch):
        """Explicit, so a client never silently runs on a partial view — a
        leaderboard that quietly stopped updating looks exactly like one that
        did not change."""
        monkeypatch.setattr(realtime, "REPLAY_FRAMES", 2)
        contest = db.execute(text("""
            INSERT INTO competitions (org_id, test_version_id, title, lobby_opens_at,
                                      starts_at, ends_at, duration_seconds, tiebreak,
                                      created_by)
            VALUES (:o, :v, 'Cup', now(), now(), now() + interval '1 hour', 3600,
                    '[]'::jsonb, :by)
            RETURNING id, xid
        """).bindparams(o=seed["org"].id, v=seed["test_version"].id,
                        by=seed["author"].id)).mappings().one()
        db.execute(text("INSERT INTO competition_entries (competition_id, user_id) "
                        "VALUES (:c, :u)")
                   .bindparams(c=contest["id"], u=seed["student"].id))
        db.commit()

        channel = f"competition:{contest['xid']}"
        for index in range(5):
            realtime.publish(channel, "leaderboard.delta", {"n": index})

        with socket(live, seed["student"].xid) as ws:
            frames = subscribe(ws, channel, since_seq=1)
        resync = next(f for f in frames if f["type"] == "resync")
        assert resync["channel"] == channel
        assert resync["data"]["refetch"] == \
            f"/api/v1/competitions/{contest['xid']}/leaderboard"
        assert not [f for f in frames if f["type"] == "leaderboard.delta"]

    def test_a_resume_on_a_silent_channel_replays_nothing_and_does_not_resync(
            self, live, db, seed):
        """The distinction the sequence counter exists for: an empty buffer means
        either "nothing happened" or "everything aged out", and answering
        `resync` to the first would send every idle client back to HTTP."""
        db.commit()
        channel = f"user:{seed['student'].xid}"
        realtime.publish(channel, "notification", {"n": 0})
        with socket(live, seed["student"].xid) as ws:
            frames = subscribe(ws, channel, since_seq=1)
        assert [f["type"] for f in frames] == ["subscribed"]


# ── lifecycle and backpressure ───────────────────────────────────────

class TestBackpressure:
    def test_the_send_queue_is_bounded(self):
        """A slow client must cost a bounded amount of memory. `offer` refuses
        rather than growing, which is what makes "drop the connection" possible
        at all — an `await put` would just move the unboundedness into Redis."""
        state = _idle_connection()
        assert all(state.offer({"n": index}) for index in range(gateway.SEND_QUEUE))
        assert not state.offer({"n": "one too many"})
        assert state.outbound.qsize() == gateway.SEND_QUEUE

    def test_a_client_that_cannot_keep_up_is_dropped(self):
        """The pump's half of the same rule: when the queue is full it stops and
        closes, rather than spinning while frames pile up in Redis."""
        import anyio

        state = _idle_connection()
        while state.offer({"filler": True}):
            pass

        async def drive():
            async with anyio.create_task_group() as tasks:
                await gateway._pump(_OneFrameFeed(), state, None, tasks.cancel_scope)

        anyio.run(drive)
        assert state.close_code == gateway.CLOSE_TOO_SLOW
        assert state.close_reason == "send_queue_full"

    def test_a_frame_for_a_dropped_channel_is_not_delivered(self):
        """The window `unsubscribe` opens, and the only thing that closes it.

        `_unsubscribe` removes the channel on the reader task and leaves the
        Redis UNSUBSCRIBE to the pump, so a frame can arrive after the client
        was told it had been dropped. The end-to-end unsubscribe test cannot see
        this: by the time it publishes, Redis has already stopped sending. So
        the pump is driven directly, with a feed that hands it a frame for a
        channel the connection does not hold.
        """
        import anyio

        state = _idle_connection()
        state.subscribed.clear()

        async def drive():
            async with anyio.create_task_group() as tasks:
                await gateway._pump(_OneFrameFeed(), state, None, tasks.cancel_scope)

        anyio.run(drive)
        assert state.outbound.qsize() == 0

    def test_the_bus_going_away_closes_the_connection(self):
        """Rather than holding a socket open that will never deliver anything.
        A silently empty connection is the one failure the protocol's explicit
        resync exists to prevent."""
        import anyio

        state = _idle_connection()

        async def drive():
            async with anyio.create_task_group() as tasks:
                await gateway._pump(_BrokenFeed(), state, None, tasks.cancel_scope)

        anyio.run(drive)
        assert state.close_code == gateway.CLOSE_BUS_DOWN

    def test_a_send_that_never_completes_drops_the_connection(self, live, db, seed,
                                                              monkeypatch):
        """A half-open TCP connection accepts writes into a kernel buffer and
        then stops. Without a deadline on the send, the writer task is pinned to
        that one phone for as long as the connection stays half-open."""
        monkeypatch.setattr(gateway, "SEND_TIMEOUT_SECONDS", 0.000_001)
        db.commit()
        token = realtime.mint(str(seed["student"].xid)).token
        with live.websocket_connect(f"/realtime?ticket={token}") as ws:
            assert closes(ws) == gateway.CLOSE_TOO_SLOW

    def test_the_inbound_budget_counts_and_resets(self):
        state = _idle_connection()
        assert all(state.within_budget(now=100.0)
                   for _ in range(gateway.INBOUND_FRAMES))
        assert not state.within_budget(now=100.0)
        assert state.within_budget(now=100.0 + gateway.INBOUND_WINDOW_SECONDS)

    def test_a_flood_closes_the_socket(self, live, db, seed):
        db.commit()
        with socket(live, seed["student"].xid) as ws:
            for _ in range(gateway.INBOUND_FRAMES + 2):
                ws.send_json({"type": "pong"})
            assert closes(ws) == gateway.CLOSE_POLICY

    def test_an_oversized_frame_is_refused_without_closing(self, live, db, seed):
        db.commit()
        with socket(live, seed["student"].xid) as ws:
            ws.send_json({"type": "pong", "pad": "x" * (gateway.MAX_FRAME_BYTES + 10)})
            assert _until(ws, "error")["data"]["code"] == "bad_frame"


class TestHeartbeat:
    def test_the_server_pings(self, live, db, seed):
        db.commit()
        with socket(live, seed["student"].xid) as ws:
            assert ws.receive_json()["type"] == "ping"

    def test_a_silent_client_is_reaped(self, live, db, seed, monkeypatch):
        """A half-open TCP connection is indistinguishable from a quiet one at
        the socket layer, and would otherwise hold this connection's memory
        until the kernel gave up — hours."""
        monkeypatch.setattr(gateway, "HEARTBEAT_SECONDS", 0.2)
        monkeypatch.setattr(gateway, "MISSED_HEARTBEATS_BEFORE_CLOSE", 1)
        db.commit()
        with socket(live, seed["student"].xid) as ws:
            assert closes(ws) == gateway.CLOSE_POLICY


class TestProtocolFrames:
    def test_a_frame_that_is_not_json_is_refused(self, live, db, seed):
        db.commit()
        with socket(live, seed["student"].xid) as ws:
            ws.send_text("{not json")
            assert _until(ws, "error")["data"]["code"] == "bad_frame"

    @pytest.mark.parametrize("body", [
        [1, 2, 3],
        {"type": "nonsense"},
        {"type": "subscribe", "channels": "user:x"},
        {"type": "subscribe", "channels": [17]},
        {"type": "unsubscribe", "channels": "nope"},
        {"type": "signal", "channel": 17},
    ])
    def test_malformed_frames_are_refused_without_closing(self, live, db, seed, body):
        db.commit()
        with socket(live, seed["student"].xid) as ws:
            ws.send_json(body)
            assert _until(ws, "error")["type"] == "error"

    def test_unsubscribing_stops_delivery(self, live, db, seed):
        db.commit()
        channel = f"user:{seed['student'].xid}"
        with socket(live, seed["student"].xid) as ws:
            ws.send_json({"id": "u-1", "type": "unsubscribe", "channels": [channel]})
            assert _until(ws, "unsubscribed")["data"]["channels"] == [channel]
            realtime.publish(channel, "notification", {"template": "x"})
            with pytest.raises(AssertionError):
                _until(ws, "notification", limit=3)

    def test_a_connection_may_not_hold_unbounded_channels(self, live, db, seed):
        """Refused before a single lookup, and the whole batch — otherwise a
        socket walks the platform's competitions twenty at a time and turns a
        refusal into a slow enumeration."""
        db.commit()
        with socket(live, seed["student"].xid) as ws:
            names = [f"attempt:{uuid.uuid4()}" for _ in range(gateway.MAX_CHANNELS + 1)]
            ws.send_json({"id": "s-1", "type": "subscribe", "channels": names})
            error = _until(ws, "error")
        assert error["data"]["code"] == "forbidden_channel"
        assert str(gateway.MAX_CHANNELS) in error["data"]["message"]


class TestTheContract:
    def test_the_gateway_is_not_an_http_path_in_the_document(self):
        """`scripts/check_api_coverage.py` fails on any path the contract does
        not declare. This asserts the property rather than trusting a framework
        internal — FastAPI's generator walks `APIRoute` and skips WebSocket
        routes, and that is not something to rediscover in CI."""
        from app.api.main import create_app

        generated = create_app().openapi()
        assert "/realtime" not in generated["paths"]
        assert not [p for p in generated["paths"] if p.endswith("/realtime")]

    def test_every_realtime_route_addresses_a_channel_that_carries_its_event(self):
        """The routing table, checked structurally.

        A typo in a channel name is a frame nobody receives and no error
        anywhere; a wrong FAMILY is a frame delivered to readers the subscribe
        path admitted for something else.
        """
        from app.modules.authz import channels
        from app.workers import actors

        sample = {"pair_xid": str(uuid.uuid4()), "slot_xid": str(uuid.uuid4()),
                  "user_xids": {"1": str(uuid.uuid4())}, "initiator_user_id": 1,
                  "user_xid": str(uuid.uuid4()), "reason": "expired"}
        assert actors.REALTIME
        for build in actors.REALTIME.values():
            for channel, rt_type, _data in build(sample, str(uuid.uuid4())):
                channels.assert_carries(channel, rt_type)

    def test_a_frame_published_by_another_process_arrives(self, live, db, seed):
        """The reason there is a bus at all.

        `docker-compose.yml` runs four gunicorn workers, and the outbox is
        drained by a fifth process entirely. A `slot.matched` produced by the
        scheduler has to reach a socket held by an API worker, which is a message
        broker whether or not it is called one. Publishing from a genuinely
        separate interpreter is the only way to assert that; a same-process
        publish would pass against an in-memory dictionary.
        """
        import subprocess
        import sys

        db.commit()
        channel = f"user:{seed['student'].xid}"
        with socket(live, seed["student"].xid) as ws:
            result = subprocess.run(
                [sys.executable, "-c",
                 "from app.platform import realtime;"
                 f"realtime.publish({channel!r}, 'notification', {{'from': 'worker-b'}})"],
                cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                env={**os.environ}, capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr
            assert _until(ws, "notification")["data"] == {"from": "worker-b"}

    def test_the_declared_events_cover_the_contracts_channel_table(self):
        """§4.4 lists what each channel carries. Drift between that table and
        `FAMILIES` is drift between the document a client is written from and
        the server it talks to."""
        from app.modules.authz import channels

        assert set(channels.FAMILIES) == {
            "user", "queue", "slot", "pair", "competition", "assignment",
            "attempt", "safety"}
        assert "session.revoked" in channels.FAMILIES["user"].events
        assert "leaderboard.delta" in channels.FAMILIES["competition"].events
        assert "attempt.force_submit" in channels.FAMILIES["attempt"].events


class TestSubjectLookup:
    """The other half of every rule: what the database says the channel names.

    A rule tested only against a `Subject` a test wrote is a rule tested against
    the test's opinion of the schema. These go through the real loaders.
    """

    def test_a_booked_student_may_read_their_slot(self, live, db, seed):
        slot = db.execute(text("""
            INSERT INTO speaking_slots (org_id, starts_at, age_band, status, created_by)
            VALUES (:o, now() + interval '1 hour', 'adult', 'booking', :by)
            RETURNING id, xid
        """).bindparams(o=seed["org"].id, by=seed["author"].id)).mappings().one()
        db.execute(text("""
            INSERT INTO speaking_slot_bookings (slot_id, user_id) VALUES (:s, :u)
        """).bindparams(s=slot["id"], u=seed["student"].id))
        db.commit()
        channel = f"slot:{slot['xid']}"
        with socket(live, seed["student"].xid) as ws:
            assert accepted(subscribe(ws, channel)) == [channel]

    def test_a_cancelled_booking_stops_being_a_membership(self, live, db, seed):
        """Booked-and-not-cancelled is the same set the slot reports as
        `booked_count`. Someone who pulled out is not in the room."""
        slot = db.execute(text("""
            INSERT INTO speaking_slots (org_id, starts_at, age_band, status, created_by)
            VALUES (:o, now() + interval '1 hour', 'adult', 'booking', :by)
            RETURNING id, xid
        """).bindparams(o=seed["org"].id, by=seed["author"].id)).mappings().one()
        db.execute(text("""
            INSERT INTO speaking_slot_bookings (slot_id, user_id, cancelled_at)
            VALUES (:s, :u, now())
        """).bindparams(s=slot["id"], u=seed["student"].id))
        db.commit()
        channel = f"slot:{slot['xid']}"
        with socket(live, seed["student"].xid) as ws:
            assert refusal(subscribe(ws, channel), channel)["code"] == \
                "forbidden_channel"

    def test_a_queue_entry_belongs_to_the_person_who_joined(self, live, db, seed):
        """`speaking_queue_entries` has no xid column, so `POST /speaking/queue`
        publishes `uuid.UUID(int=id)` and the loader reverses it. Asserted here
        against the id the endpoint actually returns, not against the trick."""
        db.commit()
        joined = live.post("/api/v1/speaking/queue",
                           headers=auth(seed["student"].xid), json={})
        assert joined.status_code == 201, joined.text
        channel = f"queue:{joined.json()['xid']}"
        with socket(live, seed["student"].xid) as ws:
            assert accepted(subscribe(ws, channel)) == [channel]

    def test_someone_elses_queue_entry_is_refused(self, live, db, seed):
        other = _user(db, "+998916000001", "Sardor")
        db.commit()
        joined = live.post("/api/v1/speaking/queue",
                           headers=auth(seed["student"].xid), json={})
        channel = f"queue:{joined.json()['xid']}"
        with socket(live, other["xid"]) as ws:
            assert refusal(subscribe(ws, channel), channel)["code"] == \
                "forbidden_channel"

    def test_a_queue_id_too_large_for_the_column_is_refused_not_500(self, live, db, seed):
        """`uuid.UUID(x).int` can be 2^128. Bound before it reaches a bigint
        comparison, because an error inside the transaction aborts every later
        statement on that session."""
        db.commit()
        channel = f"queue:{uuid.UUID(int=2**127)}"
        with socket(live, seed["student"].xid) as ws:
            assert refusal(subscribe(ws, channel), channel)["code"] == \
                "forbidden_channel"

    @pytest.mark.parametrize("family", ["pair", "competition", "assignment",
                                        "slot", "queue"])
    def test_a_subject_that_does_not_exist_is_refused(self, live, db, seed, family):
        db.commit()
        channel = f"{family}:{uuid.uuid4()}"
        with socket(live, seed["student"].xid) as ws:
            assert refusal(subscribe(ws, channel), channel)["code"] == \
                "forbidden_channel"

    def test_a_queue_entry_that_never_existed_is_refused(self, live, db, seed):
        """Distinct from the oversized case above: a random UUID's integer is
        past `bigint` and never reaches the query, so the row-not-found branch
        needs an id small enough to be looked up and absent."""
        db.commit()
        channel = f"queue:{uuid.UUID(int=999_999)}"
        with socket(live, seed["student"].xid) as ws:
            assert refusal(subscribe(ws, channel), channel)["code"] == \
                "forbidden_channel"


class TestTheBusBeingDown:
    """The failure modes, chosen rather than discovered.

    `platform/ratelimit.py` fails OPEN and says why. These fail differently and
    for stated reasons, so each one is pinned by a test: a decision nothing
    exercises is a comment.
    """

    @pytest.fixture
    def no_redis(self, monkeypatch):
        from app.platform.config import settings

        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6/0")
        settings.cache_clear()
        realtime.reset()
        yield
        settings.cache_clear()
        realtime.reset()

    def test_minting_a_ticket_refuses_rather_than_issuing_an_unverifiable_one(
            self, live, db, seed, no_redis):
        """503, not a ticket. A ticket that cannot be stored cannot be verified,
        and the only alternative is a gateway that admits one it cannot check."""
        db.commit()
        response = live.post("/api/v1/realtime/ticket",
                             headers=auth(seed["student"].xid))
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "realtime_unavailable"

    def test_a_publish_that_cannot_reach_redis_is_loud(self, no_redis):
        with pytest.raises(realtime.BusUnavailable):
            realtime.publish("user:x", "notification", {})

    def test_but_it_never_stalls_the_outbox(self, no_redis):
        """The relay treats a failed dispatch as "retry this row", so a realtime
        publish that raised would hold up regrades, analytics and every
        notification for as long as Redis was unreachable. `broadcast` swallows
        and logs; the frame is lost and every one of them has an HTTP mirror."""
        from app.workers import actors

        assert actors.broadcast("attempt.expired", {"reason": "expired"},
                                str(uuid.uuid4())) == 0

    def test_a_routing_typo_is_not_swallowed_with_it(self):
        """The other side of the same function. An unreachable Redis is an
        operational failure to log; an event addressed to a family that does not
        carry it is a bug, and swallowing it would deliver an invigilation view
        to whatever channel the typo named."""
        from app.modules.authz import channels
        from app.workers import actors

        with pytest.raises(channels.UndeclaredEvent):
            actors._publish("user:x", "assignment.progress", {})


class TestReplayBuffer:
    def test_an_aged_out_buffer_resyncs_rather_than_replaying_nothing(
            self, live, db, seed):
        """The distinction the sequence counter exists for. With the log expired
        and the counter still standing, "nothing happened" and "everything aged
        out" are the same empty list — and answering the second with silence is
        a client that resumes on a hole and never knows."""
        db.commit()
        channel = f"user:{seed['student'].xid}"
        realtime.publish(channel, "notification", {"n": 0})
        realtime.publish(channel, "notification", {"n": 1})
        realtime.client().delete("rt:log:" + channel)

        with socket(live, seed["student"].xid) as ws:
            frames = subscribe(ws, channel, since_seq=1)
        assert [f["type"] for f in frames] == ["resync", "subscribed"]


# ── stubs for the two tests that need a broken bus ───────────────────

def _idle_connection():
    import asyncio

    from app.api.deps import Principal

    state = gateway._Connection(actor=Principal(user_id=1, user_xid="x"),
                                outbound=asyncio.Queue(maxsize=gateway.SEND_QUEUE),
                                commands=asyncio.Queue(), now=100.0)
    state.subscribed.add("user:x")
    return state


class _OneFrameFeed:
    """One frame, then a dead bus — so the pump's loop terminates."""

    def __init__(self, channel="user:x"):
        self._channel = channel
        self._sent = False

    async def next_frame(self, *, timeout):
        if self._sent:
            raise ConnectionError("done")
        self._sent = True
        return {"channel": self._channel, "type": "notification", "data": {}}


class _BrokenFeed:
    async def next_frame(self, *, timeout):
        raise ConnectionError("redis went away")
