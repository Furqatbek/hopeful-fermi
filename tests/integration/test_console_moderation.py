"""The safety queue as the console works it, and the socket it holds open.

Three calls, in the order the screen makes them: read `GET /admin/reports` for
both queues, mint a ticket with `POST /realtime/ticket` and connect, and take an
action with `POST /admin/moderation-actions`.

Four things this file pins down, because the console is built around each of
them and none is visible from the OpenAPI document:

  * **`queue=minors` is a separate list, not a filter.** It is `involves_minor`
    and not dismissed, backed by its own partial index, and the general list is
    not the remainder — it carries the same rows.
  * **The queue's ORDER is wrong, and the console re-sorts.** `priority` is a
    `text` column and the handler asks for `ORDER BY priority DESC`, which
    PostgreSQL answers alphabetically: normal, high, critical. The most urgent
    report on the platform is the last row of the page.
  * **A ban with a target nobody matches answers 201 and revokes nothing.**
    Which is why the console refuses to send one.
  * **Nothing publishes on the `safety` channel.** The screen therefore polls,
    and the socket's job is to say when the view has stopped being live rather
    than to deliver the queue.

The WebSocket half uses the same harness as `test_realtime.py`: the real
`deps.db` against the scratch database, because the gateway opens its own
session on a worker thread and cannot be reached by a dependency override.
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

ADULT = dt.date(1990, 1, 1)
CHILD = dt.date(2012, 5, 1)


# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch):
    """A one-second ping, and a reaper that never fires.

    The ping is what bounds every `receive` below: a gateway bug that delivered
    nothing would otherwise hang the suite rather than fail it, and a hanging CI
    job is worse than a red one.
    """
    monkeypatch.setattr(gateway, "HEARTBEAT_SECONDS", 1.0)
    monkeypatch.setattr(gateway, "MISSED_HEARTBEATS_BEFORE_CLOSE", 3600)


@pytest.fixture
def client(engine, db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def live(database_url, db):
    """A TestClient on the real `deps.db`. Callers commit before connecting."""
    from app.api.main import create_app
    from app.platform import db as platform_db
    from app.platform.config import settings

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    settings.cache_clear()
    platform_db.reset_engine()
    realtime.reset()
    try:
        with TestClient(create_app(), raise_server_exceptions=False) as c:
            yield c
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


def _user(db, name, *, dob=ADULT, org_id=None, role="student"):
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES (:p, :n, CAST(:d AS date), 'active') RETURNING id, xid
    """).bindparams(p=f"+9989{uuid.uuid4().int % 10**8:08d}", n=name,
                    d=dob.isoformat())).mappings().one()
    if org_id:
        db.execute(text("""
            INSERT INTO org_memberships (org_id, user_id, role, status)
            VALUES (:o, :u, :r, 'active')
        """).bindparams(o=org_id, u=row["id"], r=role))
    db.flush()
    return row


@pytest.fixture
def admin(db, seed):
    """A platform admin, which is the only principal that may open this screen."""
    row = _user(db, "Rustam")
    db.execute(text("""
        INSERT INTO platform_role_grants (user_id, role, granted_by)
        VALUES (:u, 'platform_admin', :u)
    """).bindparams(u=row["id"]))
    db.flush()
    return row


def _report(db, *, category="harassment", minor=False, priority="normal",
            status="new", reporter=None, subject=None, evidence=None):
    """A row as `POST /speaking/pairs/{xid}/report` writes one.

    Written directly rather than through the reporting endpoint because that one
    needs a matched speaking pair and an object store, and none of what this file
    asserts is about how a report gets filed.
    """
    return db.execute(text("""
        INSERT INTO safety_reports (reporter_user_id, subject_kind, subject_user_id,
                                    category, description, involves_minor, priority,
                                    status, evidence_media_id)
        VALUES (:r, 'speaking_pair', :s, :c, 'Said something they should not have.',
                :m, :p, :st, :ev)
        RETURNING xid
    """).bindparams(r=reporter, s=subject, c=category, m=minor, p=priority,
                    st=status, ev=evidence)).scalar()


def _sessions(db, user_id, count):
    """Live refresh sessions, which is what a suspend or ban has to kill."""
    for index in range(count):
        db.execute(text("""
            INSERT INTO auth_sessions (user_id, token_hash, expires_at)
            VALUES (:u, :h, now() + interval '30 days')
        """).bindparams(u=user_id, h=f"hash-{user_id}-{index}-{uuid.uuid4().hex[:8]}"))
    db.flush()


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


