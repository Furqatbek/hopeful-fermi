"""The two rules in this system that are not merely correctness bugs.

**Fairness.** A competition starts for everyone at the same instant. The payload
is prefetched encrypted at T-120s and the key is released at T-0, and the clock
runs from `starts_at` rather than from whenever a client got around to asking.
If any of that slips, one student got a head start and the contest is worthless.

**Child safety.** A minor is never matched with an adult. Not "the UI does not
offer it" — the slot list is filtered server-side and booking re-checks, so a
client that guesses a slot id still cannot cross the band.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api.deps import issue_access_token
from app.modules.exam.models import Attempt

def _now() -> dt.datetime:
    """Read at CALL time, not at import.

    A module-level constant is minutes stale by the time the full suite reaches
    this file, which turns "starts in 60 seconds" into "started a minute ago" and
    makes these tests fail for a reason that has nothing to do with the code.
    """
    return dt.datetime.now(dt.UTC)


@pytest.fixture
def client(engine, db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def admin(db, seed):
    from app.modules.identity.models import PlatformRoleGrant

    db.add(PlatformRoleGrant(user_id=seed["author"].id, role="platform_admin",
                             granted_by=seed["author"].id))
    db.flush()
    return {"Authorization": f"Bearer {issue_access_token(str(seed['author'].xid))}"}


@pytest.fixture
def student_auth(seed):
    return {"Authorization": f"Bearer {issue_access_token(str(seed['student'].xid))}"}


@pytest.fixture
def entitled_student(db, seed):
    from app.modules.billing.models import EntitlementRow

    for feature in ("competition.entry", "mock.unlimited"):
        db.add(EntitlementRow(subject_kind="user", subject_id=seed["student"].id,
                              feature=feature, source_kind="order",
                              starts_at=_now() - dt.timedelta(days=1)))
    db.flush()
    return seed


def _competition(db, published, *, starts_in: int, duration: int = 1800,
                 lobby_lead: int = 120, status: str = "registration") -> dict:
    """Created directly rather than through the endpoint.

    The endpoint's own rules are tested separately; here the contest's position
    in time is the variable, and driving it through the API would mean waiting.
    """
    starts = _now() + dt.timedelta(seconds=starts_in)
    return db.execute(text("""
        INSERT INTO competitions (org_id, test_version_id, title, lobby_opens_at,
                                  starts_at, ends_at, duration_seconds, status,
                                  visibility, payload_key_id, created_by)
        VALUES (:org, :tv, 'Friday Contest', :lobby, :starts,
                :starts + make_interval(secs => :dur), :dur, :status, 'org',
                'k-test-1', :by)
        RETURNING id, xid
    """).bindparams(org=published["org"].id, tv=published["test_version"].id,
                    lobby=starts - dt.timedelta(seconds=lobby_lead), starts=starts,
                    dur=duration, status=status,
                    by=published["author"].id)).mappings().one()


def _register(db, competition_id: int, user_id: int) -> None:
    db.execute(text("""
        INSERT INTO competition_entries (competition_id, user_id) VALUES (:c, :u)
    """).bindparams(c=competition_id, u=user_id))
    db.flush()


class TestSynchronizedStart:
    def test_the_lobby_is_closed_before_it_opens(self, client, student_auth, db,
                                                 published, entitled_student):
        contest = _competition(db, published, starts_in=3600)
        _register(db, contest["id"], published["student"].id)
        r = client.get(f"/api/v1/competitions/{contest['xid']}/lobby",
                       headers=student_auth)
        assert r.status_code == 425, r.text
        assert r.json()["code"] == "lobby_not_open"
        # The client renders its countdown from these, never from the device clock.
        assert r.json()["lobby_opens_at"] and r.json()["server_now"]

    def test_the_payload_is_encrypted_and_undecryptable_before_t0(
            self, client, student_auth, db, published, entitled_student):
        """The whole point of the two-phase start.

        An unencrypted payload sitting on the device from T-120s is a two-minute
        reading head start for anyone who opens devtools, so what the lobby hands
        out must be useless until the key is released.
        """
        contest = _competition(db, published, starts_in=60)
        _register(db, contest["id"], published["student"].id)

        lobby = client.get(f"/api/v1/competitions/{contest['xid']}/lobby",
                           headers=student_auth)
        assert lobby.status_code == 200, lobby.text
        body = lobby.json()
        ciphertext = base64.b64decode(body["ciphertext"])
        assert b"Cartography" not in ciphertext
        assert b"question_version_xid" not in ciphertext

        key = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                          headers=student_auth)
        assert key.status_code == 425
        assert key.json()["code"] == "not_started"

    def test_the_key_decrypts_the_payload_the_lobby_served(
            self, client, student_auth, db, published, entitled_student):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        contest = _competition(db, published, starts_in=60)
        _register(db, contest["id"], published["student"].id)
        lobby = client.get(f"/api/v1/competitions/{contest['xid']}/lobby",
                           headers=student_auth).json()

        db.execute(text("""
            UPDATE competitions SET starts_at = now() - interval '5 seconds',
                                    lobby_opens_at = now() - interval '2 minutes'
            WHERE id = :c
        """).bindparams(c=contest["id"]))
        db.flush()

        released = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                               headers=student_auth)
        assert released.status_code == 200, released.text
        plaintext = AESGCM(base64.b64decode(released.json()["key"])).decrypt(
            base64.b64decode(lobby["iv"]), base64.b64decode(lobby["ciphertext"]), None)
        snapshot = json.loads(plaintext)
        assert snapshot["title"] == "Mock 1 v1"
        assert snapshot["sections"][0]["groups"][0]["questions"]

    def test_the_snapshot_carries_no_answer_keys(self, client, student_auth, db,
                                                 published, entitled_student):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        contest = _competition(db, published, starts_in=60)
        _register(db, contest["id"], published["student"].id)
        lobby = client.get(f"/api/v1/competitions/{contest['xid']}/lobby",
                           headers=student_auth).json()
        db.execute(text("UPDATE competitions SET starts_at = now() - interval '5 seconds' "
                        "WHERE id = :c").bindparams(c=contest["id"]))
        db.flush()
        key = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                          headers=student_auth).json()
        plaintext = AESGCM(base64.b64decode(key["key"])).decrypt(
            base64.b64decode(lobby["iv"]), base64.b64decode(lobby["ciphertext"]), None)
        assert b"bicycle" not in plaintext
        assert b"accept" not in plaintext

    def test_the_clock_runs_from_starts_at_not_from_the_request(
            self, client, student_auth, db, published, entitled_student):
        """A student who asks for the key thirty seconds late does not get thirty
        extra seconds. Everyone's deadline is the contest's."""
        contest = _competition(db, published, starts_in=-30, duration=600)
        _register(db, contest["id"], published["student"].id)
        db.execute(text("UPDATE competitions SET status = 'live' WHERE id = :c")
                   .bindparams(c=contest["id"]))
        db.flush()

        body = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                           headers=student_auth)
        assert body.status_code == 200, body.text
        remaining = body.json()["attempt"]["seconds_remaining"]
        # 600s duration, joined 30s late -> about 570 left, NOT 600.
        assert 555 <= remaining <= 580, remaining

    def test_an_unregistered_user_gets_nothing(self, client, student_auth, db,
                                               published, entitled_student):
        contest = _competition(db, published, starts_in=60)
        r = client.get(f"/api/v1/competitions/{contest['xid']}/lobby",
                       headers=student_auth)
        assert r.status_code == 403
        assert r.json()["code"] == "not_registered"

    def test_asking_twice_returns_the_same_attempt(self, client, student_auth, db,
                                                   published, entitled_student):
        """A client that retries because the response was lost must not burn a
        second attempt — this endpoint is the one a flaky network will retry."""
        contest = _competition(db, published, starts_in=-5, duration=600)
        _register(db, contest["id"], published["student"].id)
        first = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                            headers=student_auth).json()
        second = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                             headers=student_auth).json()
        assert first["attempt"]["xid"] == second["attempt"]["xid"]
        assert db.scalar(
            select(Attempt.id).where(Attempt.competition_id == contest["id"]).limit(2)
        ) is not None

    def test_starting_freezes_the_answer_keys(self, client, student_auth, db,
                                              published, entitled_student):
        """The record that makes "a key fix cannot silently re-rank a finished
        contest" enforceable rather than a promise."""
        contest = _competition(db, published, starts_in=-5)
        _register(db, contest["id"], published["student"].id)
        client.post(f"/api/v1/competitions/{contest['xid']}/key", headers=student_auth)

        freeze = db.execute(text("SELECT key_freeze, status FROM competitions WHERE id = :c")
                            .bindparams(c=contest["id"])).mappings().one()
        assert freeze["status"] == "live"
        assert freeze["key_freeze"]["key_versions"], "no key versions were captured"
        assert freeze["key_freeze"]["band_map_version_id"]


