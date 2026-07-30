"""Speaking slots, booking, check-in, and the safety report path.

This module carries the one rule in the product that is not merely a correctness
bug — a minor is never matched 1:1 with an adult — and, unusually for the routers
read so far, **that rule is right.** `matching.py` partitions candidates on
`is_minor` before pairing, so a cross-band pair is structurally unreachable
rather than filtered, and it says in as many words that `mixed_supervised` puts
both ages in one ROOM without authorising a minor–adult PAIR. The router's list
filter and booking re-check are the second and third layers. All three are
tested here or in `tests/speaking/`.

What was wrong is the other half of the brief's audio rule.

**The evidence buffer was never stored.** `report_pair` read the upload, hashed
it, recorded `bytes` and a `storage_key` — and dropped it. Nothing called
`storage().put`. A report came back `has_evidence: true`, the pair carried an
`evidence_media_id`, and the moderator who opened it found a row pointing at an
object that had never existed. Audio is allowed onto these servers only when
"required for a safety report", and the safety report was the case that threw it
away.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token


@pytest.fixture
def client(db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def store(tmp_path):
    """A file-backed store rooted in the test's own directory.

    Installed process-wide because the endpoint resolves `storage()` itself, and
    reset to `None` rather than to the previous instance — restoring a memoized
    object would pin it for the rest of the session.
    """
    from app.platform.storage import FileStorage, set_storage

    backend = FileStorage(root=tmp_path / "media", bucket="test-media")
    set_storage(backend)
    yield backend
    set_storage(None)


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


def _user(db, phone: str, name: str, *, dob: str, org_id=None, role="student"):
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


MINOR_DOB = (dt.date.today() - dt.timedelta(days=365 * 15)).isoformat()
ADULT_DOB = "1998-01-01"


@pytest.fixture
def teacher(db, seed):
    return _user(db, "+998910000001", "Dilnoza", dob="1985-01-01",
                 org_id=seed["org"].id, role="teacher")


@pytest.fixture
def adult(db, seed):
    return _user(db, "+998910000002", "Bekzod", dob=ADULT_DOB, org_id=seed["org"].id)


@pytest.fixture
def child(db, seed):
    return _user(db, "+998910000003", "Aziza", dob=MINOR_DOB, org_id=seed["org"].id)


def _slot(client, teacher, *, band="adult", audience="public", cohort_xid=None,
          starts_in=dt.timedelta(hours=1)):
    body = {"starts_at": (dt.datetime.now(dt.UTC) + starts_in).isoformat(),
            "audience": audience, "age_band": band}
    if cohort_xid:
        body["cohort_xid"] = str(cohort_xid)
    return client.post("/api/v1/speaking/slots", headers=auth(teacher["xid"]),
                       json=body)


# ── the safety rule, at the HTTP boundary ────────────────────────────

class TestAgeBandingIsEnforcedServerSide:
    """"Enforce age banding at the matching layer, not in the client." The
    matcher is layer three; these are layers one and two."""

    def test_a_minor_is_not_shown_adult_slots(self, client, teacher, child):
        _slot(client, teacher, band="adult")
        listed = client.get("/api/v1/speaking/slots", headers=auth(child["xid"])).json()
        assert listed == []

    def test_an_adult_is_not_shown_minor_slots(self, client, teacher, adult):
        _slot(client, teacher, band="minor")
        listed = client.get("/api/v1/speaking/slots", headers=auth(adult["xid"])).json()
        assert listed == []

    def test_each_sees_their_own_band(self, client, teacher, adult, child):
        _slot(client, teacher, band="adult")
        _slot(client, teacher, band="minor")
        assert [s["age_band"] for s in client.get(
            "/api/v1/speaking/slots", headers=auth(adult["xid"])).json()] == ["adult"]
        assert [s["age_band"] for s in client.get(
            "/api/v1/speaking/slots", headers=auth(child["xid"])).json()] == ["minor"]

    def test_a_minor_who_guesses_an_adult_slot_id_still_cannot_book(
            self, client, teacher, adult, child):
        """The list filter is a UI convenience. This is the check that makes the
        rule an invariant."""
        xid = _slot(client, teacher, band="adult").json()["xid"]
        refused = client.post(f"/api/v1/speaking/slots/{xid}/book",
                              headers=auth(child["xid"]))
        assert refused.status_code == 403
        assert refused.json()["code"] == "age_band_mismatch"

    def test_an_adult_who_guesses_a_minor_slot_id_cannot_either(
            self, client, teacher, adult):
        xid = _slot(client, teacher, band="minor").json()["xid"]
        refused = client.post(f"/api/v1/speaking/slots/{xid}/book",
                              headers=auth(adult["xid"]))
        assert refused.json()["code"] == "age_band_mismatch"

    def test_the_refusal_explains_itself(self, client, teacher, child):
        """"The student should understand why, and support needs to be able to
        explain it without reading code." """
        xid = _slot(client, teacher, band="adult").json()["xid"]
        message = client.post(f"/api/v1/speaking/slots/{xid}/book",
                              headers=auth(child["xid"])).json()["title"]
        assert "age group" in message and "never paired" in message

    def test_a_slot_defaults_to_the_creators_own_band(self, client, db, seed):
        """"A defaulting mistake should fail closed for minors." A teacher who is
        themselves a minor cannot accidentally open an adult room."""
        young_teacher = _user(db, "+998910000010", "Young", dob=MINOR_DOB,
                              org_id=seed["org"].id, role="teacher")
        response = client.post("/api/v1/speaking/slots",
                               headers=auth(young_teacher["xid"]),
                               json={"starts_at": (dt.datetime.now(dt.UTC)
                                                   + dt.timedelta(hours=1)).isoformat()})
        assert response.json()["age_band"] == "minor"

    def test_a_mixed_session_must_be_a_cohort_slot(self, client, teacher):
        """`mixed_supervised` exists for a teacher-run class. A public one would
        be a room of strangers of both ages."""
        response = _slot(client, teacher, band="mixed_supervised", audience="public")
        assert response.status_code == 409
        assert response.json()["code"] == "mixed_requires_cohort"

    def test_a_mixed_cohort_slot_admits_both_ages(self, client, db, seed, teacher,
                                                  adult, child):
        """Both ages in one ROOM is the point of the band. The matcher still
        refuses to PAIR them — `tests/speaking/` covers that."""
        cohort_id = db.scalar(text("""
            INSERT INTO cohorts (org_id, name, created_by) VALUES (:o, 'Evening', :u)
            RETURNING id
        """).bindparams(o=seed["org"].id, u=teacher["id"]))
        for user in (adult, child):
            db.execute(text("""
                INSERT INTO cohort_members (cohort_id, user_id, status)
                VALUES (:c, :u, 'active')
            """).bindparams(c=cohort_id, u=user["id"]))
        db.flush()
        cohort_xid = db.scalar(text("SELECT xid FROM cohorts WHERE id = :c")
                               .bindparams(c=cohort_id))
        xid = _slot(client, teacher, band="mixed_supervised", audience="cohort",
                    cohort_xid=cohort_xid).json()["xid"]
        for user in (adult, child):
            assert client.post(f"/api/v1/speaking/slots/{xid}/book",
                               headers=auth(user["xid"])).status_code == 201

    def test_an_outsider_cannot_join_a_supervised_session(self, client, db, seed,
                                                          teacher, adult):
        """"This supervised session is for its cohort only." Otherwise the mixed
        band is a way for any adult to reach a room with children in it."""
        cohort_id = db.scalar(text("""
            INSERT INTO cohorts (org_id, name, created_by) VALUES (:o, 'Evening', :u)
            RETURNING id
        """).bindparams(o=seed["org"].id, u=teacher["id"]))
        db.flush()
        cohort_xid = db.scalar(text("SELECT xid FROM cohorts WHERE id = :c")
                               .bindparams(c=cohort_id))
        xid = _slot(client, teacher, band="mixed_supervised", audience="cohort",
                    cohort_xid=cohort_xid).json()["xid"]
        refused = client.post(f"/api/v1/speaking/slots/{xid}/book",
                              headers=auth(adult["xid"]))
        assert refused.status_code == 403
        assert refused.json()["code"] == "not_in_cohort"