# ── the queue read ───────────────────────────────────────────────────

class TestTheTwoQueues:
    def test_minors_is_its_own_list_and_general_is_not_the_remainder(
            self, client, db, seed, admin):
        """The console renders the minors block first and unconditionally, and
        labels the second list "all reports" — this is why."""
        child = _user(db, "Aziza", dob=CHILD)
        adult = _user(db, "Bek")
        about_child = _report(db, minor=True, priority="critical",
                              category="grooming", reporter=seed["author"].id,
                              subject=child["id"])
        about_adult = _report(db, reporter=seed["author"].id, subject=adult["id"])
        db.flush()

        minors = _ok(client.get("/api/v1/admin/reports?queue=minors",
                                headers=auth(admin["xid"])))
        assert [r["xid"] for r in minors["items"]] == [str(about_child)]

        general = _ok(client.get("/api/v1/admin/reports?queue=general",
                                 headers=auth(admin["xid"])))
        # Both. The general list is NOT "the ones that are not about a child",
        # so a screen that labelled it that way would be lying.
        assert {r["xid"] for r in general["items"]} == {str(about_child),
                                                       str(about_adult)}

    def test_a_dismissed_report_leaves_the_minors_queue_and_stays_in_general(
            self, client, db, seed, admin):
        """The partial index is `involves_minor AND status <> 'dismissed'`, so
        dismissal is what empties this queue.

        Set here with SQL, because **nothing in the API writes
        `safety_reports.status`.** The column is read by this filter and by the
        handler's status filter and written by no endpoint, so a report cannot
        be dismissed, assigned or resolved from the console at all.
        """
        child = _user(db, "Aziza", dob=CHILD)
        report = _report(db, minor=True, priority="critical", subject=child["id"],
                         reporter=seed["author"].id)
        db.flush()
        assert len(_ok(client.get("/api/v1/admin/reports?queue=minors",
                                  headers=auth(admin["xid"])))["items"]) == 1

        db.execute(text("UPDATE safety_reports SET status = 'dismissed' WHERE xid = :x")
                   .bindparams(x=report))
        db.flush()

        assert _ok(client.get("/api/v1/admin/reports?queue=minors",
                              headers=auth(admin["xid"])))["items"] == []
        assert len(_ok(client.get("/api/v1/admin/reports?queue=general",
                                  headers=auth(admin["xid"])))["items"]) == 1

    def test_an_unknown_queue_name_falls_through_to_the_general_list(
            self, client, db, seed, admin):
        """`queue` is `str`, not the contract's enum, and only `"minors"` is
        tested for — so a typo silently reads the general queue rather than
        being refused. The console sends the two literals and nothing else."""
        child = _user(db, "Aziza", dob=CHILD)
        _report(db, minor=True, subject=child["id"], reporter=seed["author"].id)
        _report(db, reporter=seed["author"].id)
        db.flush()

        typo = _ok(client.get("/api/v1/admin/reports?queue=minor",
                              headers=auth(admin["xid"])))
        assert len(typo["items"]) == 2


class TestTheOrderTheQueueArrivesIn:
    def test_the_most_urgent_report_is_the_last_row(self, client, db, seed, admin):
        """**A defect, pinned here so the console's re-sort is justified.**

        `moderation_queue` asks for `ORDER BY priority DESC` and `priority` is a
        `text` column with a CHECK constraint, not an ordered type. PostgreSQL
        sorts it alphabetically, so descending is normal, high, critical — and
        `critical` is what a report involving a child together with grooming or
        sexual content is set to.

        `web/src/features/moderation/queue.ts` reorders the page before drawing
        it. When the backend is fixed this test should fail, and both this and
        that helper should change together.
        """
        for priority in ("critical", "high", "normal"):
            _report(db, priority=priority, reporter=seed["author"].id)
        db.flush()

        page = _ok(client.get("/api/v1/admin/reports?queue=general",
                              headers=auth(admin["xid"])))
        assert [r["priority"] for r in page["items"]] == ["normal", "high", "critical"]