class TestCompetitionRegradeDecision:
    def test_a_teacher_cannot_decide(self, client, db, published):
        contest = _competition(db, published, starts_in=-3600, status="final")
        job = db.execute(text("""
            INSERT INTO regrade_jobs (trigger, subject_type, subject_id,
                                      initiated_by, reason)
            VALUES ('answer_key_change', 'question_version', 1, :u, 'bad key')
            RETURNING xid
        """).bindparams(u=published["author"].id)).mappings().one()
        teacher = {"Authorization":
                   f"Bearer {issue_access_token(str(published['author'].xid))}"}
        r = client.post(
            f"/api/v1/competitions/{contest['xid']}/regrade-decisions/{job['xid']}",
            json={"decision": "leave_as_is", "rationale": "no rank change"},
            headers=teacher)
        assert r.status_code == 403
        assert r.json()["code"] == "admin_only"

    def test_republishing_requires_a_public_notice(self, client, admin, db, published):
        """If a podium moves, the people on it are told in writing. Making the
        notice mandatory is the only way that survives a busy Friday."""
        contest = _competition(db, published, starts_in=-3600, status="final")
        job = db.execute(text("""
            INSERT INTO regrade_jobs (trigger, subject_type, subject_id,
                                      initiated_by, reason)
            VALUES ('answer_key_change', 'question_version', 1, :u, 'bad key')
            RETURNING xid
        """).bindparams(u=published["author"].id)).mappings().one()
        r = client.post(
            f"/api/v1/competitions/{contest['xid']}/regrade-decisions/{job['xid']}",
            json={"decision": "regrade_and_republish", "rationale": "key was wrong"},
            headers=admin)
        assert r.status_code == 409
        assert r.json()["code"] == "public_notice_required"

    def test_a_decision_is_written_to_the_audit_log(self, client, admin, db, published):
        contest = _competition(db, published, starts_in=-3600, status="final")
        job = db.execute(text("""
            INSERT INTO regrade_jobs (trigger, subject_type, subject_id,
                                      initiated_by, reason)
            VALUES ('answer_key_change', 'question_version', 1, :u, 'bad key')
            RETURNING xid
        """).bindparams(u=published["author"].id)).mappings().one()
        r = client.post(
            f"/api/v1/competitions/{contest['xid']}/regrade-decisions/{job['xid']}",
            json={"decision": "leave_as_is", "rationale": "ranks unchanged"},
            headers=admin)
        assert r.status_code == 200, r.text
        logged = db.execute(text("""
            SELECT actor_user_id, reason, after FROM audit_log
            WHERE action = 'competition.regrade_decision' AND subject_id = :s
        """).bindparams(s=str(contest["xid"]))).mappings().one()
        assert logged["actor_user_id"] == published["author"].id
        assert logged["reason"] == "ranks unchanged"