class TestSlotCreation:
    def test_a_student_cannot_open_one(self, client, adult):
        response = _slot(client, adult)
        assert response.status_code == 403
        assert response.json()["code"] == "not_a_teacher"

    def test_an_unknown_cohort_is_a_404(self, client, teacher):
        assert _slot(client, teacher, band="mixed_supervised", audience="cohort",
                     cohort_xid=uuid.uuid4()).status_code == 404

    def test_a_cohort_at_another_centre_is_a_404(self, client, db, teacher):
        """A teacher cannot open a session against someone else's class."""
        org = db.scalar(text("""
            INSERT INTO organizations (name, slug, status)
            VALUES ('Rival', 'rival-speaking', 'active') RETURNING id
        """))
        cohort_xid = db.scalar(text("""
            INSERT INTO cohorts (org_id, name, created_by) VALUES (:o, 'Theirs', :u)
            RETURNING xid
        """).bindparams(o=org, u=teacher["id"]))
        db.flush()
        assert _slot(client, teacher, band="mixed_supervised", audience="cohort",
                     cohort_xid=cohort_xid).status_code == 404


class TestBookingAndCheckIn:
    def test_booking_then_reading_the_slot_shows_it(self, client, teacher, adult):
        xid = _slot(client, teacher).json()["xid"]
        assert client.post(f"/api/v1/speaking/slots/{xid}/book",
                           headers=auth(adult["xid"])).json()["status"] == "booked"
        listed = client.get("/api/v1/speaking/slots", headers=auth(adult["xid"])).json()
        assert listed[0]["my_booking"]["status"] == "booked"
        assert listed[0]["booked_count"] == 1

    def test_booking_twice_is_idempotent(self, client, db, teacher, adult):
        xid = _slot(client, teacher).json()["xid"]
        for _ in range(2):
            client.post(f"/api/v1/speaking/slots/{xid}/book", headers=auth(adult["xid"]))
        assert db.scalar(text("SELECT count(*) FROM speaking_slot_bookings")) == 1

    def test_a_full_slot_is_refused(self, client, db, seed, teacher):
        xid = client.post("/api/v1/speaking/slots", headers=auth(teacher["xid"]),
                          json={"starts_at": (dt.datetime.now(dt.UTC)
                                              + dt.timedelta(hours=1)).isoformat(),
                                "capacity": 1, "age_band": "adult"}).json()["xid"]
        first = _user(db, "+998910000021", "One", dob=ADULT_DOB, org_id=seed["org"].id)
        second = _user(db, "+998910000022", "Two", dob=ADULT_DOB, org_id=seed["org"].id)
        assert client.post(f"/api/v1/speaking/slots/{xid}/book",
                           headers=auth(first["xid"])).status_code == 201
        refused = client.post(f"/api/v1/speaking/slots/{xid}/book",
                              headers=auth(second["xid"]))
        assert refused.status_code == 409
        assert refused.json()["code"] == "slot_full"

    def test_a_closed_slot_is_refused(self, client, db, teacher, adult):
        xid = _slot(client, teacher).json()["xid"]
        db.execute(text("UPDATE speaking_slots SET status = 'closed'"))
        db.flush()
        refused = client.post(f"/api/v1/speaking/slots/{xid}/book",
                              headers=auth(adult["xid"]))
        assert refused.json()["code"] == "slot_closed"

    def test_cancelling_frees_the_place(self, client, teacher, adult):
        xid = _slot(client, teacher).json()["xid"]
        client.post(f"/api/v1/speaking/slots/{xid}/book", headers=auth(adult["xid"]))
        assert client.delete(f"/api/v1/speaking/slots/{xid}/book",
                             headers=auth(adult["xid"])).status_code == 204
        listed = client.get("/api/v1/speaking/slots", headers=auth(adult["xid"])).json()
        assert listed[0]["booked_count"] == 0
        assert listed[0]["my_booking"]["status"] == "cancelled"

    def test_rebooking_after_cancelling_works(self, client, teacher, adult):
        xid = _slot(client, teacher).json()["xid"]
        client.post(f"/api/v1/speaking/slots/{xid}/book", headers=auth(adult["xid"]))
        client.delete(f"/api/v1/speaking/slots/{xid}/book", headers=auth(adult["xid"]))
        again = client.post(f"/api/v1/speaking/slots/{xid}/book",
                            headers=auth(adult["xid"]))
        assert again.json()["status"] == "booked"

    def test_check_in_is_closed_until_ten_minutes_before(self, client, teacher,
                                                         adult):
        """"Only PRESENT users get paired." Opening check-in early would let
        someone who booked three days ago and forgot into the pool."""
        xid = _slot(client, teacher, starts_in=dt.timedelta(hours=3)).json()["xid"]
        client.post(f"/api/v1/speaking/slots/{xid}/book", headers=auth(adult["xid"]))
        refused = client.post(f"/api/v1/speaking/slots/{xid}/check-in",
                              headers=auth(adult["xid"]))
        assert refused.status_code == 425
        assert refused.json()["code"] == "checkin_not_open"
        assert refused.json()["opens_at"] and refused.json()["server_now"]

    def test_check_in_opens_in_the_window(self, client, teacher, adult):
        xid = _slot(client, teacher,
                    starts_in=dt.timedelta(minutes=5)).json()["xid"]
        client.post(f"/api/v1/speaking/slots/{xid}/book", headers=auth(adult["xid"]))
        response = client.post(f"/api/v1/speaking/slots/{xid}/check-in",
                               headers=auth(adult["xid"]))
        assert response.status_code == 200
        assert response.json()["status"] == "checked_in"

    def test_checking_in_twice_keeps_the_first_time(self, client, teacher, adult):
        xid = _slot(client, teacher, starts_in=dt.timedelta(minutes=5)).json()["xid"]
        client.post(f"/api/v1/speaking/slots/{xid}/book", headers=auth(adult["xid"]))
        first = client.post(f"/api/v1/speaking/slots/{xid}/check-in",
                            headers=auth(adult["xid"])).json()["checked_in_at"]
        second = client.post(f"/api/v1/speaking/slots/{xid}/check-in",
                             headers=auth(adult["xid"])).json()["checked_in_at"]
        assert first == second

    def test_checking_in_without_a_booking_is_a_404(self, client, teacher, adult):
        xid = _slot(client, teacher, starts_in=dt.timedelta(minutes=5)).json()["xid"]
        assert client.post(f"/api/v1/speaking/slots/{xid}/check-in",
                           headers=auth(adult["xid"])).status_code == 404

    def test_a_cancelled_booking_cannot_check_in(self, client, teacher, adult):
        xid = _slot(client, teacher, starts_in=dt.timedelta(minutes=5)).json()["xid"]
        client.post(f"/api/v1/speaking/slots/{xid}/book", headers=auth(adult["xid"]))
        client.delete(f"/api/v1/speaking/slots/{xid}/book", headers=auth(adult["xid"]))
        assert client.post(f"/api/v1/speaking/slots/{xid}/check-in",
                           headers=auth(adult["xid"])).status_code == 404

    def test_booking_a_slot_that_does_not_exist_is_a_404(self, client, adult):
        assert client.post(f"/api/v1/speaking/slots/{uuid.uuid4()}/book",
                           headers=auth(adult["xid"])).status_code == 404

    def test_the_list_can_be_filtered_by_audience(self, client, db, seed, teacher,
                                                  adult):
        _slot(client, teacher, audience="public")
        db.execute(text("""
            INSERT INTO speaking_slots (org_id, starts_at, capacity, status, audience,
                                        age_band, created_by)
            VALUES (:o, now() + interval '2 hours', 20, 'booking', 'org', 'adult', :u)
        """).bindparams(o=seed["org"].id, u=teacher["id"]))
        db.flush()
        both = client.get("/api/v1/speaking/slots", headers=auth(adult["xid"])).json()
        assert {s["audience"] for s in both} == {"public", "org"}
        only = client.get("/api/v1/speaking/slots?audience=org",
                          headers=auth(adult["xid"])).json()
        assert [s["audience"] for s in only] == ["org"]