class TestWhatARowCarries:
    def test_it_does_not_name_the_person_reported(self, client, db, seed, admin):
        """Which is why the action form asks for a user id to be pasted.

        `safety_reports.subject_user_id` is set on every report filed from a
        call, and the DTO drops it. Nothing else in the API exposes it either —
        there is no `GET /admin/reports/{xid}` — so a moderator reading this
        queue cannot learn who to act on from it.
        """
        subject = _user(db, "Bek")
        _report(db, reporter=seed["author"].id, subject=subject["id"])
        db.flush()

        row = _ok(client.get("/api/v1/admin/reports?queue=general",
                             headers=auth(admin["xid"])))["items"][0]
        assert set(row) == {"xid", "category", "status", "priority",
                            "involves_minor", "has_evidence", "created_at"}
        assert "subject" not in row and "description" not in row

    def test_evidence_is_reported_as_absent_even_when_it_is_there(
            self, client, db, seed, admin):
        """**A defect.** `has_evidence` is the literal `False` in this handler.

        A report filed from a call carries the reporter's rolling 60-second
        buffer in `evidence_media_id` — the one case where conversation audio
        reaches these servers at all, and it exists so an admin can act without
        recording minors' conversations wholesale. The queue tells the admin
        there is none. The console renders no evidence column rather than a
        column that is always "no".
        """
        media = db.execute(text("""
            INSERT INTO media_assets (owner_user_id, kind, bucket, storage_key,
                                      content_type, bytes, checksum_sha256, status)
            VALUES (:u, 'audio', 'test-media', 'safety/buffer.webm', 'audio/webm',
                    2048, 'sum', 'quarantined')
            RETURNING id
        """).bindparams(u=seed["author"].id)).scalar()
        _report(db, reporter=seed["author"].id, evidence=media)
        db.flush()

        row = _ok(client.get("/api/v1/admin/reports?queue=general",
                             headers=auth(admin["xid"])))["items"][0]
        assert row["has_evidence"] is False
        stored = db.execute(text(
            "SELECT evidence_media_id FROM safety_reports")).scalar()
        assert stored == media, "the row has evidence; the DTO says it does not"


class TestWhoMayReadIt:
    def test_a_centre_admin_may_not(self, client, db, seed, admin):
        """The contractual line this queue sits on. A centre's own admin is
        staff, and still never reads a report about one of their students."""
        centre_admin = _user(db, "Nodir", org_id=seed["org"].id, role="centre_admin")
        db.flush()
        refused = client.get("/api/v1/admin/reports?queue=minors",
                             headers=auth(centre_admin["xid"]))
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "admin_only"

    def test_a_teacher_may_not(self, client, db, seed, admin):
        refused = client.get("/api/v1/admin/reports?queue=general",
                             headers=auth(seed["author"].xid))
        assert refused.status_code == 403

    def test_a_student_may_not(self, client, db, seed, admin):
        refused = client.get("/api/v1/admin/reports?queue=minors",
                             headers=auth(seed["student"].xid))
        assert refused.status_code == 403


# ── taking an action ─────────────────────────────────────────────────