class TestLeaderboardDiscloseNothingExtra:
    def test_only_a_display_name_is_returned(self, client, student_auth, db, published,
                                             entitled_student):
        """The most-screenshotted surface in the product. It must never carry an
        age, a phone number or a centre name."""
        contest = _competition(db, published, starts_in=-3600, status="final")
        _register(db, contest["id"], published["student"].id)
        attempt = db.execute(text("""
            INSERT INTO attempts (user_id, test_version_id, competition_id, mode,
                                  status, started_at, submitted_at)
            VALUES (:u, :tv, :c, 'exam', 'scored', now(), now()) RETURNING id
        """).bindparams(u=published["student"].id, tv=published["test_version"].id,
                        c=contest["id"])).scalar()
        run = db.execute(text("""
            INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                    raw_score, max_raw, band)
            VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, 3, 3, 7.0) RETURNING id
        """).bindparams(a=attempt)).scalar()
        db.execute(text("""
            INSERT INTO competition_results (competition_id, user_id, attempt_id,
                                             score_run_id, raw_score, band,
                                             duration_ms, submitted_at, rank)
            VALUES (:c, :u, :a, :r, 3, 7.0, 900000, now(), 1)
        """).bindparams(c=contest["id"], u=published["student"].id, a=attempt, r=run))
        db.flush()

        board = client.get(f"/api/v1/competitions/{contest['xid']}/leaderboard",
                           headers=student_auth)
        assert board.status_code == 200, board.text
        entry = board.json()["entries"][0]
        assert set(entry["user"]) == {"xid", "display_name"}
        assert entry["user"]["display_name"] == "Aziza"
        assert published["student"].phone not in board.text
        assert board.json()["my_rank"] == 1