class TestTheBookingStateMachine:
    """`my_booking.status` is what the client reads to decide what to show. The
    interesting one is `matched`: it is how a student learns the call is ready."""

    def _booked(self, client, teacher, adult):
        xid = _slot(client, teacher, starts_in=dt.timedelta(minutes=5)).json()["xid"]
        client.post(f"/api/v1/speaking/slots/{xid}/book", headers=auth(adult["xid"]))
        return xid

    def test_a_matched_booking_reports_matched_and_carries_the_pair(
            self, client, db, teacher, adult, child):
        self._booked(client, teacher, adult)
        pair_id, pair_xid = db.execute(text("""
            INSERT INTO speaking_pairs (user_a_id, user_b_id, origin, age_band)
            VALUES (:a, :b, 'slot_batch', 'adult') RETURNING id, xid
        """).bindparams(a=adult["id"], b=child["id"])).one()
        db.execute(text("""
            UPDATE speaking_slot_bookings SET pair_id = :p WHERE user_id = :u
        """).bindparams(p=pair_id, u=adult["id"]))
        db.flush()
        mine = client.get("/api/v1/speaking/slots",
                          headers=auth(adult["xid"])).json()[0]["my_booking"]
        assert mine["status"] == "matched"
        assert mine["pair_xid"] == str(pair_xid)

    def test_a_no_show_reports_no_show(self, client, db, teacher, adult):
        """Set by the matcher when a booked user never checked in. It has to be
        distinguishable from `booked`, or the no-show rate is unmeasurable."""
        self._booked(client, teacher, adult)
        db.execute(text("UPDATE speaking_slot_bookings SET no_show = true "
                        "WHERE user_id = :u").bindparams(u=adult["id"]))
        db.flush()
        listed = client.get("/api/v1/speaking/slots", headers=auth(adult["xid"])).json()
        assert listed[0]["my_booking"]["status"] == "no_show"

    def test_cancelled_outranks_no_show(self, client, db, teacher, adult):
        """Someone who cancelled did not fail to turn up."""
        xid = self._booked(client, teacher, adult)
        client.delete(f"/api/v1/speaking/slots/{xid}/book", headers=auth(adult["xid"]))
        db.execute(text("UPDATE speaking_slot_bookings SET no_show = true "
                        "WHERE user_id = :u").bindparams(u=adult["id"]))
        db.flush()
        listed = client.get("/api/v1/speaking/slots", headers=auth(adult["xid"])).json()
        assert listed[0]["my_booking"]["status"] == "cancelled"