class TestTakingAnAction:
    def test_a_ban_ends_every_live_session(self, client, db, seed, admin):
        """The reason the confirmation on this screen is not ceremony: it lands
        while the person is in a call, on every device at once."""
        target = _user(db, "Bek")
        _sessions(db, target["id"], 3)
        report = _report(db, reporter=seed["author"].id, subject=target["id"])
        db.flush()

        outcome = _ok(client.post("/api/v1/admin/moderation-actions",
                                  headers=auth(admin["xid"]),
                                  json={"action": "ban",
                                        "reason": "Sexual messages to a 15-year-old.",
                                        "target_user_xid": str(target["xid"]),
                                        "report_xid": str(report)}), 201)
        assert outcome["sessions_revoked"] == 3
        assert db.execute(text("""
            SELECT count(*) FROM auth_sessions
            WHERE user_id = :u AND revoked_at IS NULL
        """).bindparams(u=target["id"])).scalar() == 0
        assert db.execute(text("SELECT status FROM users WHERE id = :u")
                          .bindparams(u=target["id"])).scalar() == "suspended"

    def test_the_ban_queues_the_frame_that_closes_an_open_socket(
            self, client, db, seed, admin):
        """The refresh token is dead immediately; the ACCESS token lives another
        fifteen minutes and an open socket outlives both. `session.revoked`
        rides the outbox in the same transaction, so a ban that rolls back does
        not push one at somebody who was not banned."""
        target = _user(db, "Bek")
        _sessions(db, target["id"], 1)
        db.flush()

        _ok(client.post("/api/v1/admin/moderation-actions",
                        headers=auth(admin["xid"]),
                        json={"action": "ban", "reason": "Grooming.",
                              "target_user_xid": str(target["xid"])}), 201)
        row = db.execute(text("""
            SELECT event_type, payload FROM outbox WHERE aggregate_id = :a
        """).bindparams(a=str(target["xid"]))).mappings().one()
        assert row["event_type"] == "identity.session_revoked"
        assert row["payload"]["reason"] == "banned"

    def test_the_action_is_attached_to_the_report_that_prompted_it(
            self, client, db, seed, admin):
        """`report_xid` is optional on the endpoint and the console always sends
        it: an entry in an immutable log with no answer to "what was this about"
        is the one question the log exists for."""
        target = _user(db, "Bek")
        report = _report(db, reporter=seed["author"].id, subject=target["id"])
        db.flush()

        _ok(client.post("/api/v1/admin/moderation-actions",
                        headers=auth(admin["xid"]),
                        json={"action": "warn", "reason": "First offence.",
                              "target_user_xid": str(target["xid"]),
                              "report_xid": str(report)}), 201)
        linked = db.execute(text("""
            SELECT r.xid FROM moderation_actions a
            JOIN safety_reports r ON r.id = a.report_id
        """)).scalar()
        assert str(linked) == str(report)

    def test_an_empty_reason_is_refused(self, client, db, seed, admin):
        """`min_length=1`, for the same reason the row cannot be edited:
        somebody reads it months later, possibly a regulator, and "" is not a
        reason. The form refuses it before the request as well."""
        target = _user(db, "Bek")
        db.flush()
        refused = client.post("/api/v1/admin/moderation-actions",
                              headers=auth(admin["xid"]),
                              json={"action": "warn", "reason": "",
                                    "target_user_xid": str(target["xid"])})
        assert refused.status_code == 422, refused.text

    def test_an_unknown_action_is_refused(self, client, db, seed, admin):
        """A typo'd `"bann"` used to match neither branch: no sessions revoked,
        no account suspended, and an audit row saying the user was dealt with.
        The console offers only the seven, and the server checks anyway."""
        target = _user(db, "Bek")
        _sessions(db, target["id"], 2)
        db.flush()
        refused = client.post("/api/v1/admin/moderation-actions",
                              headers=auth(admin["xid"]),
                              json={"action": "bann", "reason": "Grooming.",
                                    "target_user_xid": str(target["xid"])})
        assert refused.status_code == 422
        assert db.execute(text("""
            SELECT count(*) FROM auth_sessions
            WHERE user_id = :u AND revoked_at IS NULL
        """).bindparams(u=target["id"])).scalar() == 2

    def test_a_content_action_must_name_its_content(self, client, db, seed, admin):
        """`content_remove` recorded that content was removed and not WHICH, in
        the log that answers a rights holder's lawyers."""
        refused = client.post("/api/v1/admin/moderation-actions",
                              headers=auth(admin["xid"]),
                              json={"action": "content_remove",
                                    "reason": "Cambridge paper, uploaded whole."})
        assert refused.status_code == 422

    def test_an_unknown_report_is_refused_rather_than_recorded(
            self, client, db, seed, admin):
        target = _user(db, "Bek")
        db.flush()
        refused = client.post("/api/v1/admin/moderation-actions",
                              headers=auth(admin["xid"]),
                              json={"action": "warn", "reason": "Abuse.",
                                    "target_user_xid": str(target["xid"]),
                                    "report_xid": str(uuid.uuid4())})
        assert refused.status_code == 404
        assert db.execute(text("SELECT count(*) FROM moderation_actions")).scalar() == 0

    def test_a_ban_that_names_nobody_is_recorded_and_ends_nothing(
            self, client, db, seed, admin):
        """**A defect, and the reason `action.ts` refuses to send this.**

        `target_user_xid` is optional and an xid matching no user leaves
        `target_id` as `None`, so the revocation and the suspension are both
        skipped — and the row is still written. The endpoint answers 201 with
        `sessions_revoked: 0` and the immutable log now says a user was banned.
        Not a refusal, not an action: a record that the report was handled.

        The console never sends one, and shows `sessions_revoked` afterwards so
        a zero on a ban reads as the alarm it is.
        """
        outcome = _ok(client.post("/api/v1/admin/moderation-actions",
                                  headers=auth(admin["xid"]),
                                  json={"action": "ban", "reason": "Grooming.",
                                        "target_user_xid": str(uuid.uuid4())}), 201)
        assert outcome["sessions_revoked"] == 0
        assert db.execute(text(
            "SELECT target_user_id FROM moderation_actions")).scalar() is None

    def test_taking_an_action_does_not_close_the_report(
            self, client, db, seed, admin):
        """**A gap the screen states in words.** No endpoint writes
        `safety_reports.status`, so a report stays `new` after it has been
        answered, and the minors queue never empties. A moderator who read the
        status as the record of their work would ban the same person twice."""
        target = _user(db, "Bek")
        report = _report(db, reporter=seed["author"].id, subject=target["id"])
        db.flush()

        _ok(client.post("/api/v1/admin/moderation-actions",
                        headers=auth(admin["xid"]),
                        json={"action": "ban", "reason": "Grooming.",
                              "target_user_xid": str(target["xid"]),
                              "report_xid": str(report)}), 201)
        after = _ok(client.get("/api/v1/admin/reports?queue=general",
                               headers=auth(admin["xid"])))["items"][0]
        assert after["status"] == "new"

    def test_a_centre_admin_may_not_act(self, client, db, seed, admin):
        centre_admin = _user(db, "Nodir", org_id=seed["org"].id, role="centre_admin")
        target = _user(db, "Bek")
        _sessions(db, target["id"], 1)
        db.flush()
        refused = client.post("/api/v1/admin/moderation-actions",
                              headers=auth(centre_admin["xid"]),
                              json={"action": "ban", "reason": "A rival's student.",
                                    "target_user_xid": str(target["xid"])})
        assert refused.status_code == 403
        assert db.execute(text("""
            SELECT count(*) FROM auth_sessions
            WHERE user_id = :u AND revoked_at IS NULL
        """).bindparams(u=target["id"])).scalar() == 1