# ── speaking ─────────────────────────────────────────────────────────

def _slot(db, published, *, age_band: str, audience: str = "public",
          starts_in: int = 3600) -> dict:
    return db.execute(text("""
        INSERT INTO speaking_slots (org_id, starts_at, duration_minutes, capacity,
                                    status, audience, age_band, created_by)
        VALUES (:org, :starts, 15, 20, 'booking', :aud, :band, :by)
        RETURNING id, xid
    """).bindparams(org=published["org"].id,
                    starts=_now() + dt.timedelta(seconds=starts_in), aud=audience,
                    band=age_band, by=published["author"].id)).mappings().one()


@pytest.fixture
def minor(db, seed):
    """A user who is genuinely under 18 on the fixture's clock.

    Created rather than borrowed: the seed's "student" was born in 2008 and has
    since turned 18, which is exactly the kind of assumption that makes an
    age-banding test quietly stop testing anything.
    """
    from app.modules.identity.models import Organization, OrgMembership, User

    user = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Malika",
                date_of_birth=(dt.date.today() - dt.timedelta(days=365 * 14)))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id, role="student"))
    db.flush()
    assert user.adult_at > dt.date.today(), "the fixture stopped producing a minor"
    _ = Organization
    return user


@pytest.fixture
def minor_auth(minor):
    return {"Authorization": f"Bearer {issue_access_token(str(minor.xid))}"}


class TestMinorsAreNeverMatchedWithAdults:
    """`minor` is 14; the seed author was born in 1995 and is an adult."""

    def test_a_minor_does_not_see_adult_slots(self, client, minor_auth, db, seed):
        adult = _slot(db, seed, age_band="adult")
        minor = _slot(db, seed, age_band="minor")
        db.flush()
        listed = client.get("/api/v1/speaking/slots", headers=minor_auth)
        assert listed.status_code == 200, listed.text
        xids = {s["xid"] for s in listed.json()}
        assert str(minor["xid"]) in xids
        assert str(adult["xid"]) not in xids

    def test_an_adult_does_not_see_minor_slots(self, client, db, seed):
        adult = _slot(db, seed, age_band="adult")
        minor = _slot(db, seed, age_band="minor")
        db.flush()
        author = {"Authorization":
                  f"Bearer {issue_access_token(str(seed['author'].xid))}"}
        xids = {s["xid"] for s in client.get("/api/v1/speaking/slots",
                                             headers=author).json()}
        assert str(adult["xid"]) in xids
        assert str(minor["xid"]) not in xids

    def test_guessing_the_slot_id_does_not_get_a_minor_into_an_adult_pool(
            self, client, minor_auth, db, seed):
        """The check that makes it an invariant rather than a UI behaviour.

        Filtering the list is not enough: a client that already holds the id, or
        one written by someone curious, must still be refused at the booking.
        """
        adult = _slot(db, seed, age_band="adult")
        db.flush()
        r = client.post(f"/api/v1/speaking/slots/{adult['xid']}/book",
                        headers=minor_auth)
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "age_band_mismatch"
        assert not db.scalar(text("SELECT count(*) FROM speaking_slot_bookings "
                                  "WHERE slot_id = :s").bindparams(s=adult["id"]))

    def test_a_minor_can_book_a_minor_slot(self, client, minor_auth, db, seed):
        minor = _slot(db, seed, age_band="minor")
        db.flush()
        r = client.post(f"/api/v1/speaking/slots/{minor['xid']}/book",
                        headers=minor_auth)
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "booked"

    def test_the_live_queue_records_the_actors_own_band(self, client, minor_auth,
                                                        db, minor):
        """`age_band` is taken from the account, never from the request body.

        The matcher's index leads with it, so a cross-band candidate is not merely
        forbidden — it is not returned by the query that finds candidates.
        """
        r = client.post("/api/v1/speaking/queue",
                        json={"language": "en", "band_min": 5, "band_max": 7},
                        headers=minor_auth)
        assert r.status_code == 201, r.text
        band = db.scalar(text("""
            SELECT age_band FROM speaking_queue_entries
            WHERE user_id = :u ORDER BY joined_at DESC LIMIT 1
        """).bindparams(u=minor.id))
        assert band == "minor"

    def test_a_mixed_session_must_be_a_supervised_cohort_slot(self, client, db, seed):
        author = {"Authorization":
                  f"Bearer {issue_access_token(str(seed['author'].xid))}"}
        r = client.post("/api/v1/speaking/slots",
                        json={"starts_at": "2026-08-01T09:00:00Z",
                              "duration_minutes": 15, "audience": "public",
                              "age_band": "mixed_supervised"}, headers=author)
        assert r.status_code == 409
        assert r.json()["code"] == "mixed_requires_cohort"

    def test_a_slot_defaults_to_the_creators_own_band(self, client, db, seed):
        """Fails closed: a defaulting mistake must not put minors in an adult pool.

        The author is an adult, so their slot is an adult slot unless they say
        otherwise — and a teacher who is themselves a minor could never create an
        adult pool by omission.
        """
        author = {"Authorization":
                  f"Bearer {issue_access_token(str(seed['author'].xid))}"}
        r = client.post("/api/v1/speaking/slots",
                        json={"starts_at": "2026-08-01T09:00:00Z",
                              "duration_minutes": 15}, headers=author)
        assert r.status_code == 201, r.text
        assert r.json()["age_band"] == "adult"