# ── the safety report ────────────────────────────────────────────────

@pytest.fixture
def pair(db, seed, adult, child):
    """A finished pair between the two seeded speakers.

    Written directly: the matcher has its own tests, and what is under test here
    is the report path over an existing pair.
    """
    row = db.execute(text("""
        INSERT INTO speaking_pairs (user_a_id, user_b_id, origin, age_band,
                                    matched_at)
        VALUES (:a, :b, 'slot_batch', 'adult', now()) RETURNING id, xid
    """).bindparams(a=adult["id"], b=child["id"])).mappings().one()
    db.flush()
    return row


def _report(client, pair, reporter, *, category="harassment", audio=None,
            content_type="audio/webm"):
    files = None
    if audio is not None:
        files = {"audio_buffer": ("buffer.webm", io.BytesIO(audio), content_type)}
    return client.post(f"/api/v1/speaking/pairs/{pair['xid']}/report",
                       headers=auth(reporter["xid"]),
                       data={"category": category, "description": "He was abusive."},
                       files=files)


def _evidence(db, pair):
    """The media row reached by following the pair's own pointer.

    Deliberately joined through `evidence_media_id` rather than selected from
    `media_assets` directly: the pointer is the thing the moderator follows, and
    the defect was that following it led nowhere.
    """
    return db.execute(text("""
        SELECT m.* FROM speaking_pairs p
        JOIN media_assets m ON m.id = p.evidence_media_id
        WHERE p.id = :p
    """).bindparams(p=pair["id"])).mappings().first()


