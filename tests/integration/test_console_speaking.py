"""The three speaking-adjacent create screens, driven the way they call.

`Slots.tsx`, `CueCards.tsx` and `BandMaps.tsx` are the console's first callers of
`/speaking/slots`, `/cue-card-sets` and `POST /band-maps`. Each screen refuses
things before it sends them, and a client-side refusal is only worth writing if
it matches what the server does — or, in the cases below, only worth writing
*because* the server does not refuse at all.

Three groups, and the middle one is the reason this file exists:

  * what the server enforces, so the screen can rely on it;
  * what the server accepts and should not, so the screen is the only guard;
  * what `GET /speaking/slots` returns for a teacher, which is not what a
    console needs and is reported as a gap rather than papered over.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token


@pytest.fixture
def client(engine, db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


def soon(days: int = 1) -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(days=days)).isoformat()


@pytest.fixture
def teacher(seed):
    """`seed["author"]` holds `role="teacher"` in the seeded organization."""
    return auth(seed["author"].xid)


@pytest.fixture
def centre_admin(db, seed):
    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name="Rustam",
                date_of_birth=dt.date(1990, 1, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id,
                         role="centre_admin", status="active"))
    db.flush()
    return auth(user.xid)


@pytest.fixture
def platform_admin(db):
    from app.modules.identity.models import PlatformRoleGrant, User

    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name="Sardor",
                date_of_birth=dt.date(1985, 1, 1))
    db.add(user)
    db.flush()
    db.add(PlatformRoleGrant(user_id=user.id, role="platform_admin"))
    db.flush()
    return auth(user.xid)


@pytest.fixture
def cohort(db, seed):
    return db.execute(text("""
        INSERT INTO cohorts (org_id, name) VALUES (:o, 'Evening group')
        RETURNING id, xid
    """).bindparams(o=seed["org"].id)).mappings().one()


# The complete platform READING curve from migration 0026 — what a real band map
# looks like, and the fixture the screen's validation is calibrated against.
READING = [
    (0, 3, 2.0), (4, 5, 2.5), (6, 7, 3.0), (8, 9, 3.5), (10, 12, 4.0),
    (13, 14, 4.5), (15, 18, 5.0), (19, 22, 5.5), (23, 26, 6.0), (27, 29, 6.5),
    (30, 32, 7.0), (33, 34, 7.5), (35, 36, 8.0), (37, 38, 8.5), (39, 40, 9.0),
]
FULL_MAPPING = [{"raw_min": lo, "raw_max": hi, "band": band}
                for lo, hi, band in READING]


class TestTheSlotFormSendsWhatTheServerAccepts:
    def test_the_screens_own_sequence(self, client, teacher, cohort):
        """Cue cards first, then the slot that attaches them — the order the two
        screens are used in, and the reason the version xid is surfaced."""
        created = _ok(client.post("/api/v1/cue-card-sets", headers=teacher, json={
            "title": "Describe a journey", "tags": ["travel"],
            "body": {"part1": ["Do you work or study?"],
                     "part2": {"topic": "A journey you remember",
                               "bullets": ["where", "who with"]},
                     "part3": ["Why do people travel?"]}}))
        version = created["current_version_xid"]
        assert version, "the version xid is the only handle a slot can attach"

        slot = _ok(client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "cohort", "cohort_xid": str(cohort["xid"]),
            "age_band": "mixed_supervised", "band_min": 5.0, "band_max": 6.5,
            "cue_card_set_version_xid": version}), 201)
        assert slot["cue_card_set_version_xid"] == version
        assert slot["age_band"] == "mixed_supervised"
        assert (slot["band_min"], slot["band_max"]) == (5.0, 6.5)

    def test_a_mixed_age_session_must_be_a_class_session(self, client, teacher):
        """`slotProblems` refuses this before sending. The server's answer is the
        one the copy is written from."""
        refused = client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "public", "age_band": "mixed_supervised"})
        assert refused.status_code == 409
        assert refused.json()["code"] == "mixed_requires_cohort"

    def test_an_inverted_band_range_is_refused(self, client, teacher):
        refused = client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "org", "age_band": "adult",
            "band_min": 7.0, "band_max": 5.0})
        assert refused.status_code == 409
        assert refused.json()["code"] == "invalid_band_range"

    def test_a_band_off_the_scale_is_refused(self, client, teacher):
        refused = client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "org", "age_band": "adult", "band_min": 12})
        assert refused.status_code == 422

    def test_only_the_three_age_bands_exist(self, client, teacher):
        """The radio group offers exactly what the handler's pattern allows.
        A fourth value is a 422, which is why they are read and not guessed."""
        for band in ("minor", "adult"):
            _ok(client.post("/api/v1/speaking/slots", headers=teacher, json={
                "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
                "audience": "org", "age_band": band}), 201)
        refused = client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "org", "age_band": "supervised"})
        assert refused.status_code == 422

    def test_a_student_cannot_open_a_slot(self, client, seed):
        refused = client.post("/api/v1/speaking/slots",
                              headers=auth(seed["student"].xid),
                              json={"starts_at": soon(), "duration_minutes": 15,
                                    "capacity": 8, "audience": "public",
                                    "age_band": "adult"})
        assert refused.status_code == 403
        assert refused.json()["code"] == "not_a_teacher"

    def test_a_cue_card_set_from_another_centre_cannot_be_attached(
            self, client, db, teacher):
        """The slot handler scopes the version to platform-global or the actor's
        own orgs, so the picker offering only the listing's sets is the same rule
        stated twice rather than a guess."""
        rival = db.scalar(text("""
            INSERT INTO organizations (name, slug, status)
            VALUES ('Rival Centre', :s, 'active') RETURNING id
        """).bindparams(s=f"rival-{_uuid.uuid4().hex[:6]}"))
        owner = db.scalar(text("SELECT id FROM users LIMIT 1"))
        set_id = db.scalar(text("""
            INSERT INTO cue_card_sets (org_id, owner_user_id, title)
            VALUES (:o, :u, 'Rival prompts') RETURNING id
        """).bindparams(o=rival, u=owner))
        theirs = db.scalar(text("""
            INSERT INTO cue_card_set_versions (set_id, version_no, body, created_by)
            VALUES (:s, 1, '{"part1":["hi"]}'::jsonb, :u) RETURNING xid
        """).bindparams(s=set_id, u=owner))
        db.flush()

        refused = client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "org", "age_band": "adult",
            "cue_card_set_version_xid": str(theirs)})
        assert refused.status_code == 404


class TestWhatTheBookableListReturnsForAMemberOfStaff:
    """`GET /speaking/slots` is the caller's BOOKABLE list, not a listing of what
    they created. Everything here is why `Slots.tsx` says so instead of
    presenting the table as "your slots"."""

    def test_a_teacher_does_not_see_the_class_session_they_opened(
            self, client, db, teacher, seed, cohort):
        opened = _ok(client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "cohort", "cohort_xid": str(cohort["xid"]),
            "age_band": "mixed_supervised"}), 201)

        assert _ok(client.get("/api/v1/speaking/slots", headers=teacher)) == [], (
            "the cohort branch of the listing is `cohort_id IN (SELECT ... FROM "
            "cohort_members WHERE user_id = :u)`, and a cohort roster holds "
            "students — so the console has no listing of the slots it creates")

        # And it is the roster that decides, not authorship.
        db.execute(text("INSERT INTO cohort_members (cohort_id, user_id) "
                        "VALUES (:c, :u)")
                   .bindparams(c=cohort["id"], u=seed["author"].id))
        db.flush()
        listed = _ok(client.get("/api/v1/speaking/slots", headers=teacher))
        assert [s["xid"] for s in listed] == [opened["xid"]]

    def test_an_adult_never_sees_a_minors_session_even_one_they_opened(
            self, client, teacher):
        """The age filter is on the CALLER's own age band. It is the child-safety
        rule and it applies to staff accounts, which is why the screen explains
        the absence rather than treating it as an error."""
        _ok(client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "org", "age_band": "minor"}), 201)
        assert _ok(client.get("/api/v1/speaking/slots", headers=teacher)) == []

    def test_an_org_session_is_listed_for_the_centre(self, client, teacher):
        opened = _ok(client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "org", "age_band": "adult"}), 201)
        listed = _ok(client.get("/api/v1/speaking/slots", headers=teacher))
        assert [s["xid"] for s in listed] == [opened["xid"]]

    def test_the_audience_filter_the_dropdown_sends(self, client, teacher):
        _ok(client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "org", "age_band": "adult"}), 201)
        _ok(client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "public", "age_band": "adult"}), 201)
        assert len(_ok(client.get("/api/v1/speaking/slots?audience=org",
                                  headers=teacher))) == 1
        assert len(_ok(client.get("/api/v1/speaking/slots", headers=teacher))) == 2

    def test_a_session_in_the_past_is_created_and_listed_for_nobody(
            self, client, teacher):
        """Why `slotProblems` refuses a past start time. The server does not."""
        past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
        _ok(client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": past, "duration_minutes": 15, "capacity": 8,
            "audience": "org", "age_band": "adult"}), 201)
        assert _ok(client.get("/api/v1/speaking/slots", headers=teacher)) == []

    def test_the_from_parameter_the_contract_declares_does_nothing(
            self, client, teacher):
        """`openapi.yaml` declares `from`; the handler's parameter is `from_`, so
        the generated client's `from` is accepted and ignored. The screen offers
        no date control rather than one that silently does nothing."""
        past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
        _ok(client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": past, "duration_minutes": 15, "capacity": 8,
            "audience": "org", "age_band": "adult"}), 201)
        window = (dt.datetime.now(dt.UTC) - dt.timedelta(days=7)).isoformat()
        assert _ok(client.get("/api/v1/speaking/slots", headers=teacher,
                              params={"from": window})) == []
        assert len(_ok(client.get("/api/v1/speaking/slots", headers=teacher,
                                  params={"from_": window}))) == 1

    def test_an_unmeasured_student_is_excluded_by_no_band_range(
            self, client, teacher, seed):
        """The fact the range copy is written from. A student who has sat no
        scored mock has no measured band, so a narrow range does not hide the
        session from them — which for a new intake is nearly everyone."""
        _ok(client.post("/api/v1/speaking/slots", headers=teacher, json={
            "starts_at": soon(), "duration_minutes": 15, "capacity": 8,
            "audience": "org", "age_band": "adult",
            "band_min": 6.5, "band_max": 7.5}), 201)
        listed = _ok(client.get("/api/v1/speaking/slots",
                                headers=auth(seed["student"].xid)))
        assert len(listed) == 1


class TestCueCardSets:
    def test_the_library_lists_what_the_form_created(self, client, teacher):
        made = _ok(client.post("/api/v1/cue-card-sets", headers=teacher, json={
            "title": "Small talk", "tags": ["part 1"],
            "body": {"part1": ["Do you work or study?"]}}))
        listed = _ok(client.get("/api/v1/cue-card-sets", headers=teacher))
        assert [(s["title"], s["current_version_xid"]) for s in listed] == [
            ("Small talk", made["current_version_xid"])]
        assert listed[0]["visibility"] == "org_private", (
            "a centre's prompts must not reach a competitor by default")

    def test_an_untitled_set_is_refused(self, client, teacher):
        refused = client.post("/api/v1/cue-card-sets", headers=teacher,
                              json={"title": "", "body": {"part1": ["a"]}})
        assert refused.status_code == 422

    def test_a_set_with_no_prompts_at_all_is_accepted(self, client, teacher):
        """Why `promptProblems` refuses an empty body. `CueCardSetCreate` makes
        `body` required and then defaults all three parts to empty, so `{}` is a
        valid body — and the result is a session that opens with nothing to talk
        about."""
        made = _ok(client.post("/api/v1/cue-card-sets", headers=teacher,
                               json={"title": "Empty", "body": {}}), 201)
        assert made["current_version_xid"]

    def test_a_part_two_with_no_topic_is_refused(self, client, teacher):
        """Why the form omits `part2` entirely when the topic is blank rather
        than sending an empty object."""
        refused = client.post("/api/v1/cue-card-sets", headers=teacher, json={
            "title": "Bullets only", "body": {"part2": {"bullets": ["where"]}}})
        assert refused.status_code == 422

    def test_a_student_cannot_author_prompts(self, client, seed):
        refused = client.post("/api/v1/cue-card-sets",
                              headers=auth(seed["student"].xid),
                              json={"title": "x", "body": {"part1": ["a"]}})
        assert refused.status_code == 403

    def test_another_centres_set_is_not_in_the_picker(self, client, db, teacher):
        rival = db.scalar(text("""
            INSERT INTO organizations (name, slug, status)
            VALUES ('Rival Centre', :s, 'active') RETURNING id
        """).bindparams(s=f"rival-{_uuid.uuid4().hex[:6]}"))
        owner = db.scalar(text("SELECT id FROM users LIMIT 1"))
        db.execute(text("""
            INSERT INTO cue_card_sets (org_id, owner_user_id, title)
            VALUES (:o, :u, 'Rival prompts')
        """).bindparams(o=rival, u=owner))
        db.flush()
        titles = [s["title"] for s in
                  _ok(client.get("/api/v1/cue-card-sets", headers=teacher))]
        assert "Rival prompts" not in titles

    def test_a_platform_admins_set_is_created_and_then_unlistable(
            self, client, platform_admin):
        """A real defect, not a screen decision. With no organization the set is
        stored `org_id = NULL, visibility = 'org_private'`, and the listing's
        three branches — platform-global, in my orgs, my own author-private —
        match none of them. The set exists and no listing will ever return it."""
        made = _ok(client.post("/api/v1/cue-card-sets", headers=platform_admin,
                               json={"title": "Platform prompts",
                                     "body": {"part1": ["Hello?"]}}), 201)
        assert made["current_version_xid"]
        assert _ok(client.get("/api/v1/cue-card-sets", headers=platform_admin)) == []


class TestBandMapsAreGuardedByTheServer:
    def test_a_complete_curve_is_created_and_listed(self, client, centre_admin):
        made = _ok(client.post("/api/v1/band-maps", headers=centre_admin, json={
            "name": "Tashkent Prep reading", "skill": "reading",
            "variant": "academic", "max_raw": 40, "mapping": FULL_MAPPING}), 201)
        assert made["current_version"]["max_raw"] == 40
        assert len(made["current_version"]["mapping"]) == 15
        listed = _ok(client.get("/api/v1/band-maps", headers=centre_admin))
        ours = [m for m in listed if m["name"] == "Tashkent Prep reading"]
        assert ours and ours[0]["is_platform_default"] is False

    def test_a_teacher_may_not_create_one(self, client, teacher):
        """`MANAGE_BAND_MAP` is centre-admin and platform-admin only, which is
        narrower than every other create on the authoring surface."""
        refused = client.post("/api/v1/band-maps", headers=teacher, json={
            "name": "By a teacher", "skill": "reading", "max_raw": 40,
            "mapping": FULL_MAPPING})
        assert refused.status_code == 403
        assert refused.json()["code"] == "manage_band_map_not_permitted"

    def test_a_gapped_curve_is_refused(self, client, centre_admin):
        """These six pinned the absence of validation and now assert it.

        A table covering 10-20 of a 40-mark paper used to be stored, and every
        student scoring 0-9 or 21-40 got a raw count and no band — silently,
        because `BandMap.band_for` answers None for an uncovered mark. The
        screen's `tableProblems` still checks, because a refusal a teacher sees
        while typing beats one they see on submit.
        """
        refused = client.post("/api/v1/band-maps", headers=centre_admin, json={
            "name": "Gapped", "skill": "reading", "max_raw": 40,
            "mapping": [{"raw_min": 10, "raw_max": 20, "band": 5.0}]})
        assert refused.status_code == 422, refused.text
        assert "BAND_MAP_GAP" in refused.text

    def test_an_empty_curve_is_refused(self, client, centre_admin):
        refused = client.post("/api/v1/band-maps", headers=centre_admin, json={
            "name": "Empty", "skill": "reading", "max_raw": 40, "mapping": []})
        assert refused.status_code == 422, refused.text

    def test_rows_with_the_wrong_keys_are_refused(self, client, centre_admin):
        """`exam.session` builds the scorer's table with `int(row["raw_min"])`,
        so a row like this was a KeyError INSIDE SCORING — the student's
        submission failed rather than their band being absent."""
        refused = client.post("/api/v1/band-maps", headers=centre_admin, json={
            "name": "Wrong keys", "skill": "reading", "max_raw": 40,
            "mapping": [{"lo": 0, "hi": 40, "b": 5}]})
        assert refused.status_code == 422, refused.text
        assert "BAND_MAP_ROW_MALFORMED" in refused.text

    def test_a_negative_maximum_is_refused(self, client, centre_admin):
        refused = client.post("/api/v1/band-maps", headers=centre_admin, json={
            "name": "Negative", "skill": "reading", "max_raw": -5,
            "mapping": FULL_MAPPING})
        assert refused.status_code == 422, refused.text

    def test_overlapping_rows_are_refused(self, client, centre_admin):
        """Not merely untidy: `BandMap.band_for` returns the first matching row,
        so the band a cohort is told was decided by row order."""
        refused = client.post("/api/v1/band-maps", headers=centre_admin, json={
            "name": "Overlap", "skill": "reading", "max_raw": 4,
            "mapping": [{"raw_min": 0, "raw_max": 4, "band": 9.0},
                        {"raw_min": 0, "raw_max": 4, "band": 2.0}]})
        assert refused.status_code == 422, refused.text
        assert "BAND_MAP_OVERLAP" in refused.text

    def test_a_skill_outside_the_column_is_a_500_not_a_message(
            self, client, centre_admin):
        """`skill` is a DB CHECK and the request model does not constrain it, so
        a value outside the two is an internal error with nothing to act on.
        The screen offers a fixed pair for that reason."""
        refused = client.post("/api/v1/band-maps", headers=centre_admin, json={
            "name": "Speaking curve", "skill": "speaking", "max_raw": 40,
            "mapping": FULL_MAPPING})
        assert refused.status_code == 500

    def test_a_map_from_an_account_with_no_centre_becomes_a_platform_default(
            self, client, db, platform_admin, centre_admin):
        """`org_id` comes from `actor.org_ids[0]`, and no organization means
        NULL — which is what a platform default IS. That was reachable by any
        account belonging to no centre, and the response said
        `is_platform_default: false` about it, which was untrue as well as
        unhelpful. Platform admin only now, and the field is honest."""
        made = _ok(client.post("/api/v1/band-maps", headers=platform_admin, json={
            "name": "Platform curve", "skill": "listening", "max_raw": 40,
            "mapping": FULL_MAPPING}), 201)
        assert made["is_platform_default"] is True

        assert db.scalar(text("SELECT org_id FROM band_maps WHERE name = :n")
                         .bindparams(n="Platform curve")) is None
        listed = _ok(client.get("/api/v1/band-maps", headers=platform_admin))
        theirs = [m for m in listed if m["name"] == "Platform curve"]
        assert theirs and theirs[0]["is_platform_default"] is True

        # And an unrelated centre is marking against it from that moment.
        seen = [m["name"] for m in
                _ok(client.get("/api/v1/band-maps", headers=centre_admin))]
        assert "Platform curve" in seen