class TestSafetyReport:
    def _pair(self, db, reporter, peer, age_band="minor"):
        return db.execute(text("""
            INSERT INTO speaking_pairs (origin, user_a_id, user_b_id, age_band)
            VALUES ('live_queue', :a, :b, :band) RETURNING id, xid
        """).bindparams(a=reporter.id, b=peer.id, band=age_band)).mappings().one()

    def test_involves_minor_is_set_by_the_system_not_the_reporter(
            self, client, minor_auth, minor, db, seed):
        """A report about a child must not depend on the reporter remembering to
        tick a box — nor on the pair record being labelled correctly."""
        # Deliberately mislabelled `adult`: the system must still derive the truth
        # from the participants' actual ages.
        pair = self._pair(db, minor, seed["author"], age_band="adult")
        db.flush()
        r = client.post(f"/api/v1/speaking/pairs/{pair['xid']}/report",
                        data={"category": "harassment", "description": "rude"},
                        headers=minor_auth)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["involves_minor"] is True
        assert body["priority"] == "critical"
        assert body["has_evidence"] is False

    def test_a_report_between_two_adults_is_high_not_critical(self, client, db, seed):
        """Priority is a triage decision, and it must still leave headroom.

        If everything is critical the queue has no order, and the reports that
        genuinely involve a child stop being first.
        """
        pair = self._pair(db, seed["student"], seed["author"], age_band="adult")
        db.flush()
        headers = {"Authorization":
                   f"Bearer {issue_access_token(str(seed['student'].xid))}"}
        r = client.post(f"/api/v1/speaking/pairs/{pair['xid']}/report",
                        data={"category": "spam"}, headers=headers)
        assert r.status_code == 201, r.text
        assert r.json()["involves_minor"] is False
        assert r.json()["priority"] == "high"

    def test_audio_reaches_the_server_only_with_a_report(self, client, student_auth,
                                                         db, seed):
        """The only path by which conversation audio is ever stored.

        The client's rolling ~60 s buffer is discarded unless a report is filed —
        evidence without routine recording of minors' conversations.
        """
        pair = self._pair(db, seed["student"], seed["author"])
        db.flush()
        assert not db.scalar(text("SELECT count(*) FROM media_assets "
                                  "WHERE bucket = 'safety-evidence'"))
        r = client.post(f"/api/v1/speaking/pairs/{pair['xid']}/report",
                        data={"category": "grooming"},
                        files={"audio_buffer": ("buffer.webm", b"\x00fake",
                                                "audio/webm")},
                        headers=student_auth)
        assert r.status_code == 201, r.text
        assert r.json()["has_evidence"] is True
        stored = db.execute(text("""
            SELECT status, bucket FROM media_assets WHERE bucket = 'safety-evidence'
        """)).mappings().one()
        # Quarantined, not 'ready': evidence is never servable as ordinary media.
        assert stored["status"] == "quarantined"

    def test_a_stranger_cannot_report_a_session_they_were_not_in(
            self, client, db, seed):
        pair = self._pair(db, seed["student"], seed["author"])
        db.flush()
        from app.modules.identity.models import User

        outsider = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
                        given_name="Nodir",
                        date_of_birth=dt.datetime(1992, 1, 1).date())
        db.add(outsider)
        db.flush()
        headers = {"Authorization": f"Bearer {issue_access_token(str(outsider.xid))}"}
        r = client.post(f"/api/v1/speaking/pairs/{pair['xid']}/report",
                        data={"category": "spam"}, headers=headers)
        assert r.status_code == 404