class TestTheEvidenceBufferIsActuallyStored:
    """The defect. The upload was read, hashed, its length recorded — and
    dropped. `storage().put` was never called, so the moderator opening a report
    marked `has_evidence: true` found a row pointing at nothing.

    Every test here reads the object back out of storage. Asserting on the
    `media_assets` row alone is what let the bug survive: the row was perfectly
    correct, and the object under it did not exist.
    """

    def test_the_bytes_reach_storage(self, client, db, store, pair, adult):
        response = _report(client, pair, adult, audio=b"OggS-pretend-audio" * 64)
        assert response.status_code == 201
        assert response.json()["has_evidence"] is True

        row = _evidence(db, pair)
        assert row["bucket"] == store.bucket, "recorded in a bucket that is not ours"
        assert store.stat(row["storage_key"]) is not None, "the report kept no evidence"
        assert b"".join(store.get(row["storage_key"])) == b"OggS-pretend-audio" * 64

    def test_the_recorded_size_and_checksum_match_the_object(self, client, db,
                                                             store, pair, adult):
        """Both derived from the stored object, not from the payload the test
        sent. Comparing the row against the request is how you write a test that
        passes while the bytes are on the floor."""
        import hashlib

        raw = b"OggS-pretend-audio" * 64
        _report(client, pair, adult, audio=raw)
        row = _evidence(db, pair)
        stored = b"".join(store.get(row["storage_key"]))
        assert row["bytes"] == len(stored) == len(raw)
        assert row["checksum_sha256"] == hashlib.sha256(stored).hexdigest()
        assert row["content_type"] == "audio/webm"

    def test_it_arrives_quarantined(self, client, db, store, pair, adult):
        """Unreviewed audio from a stranger, in a bucket a moderator reads and
        nobody else does."""
        _report(client, pair, adult, audio=b"x" * 128)
        assert db.scalar(text("SELECT status FROM media_assets")) == "quarantined"

    def test_the_pair_points_at_something_that_exists(self, client, db, store,
                                                      pair, adult):
        """The pointer was set in the broken version too. What it pointed at is
        the assertion that matters."""
        _report(client, pair, adult, audio=b"x" * 128)
        row = _evidence(db, pair)
        assert row is not None, "the pair carries no evidence pointer"
        assert store.stat(row["storage_key"]) is not None, "it points at nothing"

    def test_an_oversized_buffer_is_refused(self, client, store, pair, adult):
        """`file.read()` was unbounded, so the endpoint would take whatever an
        authenticated client sent — straight into memory on a 4 vCPU box."""
        from app.api.routers.speaking import MAX_EVIDENCE_BYTES

        response = _report(client, pair, adult, audio=b"x" * (MAX_EVIDENCE_BYTES + 1))
        assert response.status_code == 409
        assert response.json()["code"] == "evidence_too_large"

    def test_a_non_audio_upload_is_refused(self, client, store, pair, adult):
        response = _report(client, pair, adult, audio=b"%PDF-1.7",
                           content_type="application/pdf")
        assert response.json()["code"] == "evidence_format_unsupported"

    def test_an_empty_buffer_is_refused(self, client, store, pair, adult):
        assert _report(client, pair, adult,
                       audio=b"").json()["code"] == "evidence_empty"

    def test_a_content_type_with_parameters_is_accepted(self, client, store, pair,
                                                        adult):
        """Browsers send `audio/webm;codecs=opus`."""
        assert _report(client, pair, adult, audio=b"x" * 64,
                       content_type="audio/webm;codecs=opus").status_code == 201