# ── the socket ───────────────────────────────────────────────────────

def _admin_in(db, name="Rustam"):
    row = _user(db, name)
    db.execute(text("""
        INSERT INTO platform_role_grants (user_id, role, granted_by)
        VALUES (:u, 'platform_admin', :u)
    """).bindparams(u=row["id"]))
    db.flush()
    return row


def _until(ws, *types, limit=8):
    """The next frame of one of `types`, ignoring heartbeats."""
    seen = []
    for _ in range(limit):
        frame = ws.receive_json()
        if frame["type"] == "ping":
            continue
        seen.append(frame)
        if frame["type"] in types:
            return frame
    raise AssertionError(f"no {types} frame; saw {seen}")


def _subscribe(ws, *names):
    ws.send_json({"id": "s-1", "type": "subscribe", "channels": list(names)})
    frames = []
    for _ in range(12):
        frame = ws.receive_json()
        if frame["type"] == "ping":
            continue
        frames.append(frame)
        if frame["type"] == "subscribed":
            return frames
    raise AssertionError(f"no acknowledgement; saw {frames}")


class TestTheTicketTheConsoleMints:
    def test_it_carries_what_the_client_needs_and_is_burned_once(
            self, live, db, seed):
        """The property the reconnect logic is built on: **a ticket works
        exactly once.** A helper that cached one would connect, drop on the
        first tunnel outage — routine on these networks — and then be refused
        for the rest of the session with a close code that looks like a dead
        server. So a new ticket is minted for every connect, including retries.
        """
        staff = _admin_in(db)
        db.commit()
        minted = _ok(live.post("/api/v1/realtime/ticket",
                               headers=auth(staff["xid"])))
        assert set(minted) == {"ticket", "url", "expires_at"}

        with live.websocket_connect(f"/realtime?ticket={minted['ticket']}") as ws:
            assert ws.receive_json()["type"] == "hello"

        with live.websocket_connect(f"/realtime?ticket={minted['ticket']}") as second:
            with pytest.raises(WebSocketDisconnect) as refused:
                for _ in range(4):
                    second.receive_json()
        assert refused.value.code == gateway.CLOSE_UNAUTHORIZED

    def test_the_gateway_is_not_under_the_api_prefix(self, live, db, seed):
        """**A deployment defect the console works around.**

        The gateway is mounted at `/realtime`, deliberately outside `/api/v1` —
        a socket is not an HTTP operation and `check_api_coverage.py` compares
        that prefix against the OpenAPI document. `docker-compose.yml` sets
        `REALTIME_URL: wss://${DOMAIN}/api/v1/realtime`, which is the `url` this
        endpoint hands the client, and which nothing serves. The console builds
        its URL from the page origin and ignores that field.
        """
        staff = _admin_in(db)
        db.commit()
        minted = _ok(live.post("/api/v1/realtime/ticket", headers=auth(staff["xid"])))

        with pytest.raises(Exception):  # noqa: B017 — any failure; there is no route
            with live.websocket_connect(
                    f"/api/v1/realtime?ticket={minted['ticket']}") as ws:
                ws.receive_json()