class TestReportsWithoutEvidence:
    def test_a_report_can_be_filed_with_no_audio_at_all(self, client, pair, adult):
        """The buffer is optional. A report nobody can substantiate is still a
        report, and refusing it would teach students not to file."""
        response = _report(client, pair, adult)
        assert response.status_code == 201
        assert response.json()["has_evidence"] is False

    def test_an_unknown_category_is_refused(self, client, pair, adult):
        assert _report(client, pair, adult,
                       category="vibes").json()["code"] == "unknown_category"

    def test_a_report_about_a_minor_is_critical(self, client, pair, adult):
        """"`involves_minor` is set by the SYSTEM from the participants' ages,
        never by the reporter." The pair here is banded `adult`, and the child in
        it is found from `users.adult_at` anyway."""
        response = _report(client, pair, adult)
        assert response.json()["involves_minor"] is True
        assert response.json()["priority"] == "critical"

    def test_a_report_between_two_adults_is_high_not_critical(self, client, db,
                                                              seed, adult):
        other = _user(db, "+998910000031", "Sardor", dob=ADULT_DOB,
                      org_id=seed["org"].id)
        row = db.execute(text("""
            INSERT INTO speaking_pairs (user_a_id, user_b_id, origin, age_band,
                                        matched_at)
            VALUES (:a, :b, 'slot_batch', 'adult', now()) RETURNING id, xid
        """).bindparams(a=adult["id"], b=other["id"])).mappings().one()
        db.flush()
        response = _report(client, row, adult)
        assert response.json()["involves_minor"] is False
        assert response.json()["priority"] == "high"

    def test_grooming_is_critical_regardless(self, client, db, seed, adult):
        other = _user(db, "+998910000032", "Sardor", dob=ADULT_DOB,
                      org_id=seed["org"].id)
        row = db.execute(text("""
            INSERT INTO speaking_pairs (user_a_id, user_b_id, origin, age_band,
                                        matched_at)
            VALUES (:a, :b, 'slot_batch', 'adult', now()) RETURNING id, xid
        """).bindparams(a=adult["id"], b=other["id"])).mappings().one()
        db.flush()
        assert _report(client, row, adult,
                       category="grooming").json()["priority"] == "critical"

    def test_the_subject_is_the_other_party(self, client, db, pair, adult, child):
        _report(client, pair, adult)
        assert db.scalar(text("""
            SELECT subject_user_id FROM safety_reports
        """)) == child["id"]

    def test_filing_is_written_to_the_audit_log(self, client, db, pair, adult):
        """"Immutable audit log for safety events." A moderation decision is
        reviewed months later; the filing has to be in the record."""
        _report(client, pair, adult)
        row = db.execute(text("""
            SELECT action, after FROM audit_log WHERE action = 'safety.report_filed'
        """)).mappings().one()
        assert row["after"]["category"] == "harassment"
        assert row["after"]["involves_minor"] is True

    def test_someone_outside_the_pair_cannot_report_it(self, client, db, seed,
                                                       pair):
        outsider = _user(db, "+998910000033", "Nosy", dob=ADULT_DOB,
                         org_id=seed["org"].id)
        assert _report(client, pair, outsider).status_code == 404

    def test_an_unknown_pair_is_a_404(self, client, adult):
        assert client.post(f"/api/v1/speaking/pairs/{uuid.uuid4()}/report",
                           headers=auth(adult["xid"]),
                           data={"category": "harassment"}).status_code == 404