class TestTheSafetyChannel:
    def test_the_console_sequence_reaches_it(self, live, db, seed):
        """Exactly what the screen does: mint over HTTP, connect, subscribe."""
        staff = _admin_in(db)
        db.commit()
        ticket = _ok(live.post("/api/v1/realtime/ticket",
                               headers=auth(staff["xid"])))["ticket"]

        with live.websocket_connect(f"/realtime?ticket={ticket}") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            # Implicit for staff, so the queue cannot miss a frame in the
            # half second between connecting and asking.
            assert "safety" in hello["data"]["implicit_channels"]
            assert ws.receive_json()["type"] == "subscribed"

            frames = _subscribe(ws, "safety")
            granted = next(f["data"]["channels"] for f in frames
                           if f["type"] == "subscribed")
            assert "safety" in granted

            realtime.publish("safety", "safety.action_taken", {"action": "ban"})
            assert _until(ws, "safety.action_taken")["data"]["action"] == "ban"

    def test_a_centre_admin_is_refused_and_told_nothing_more(self, live, db, seed):
        """The refusal is `forbidden_channel` and carries no reason, on purpose.
        The console does not try to turn it into one — it says live updates are
        not available and keeps polling."""
        centre_admin = _user(db, "Nodir", org_id=seed["org"].id, role="centre_admin")
        db.commit()
        ticket = _ok(live.post("/api/v1/realtime/ticket",
                               headers=auth(centre_admin["xid"])))["ticket"]

        with live.websocket_connect(f"/realtime?ticket={ticket}") as ws:
            hello = ws.receive_json()
            assert "safety" not in hello["data"]["implicit_channels"]
            assert ws.receive_json()["type"] == "subscribed"

            frames = _subscribe(ws, "safety")
            error = next(f for f in frames if f["type"] == "error")
            assert error["data"]["code"] == "forbidden_channel"
            assert "centre" not in error["data"]["message"].lower()

            realtime.publish("safety", "safety.report_filed", {"priority": "critical"})
            with pytest.raises(AssertionError):
                _until(ws, "safety.report_filed", limit=4)

    def test_an_action_puts_nothing_on_the_channel(self, live, db, seed):
        """**Why the screen polls.** `safety.report_filed` and
        `safety.action_taken` are declared, authorized and emitted by nothing —
        the contract lists them under "declared, no producer yet". A queue that
        refreshed only on a frame would never refresh.

        When a producer is wired, this test should fail and the polling comment
        in `Moderation.tsx` should be revisited with it.
        """
        staff = _admin_in(db)
        target = _user(db, "Bek")
        db.commit()
        ticket = _ok(live.post("/api/v1/realtime/ticket",
                               headers=auth(staff["xid"])))["ticket"]

        with live.websocket_connect(f"/realtime?ticket={ticket}") as ws:
            assert ws.receive_json()["type"] == "hello"
            assert ws.receive_json()["type"] == "subscribed"
            _subscribe(ws, "safety")

            _ok(live.post("/api/v1/admin/moderation-actions",
                          headers=auth(staff["xid"]),
                          json={"action": "warn", "reason": "First offence.",
                                "target_user_xid": str(target["xid"])}), 201)
            with pytest.raises(AssertionError):
                _until(ws, "safety.action_taken", "safety.report_filed", limit=4)

        with contextlib.suppress(Exception):
            db.execute(text("DELETE FROM moderation_actions"))
            db.commit()