class TestEndingAPair:
    def test_ending_records_the_reason_and_relay_flag(self, client, db, pair,
                                                      adult):
        """"`turn_relayed` ... is the number the entire TURN bandwidth estimate
        depends on, and the one that decides whether this feature costs $5 a month
        or $80." """
        response = client.post(f"/api/v1/speaking/pairs/{pair['xid']}/end",
                               headers=auth(adult["xid"]),
                               json={"reason": "completed", "turn_relayed": True,
                                     "connection_quality": {"rtt_ms": 180}})
        assert response.status_code == 200
        row = db.execute(text("""
            SELECT end_reason, turn_relayed, connection_quality FROM speaking_pairs
        """)).mappings().one()
        assert row["end_reason"] == "completed"
        assert row["turn_relayed"] is True
        assert row["connection_quality"] == {"rtt_ms": 180}

    def test_ending_twice_keeps_the_first_time(self, client, db, pair, adult):
        first = client.post(f"/api/v1/speaking/pairs/{pair['xid']}/end",
                            headers=auth(adult["xid"]),
                            json={"reason": "completed"}).json()["ended_at"]
        second = client.post(f"/api/v1/speaking/pairs/{pair['xid']}/end",
                             headers=auth(adult["xid"]),
                             json={"reason": "left"}).json()["ended_at"]
        assert first == second

    def test_either_party_may_end_it(self, client, pair, child):
        assert client.post(f"/api/v1/speaking/pairs/{pair['xid']}/end",
                           headers=auth(child["xid"]),
                           json={"reason": "left"}).status_code == 200

    def test_someone_outside_the_pair_cannot(self, client, db, seed, pair):
        outsider = _user(db, "+998910000034", "Nosy", dob=ADULT_DOB,
                         org_id=seed["org"].id)
        assert client.post(f"/api/v1/speaking/pairs/{pair['xid']}/end",
                           headers=auth(outsider["xid"]),
                           json={"reason": "completed"}).status_code == 404
