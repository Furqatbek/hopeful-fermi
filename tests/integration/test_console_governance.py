"""The three governance screens, driven the way the console drives them.

Sharing, Exposure and Takedowns are where the product's two contractual
promises live: that a centre's material never reaches a competitor, and that
copyright liability and evidence are handled. Each screen is a fixed sequence of
calls and this file is that sequence, in order, with the bodies the components
send.

Three things here are not happy paths and matter more than the ones that are:

  * **What the screens must NOT offer.** A teacher has no `Action.SHARE` and a
    centre admin cannot share publicly, so both controls are absent rather than
    present-and-refused. A control that can only fail teaches staff to distrust
    the screen, so the refusals are asserted where the UI decisions rest on them.

  * **What the screens must not CLAIM.** Two pieces of this subsystem are
    designed and not implemented — a `view` grant makes nothing visible, and the
    soft-hide on a takedown hides nothing — and the copy on both screens says so.
    `TestWhatTheseScreensMayNotPromise` is what keeps that copy honest: if either
    is ever wired up, those tests fail and the words have to change.

  * **What a takedown row can contain.** Filing is unauthenticated and validates
    almost nothing, so the queue can carry a subject type outside the vocabulary
    and a subject that does not resolve at all. Both render, because an
    unreadable queue is the failure the listing exists to prevent.
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


def _user(db, name: str):
    from app.modules.identity.models import User

    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name=name,
                family_name="T", date_of_birth=dt.date(1985, 1, 1), status="active")
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def centre_admin(db, seed):
    """`Action.SHARE` is `{CENTRE_ADMIN, PLATFORM_ADMIN}`. The seeded author is a
    teacher, so nothing in `seed` can create a grant."""
    from app.modules.identity.models import OrgMembership

    user = _user(db, "Gulnora")
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id,
                         role="centre_admin", status="active"))
    db.flush()
    return user


@pytest.fixture
def rival(db, seed):
    """A competitor centre and its admin: the party the sharing rules exist for."""
    from app.modules.identity.models import Organization, OrgMembership

    org = Organization(name="Rival Prep Centre", slug=f"rival-{_uuid.uuid4().hex[:6]}",
                       status="active")
    db.add(org)
    db.flush()
    admin = _user(db, "Rustam")
    db.add(OrgMembership(org_id=org.id, user_id=admin.id, role="centre_admin",
                         status="active"))
    db.flush()
    return {"org": org, "admin": admin}


@pytest.fixture
def platform_admin(db):
    """Deliberately not the seeded author. Granting the platform role to somebody
    already in the centre would make every "a centre admin cannot" assertion in
    this file pass for the wrong reason."""
    from app.modules.identity.models import PlatformRoleGrant

    user = _user(db, "Platform")
    db.add(PlatformRoleGrant(user_id=user.id, role="platform_admin",
                             granted_by=user.id))
    db.flush()
    return user


def _question_xid(db, seed, index: int = 0) -> str:
    return str(db.scalar(text("SELECT xid FROM questions WHERE id = :i")
                         .bindparams(i=seed["question_versions"][index].question_id)))


def _passage_xid(db, seed) -> str:
    """The PASSAGE's xid, not the passage version's.

    `_SUBJECT_TABLES` maps `passage` to `passages`, so a grant or a takedown is
    filed against the asset and never against a version of it. The distinction is
    invisible in a failure — filing with a version xid is accepted and resolves
    to nothing — which is why it is spelled out here rather than inlined.
    """
    return str(db.scalar(text("""
        SELECT p.xid FROM passages p
        JOIN passage_versions v ON v.passage_id = p.id
        WHERE v.id = :i
    """).bindparams(i=seed["passage_version"].id)))


def _file_takedown(client, *, subject_type="passage", subject_xid,
                   claimant="Cambridge University Press"):
    """Filed the way a rights holder files: no bearer token at all."""
    return _ok(client.post("/api/v1/takedowns", json={
        "claimant_name": claimant, "claimant_org": "CUP",
        "claimant_email": "rights@example.org",
        "rights_basis": "Copyright owner of Cambridge IELTS 17",
        "sworn_statement": True, "subject_type": subject_type,
        "subject_xid": str(subject_xid),
        "description": "This is Reading Passage 1 from Cambridge IELTS 17 Test 2.",
    }), 201)


# ── Sharing ──────────────────────────────────────────────────────────


def _share(client, seed, actor, grantee_xid, *, permission="view",
           grantee_kind="org", subject_type="test", subject_xid=None):
    """The exact body the create form sends: optional fields spread in only when
    filled, because `exactOptionalPropertyTypes` forbids sending `undefined`."""
    return client.post("/api/v1/content-grants", json={
        "subject_type": subject_type,
        "subject_xid": str(subject_xid or seed["test"].xid),
        "grantee_kind": grantee_kind,
        **({"grantee_xid": str(grantee_xid)} if grantee_xid else {}),
        "permission": permission,
        "note": "Pilot term, agreed with their director",
    }, headers=auth(actor.xid))


class TestTheSharingScreen:
    def test_the_listing_it_opens_on(self, client, seed, centre_admin, rival):
        """`GET /content-grants?direction=granted&limit=200`, and every column
        the table renders."""
        _ok(_share(client, seed, centre_admin, rival["org"].xid), 201)
        body = _ok(client.get("/api/v1/content-grants?direction=granted&limit=200",
                              headers=auth(centre_admin.xid)))
        row = body["items"][0]
        assert row["subject_title"] == seed["test"].title
        assert row["subject_type"] == "test"
        assert row["grantee_name"] == "Rival Prep Centre"
        assert row["grantee_kind"] == "org"
        assert row["permission"] == "view"
        assert row["note"] == "Pilot term, agreed with their director"
        # Rendered as "No end date" rather than omitted: a grant with no end is
        # the one worth noticing.
        assert row["expires_at"] is None
        assert row["granted_at"]

    def test_the_other_direction_is_a_different_question(
            self, client, seed, centre_admin, rival):
        """The toggle is not a filter over one list. The receiving centre sees
        the grant as received and never as granted, because it was not the party
        that let the material out."""
        _ok(_share(client, seed, centre_admin, rival["org"].xid), 201)

        received = _ok(client.get("/api/v1/content-grants?direction=received&limit=200",
                                  headers=auth(rival["admin"].xid)))
        assert [r["subject_title"] for r in received["items"]] == [seed["test"].title]

        granted = _ok(client.get("/api/v1/content-grants?direction=granted&limit=200",
                                 headers=auth(rival["admin"].xid)))
        assert granted["items"] == []

    def test_every_subject_picker_answers(self, client, seed, centre_admin):
        """One listing per subject type, and they are genuinely not one endpoint:
        five are paged, two are bare arrays, and the label column is `title`,
        `name` or `type_key` depending. `loadSubjects` writes each out for that
        reason, so each is called here."""
        headers = auth(centre_admin.xid)
        for path in ("/api/v1/tests?limit=100", "/api/v1/passages?limit=100",
                     "/api/v1/audio-tracks?limit=100",
                     "/api/v1/question-groups?limit=100",
                     "/api/v1/questions?limit=100"):
            assert isinstance(_ok(client.get(path, headers=headers))["items"], list), path
        for path in ("/api/v1/cue-card-sets", "/api/v1/band-maps"):
            assert isinstance(_ok(client.get(path, headers=headers)), list), path

    def test_creating_a_grant_returns_what_the_form_needs_next(
            self, client, seed, centre_admin, rival):
        created = _ok(_share(client, seed, centre_admin, rival["org"].xid,
                             permission="copy"), 201)
        assert created["permission"] == "copy"
        assert created["subject_xid"] == str(seed["test"].xid)

    def test_revoking_twice_is_not_two_successes(
            self, client, seed, centre_admin, rival):
        """The reason the revoke handler cannot be written as fire-and-forget.
        It used to answer 204 for any uuid at all, so a client could not tell a
        revoke from a typo — and the second press of a stale button is the
        commonest way to send one."""
        grant = _ok(_share(client, seed, centre_admin, rival["org"].xid), 201)
        path = f"/api/v1/content-grants/{grant['xid']}"
        assert client.delete(path, headers=auth(centre_admin.xid)).status_code == 204
        assert client.delete(path, headers=auth(centre_admin.xid)).status_code == 404
        assert _ok(client.get("/api/v1/content-grants?direction=granted&limit=200",
                              headers=auth(centre_admin.xid)))["items"] == []

    def test_a_teacher_is_told_no_rather_than_shown_a_form(
            self, client, seed, rival):
        """Why the create form is absent for a teacher instead of disabled."""
        refused = _share(client, seed, seed["author"], rival["org"].xid)
        assert refused.status_code == 403, refused.text

    def test_a_teacher_still_sees_what_the_centre_may_use(
            self, client, db, seed, rival):
        """The two directions do not carry the same authority, and the screen's
        two empty states say different things because of it. `granted` filters on
        `Action.SHARE`, which a teacher lacks; `received` filters on org
        membership, which they have. So a teacher reads "what may we use" and is
        told the other list is a centre admin's to see."""
        from app.modules.content.models import Test

        theirs = Test(org_id=rival["org"].id, owner_user_id=rival["admin"].id,
                      title="Rival Mock 1", kind="mock", skills=["reading"])
        db.add(theirs)
        db.flush()
        _ok(_share(client, seed, rival["admin"], seed["org"].xid,
                   subject_xid=theirs.xid), 201)

        teacher = auth(seed["author"].xid)
        assert [r["subject_title"] for r in _ok(client.get(
            "/api/v1/content-grants?direction=received&limit=200",
            headers=teacher))["items"]] == ["Rival Mock 1"]
        assert _ok(client.get("/api/v1/content-grants?direction=granted&limit=200",
                              headers=teacher))["items"] == []

    def test_public_is_refused_for_a_centre_admin(
            self, client, seed, centre_admin):
        """The single rule that is most of the copyright containment: content
        cannot become world-visible without a platform review. The option is
        absent from the picker rather than present, and this is the refusal that
        would otherwise arrive after the form was filled in."""
        refused = _share(client, seed, centre_admin, None, grantee_kind="public")
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "public_share_not_permitted"

    def test_public_is_the_platform_admins_to_give(
            self, client, seed, platform_admin):
        created = _ok(_share(client, seed, platform_admin, None,
                             grantee_kind="public"), 201)
        assert created["grantee_kind"] == "public"
        listed = _ok(client.get("/api/v1/content-grants?direction=granted&limit=200",
                                headers=auth(platform_admin.xid)))["items"][0]
        assert listed["grantee_name"] == "Everyone"

    def test_the_grantee_cannot_revoke_what_was_given_to_them(
            self, client, seed, centre_admin, rival):
        """Why Revoke is drawn only in the `granted` direction. The grantee is
        precisely the party holding the xid."""
        grant = _ok(_share(client, seed, centre_admin, rival["org"].xid), 201)
        refused = client.delete(f"/api/v1/content-grants/{grant['xid']}",
                                headers=auth(rival["admin"].xid))
        assert refused.status_code == 403, refused.text


# ── Exposure ─────────────────────────────────────────────────────────


class TestTheExposureScreen:
    def test_the_watch_list_call_sequence(self, client, seed):
        """The bank page, then one exposure request per item on it. There is no
        other way to build this list."""
        page = _ok(client.get("/api/v1/questions?limit=25&skill=reading",
                              headers=auth(seed["author"].xid)))
        assert len(page["items"]) == 3
        for question in page["items"]:
            report = _ok(client.get(
                f"/api/v1/questions/{question['xid']}/exposure",
                headers=auth(seed["author"].xid)))
            assert report["question_xid"] == question["xid"]
            assert report["recommendation"] in ("fresh", "watch", "retire")

    def test_the_bank_listing_cannot_answer_it(self, client, seed):
        """Why the screen fans out at all. `Question.burn_score` is in the
        contract and `question_dto` hardcodes `None`, so the one field that would
        make this a single request is a constant null on every row."""
        page = _ok(client.get("/api/v1/questions?limit=25",
                              headers=auth(seed["author"].xid)))
        assert page["items"], "a listing with no rows would prove nothing here"
        assert {q.get("burn_score") for q in page["items"]} == {None}

    def test_an_item_nobody_has_sat_reads_as_zero_and_fresh(self, client, db, seed):
        """Not a 404. An item with no exposure row has never been sat, and zero
        is the true answer to how often — an error would read as a broken screen
        on the commonest case in a new bank."""
        report = _ok(client.get(
            f"/api/v1/questions/{_question_xid(db, seed)}/exposure",
            headers=auth(seed["author"].xid)))
        assert (report["times_sat"], report["distinct_users"],
                report["distinct_orgs"]) == (0, 0, 0)
        assert report["burn_score"] == 0
        assert report["recommendation"] == "fresh"
        assert report["last_seen_at"] is None

    def test_a_circulated_item_is_reported_as_spent(self, client, db, seed):
        """The row the screen sorts to the top, and the banner it draws."""
        db.execute(text("""
            INSERT INTO item_exposure_stats (question_id, times_sat, distinct_users,
                distinct_orgs, first_seen_at, last_seen_at, burn_score)
            VALUES (:q, 900, 780, 6, now() - interval '90 days', now(), 0.985)
        """).bindparams(q=seed["question_versions"][0].question_id))
        db.flush()
        report = _ok(client.get(
            f"/api/v1/questions/{_question_xid(db, seed)}/exposure",
            headers=auth(seed["author"].xid)))
        assert report["times_sat"] == 900
        assert report["distinct_orgs"] == 6
        assert report["burn_score"] > 0.7
        assert report["recommendation"] == "retire"

    def test_a_student_may_not_read_exposure(self, client, db, seed):
        """Which items have been sat is a map of what to revise."""
        refused = client.get(
            f"/api/v1/questions/{_question_xid(db, seed)}/exposure",
            headers=auth(seed["student"].xid))
        assert refused.status_code in (403, 404), refused.text

    def test_the_bank_search_box_is_not_offered_because_it_does_nothing(
            self, client, seed):
        """`list_questions` declares `q` and never applies it. A filter box on
        this screen would look like it had narrowed the page and would have
        returned everything, which on a watch list is worse than no box."""
        everything = _ok(client.get("/api/v1/questions?limit=25",
                                    headers=auth(seed["author"].xid)))
        filtered = _ok(client.get("/api/v1/questions?limit=25&q=zzz-no-such-question",
                                  headers=auth(seed["author"].xid)))
        assert len(filtered["items"]) == len(everything["items"])


# ── Takedowns ────────────────────────────────────────────────────────


class TestTheTakedownScreen:
    def test_the_queue_it_opens_on(self, client, db, seed, platform_admin):
        """`GET /admin/takedowns?status=open&limit=200`, and every field the
        claim panel puts in front of a decision."""
        _file_takedown(client, subject_xid=_passage_xid(db, seed))
        row = _ok(client.get("/api/v1/admin/takedowns?status=open&limit=200",
                             headers=auth(platform_admin.xid)))["items"][0]
        assert row["claimant_name"] == "Cambridge University Press"
        assert row["claimant_org"] == "CUP"
        assert row["claimant_email"] == "rights@example.org"
        assert row["rights_basis"].startswith("Copyright owner")
        assert row["description"].startswith("This is Reading Passage 1")
        assert row["subject_type"] == "passage"
        assert row["subject_title"] == "Cartography"
        assert row["status"] == "received"
        assert row["xid"] and row["received_at"]

    def test_every_status_the_filter_offers(self, client, db, seed, platform_admin):
        """The select is not decoration: `open`, `all` and each single status
        take different branches in the handler, and one that 500s would be a
        dead option on the only screen that can read this table."""
        filed = _file_takedown(client, subject_xid=_passage_xid(db, seed))
        _ok(client.patch(f"/api/v1/admin/takedowns/{filed['xid']}",
                         json={"status": "upheld", "outcome_note": "Verbatim."},
                         headers=auth(platform_admin.xid)))
        headers = auth(platform_admin.xid)
        assert _ok(client.get("/api/v1/admin/takedowns?status=open&limit=200",
                              headers=headers))["items"] == []
        for status in ("all", "upheld"):
            body = _ok(client.get(
                f"/api/v1/admin/takedowns?status={status}&limit=200", headers=headers))
            assert [i["status"] for i in body["items"]] == ["upheld"], status
        for status in ("rejected", "counter_noticed", "withdrawn", "received",
                       "reviewing"):
            body = _ok(client.get(
                f"/api/v1/admin/takedowns?status={status}&limit=200", headers=headers))
            assert body["items"] == [], status

    def test_reviewing_keeps_it_in_the_queue(self, client, db, seed, platform_admin):
        """`isOpen` exists for this. Four of the five decisions end the request
        and `reviewing` does not — a screen that treated them alike would tell an
        admin a claim had left a queue it is still sitting in."""
        filed = _file_takedown(client, subject_xid=_passage_xid(db, seed))
        decided = _ok(client.patch(f"/api/v1/admin/takedowns/{filed['xid']}",
                                   json={"status": "reviewing"},
                                   headers=auth(platform_admin.xid)))
        assert decided["status"] == "reviewing"
        still = _ok(client.get("/api/v1/admin/takedowns?status=open&limit=200",
                               headers=auth(platform_admin.xid)))["items"]
        assert [i["status"] for i in still] == ["reviewing"]

    def test_a_final_decision_keeps_the_note_that_explains_it(
            self, client, db, seed, platform_admin):
        """The note is required by the form for the four closing decisions
        because it is the only lasting record: `decide_takedown` writes no audit
        row and overwrites `actioned_by` if it is ever called twice."""
        filed = _file_takedown(client, subject_xid=_passage_xid(db, seed))
        _ok(client.patch(
            f"/api/v1/admin/takedowns/{filed['xid']}",
            json={"status": "rejected",
                  "outcome_note": "Public domain; the 1897 edition is out of copyright."},
            headers=auth(platform_admin.xid)))
        decided = _ok(client.get("/api/v1/admin/takedowns?status=all&limit=200",
                                 headers=auth(platform_admin.xid)))["items"][0]
        assert decided["status"] == "rejected"
        assert decided["outcome_note"].startswith("Public domain")

    def test_a_status_outside_the_five_is_refused(self, client, db, seed, platform_admin):
        """Which is why the decision select offers exactly five values and not
        `received`. A typo'd status is a takedown whose outcome the record cannot
        state."""
        filed = _file_takedown(client, subject_xid=_passage_xid(db, seed))
        refused = client.patch(f"/api/v1/admin/takedowns/{filed['xid']}",
                               json={"status": "received"},
                               headers=auth(platform_admin.xid))
        assert refused.status_code == 422, refused.text

    def test_oldest_first_and_the_screen_keeps_that_order(
            self, client, db, seed, platform_admin):
        """The order is the server's and this page does not re-sort. Newest-first
        is how the request that has been sitting for three weeks stays at the
        bottom, and the clock a rights holder cares about started when they
        filed.

        `received_at` defaults to `now()`, which inside one transaction is the
        same instant for all three rows — so the ages are set explicitly, or this
        would assert nothing.
        """
        names = ["Oldest", "Middle", "Newest"]
        for index, name in enumerate(names):
            filed = _file_takedown(client, subject_xid=_passage_xid(db, seed),
                                   claimant=name)
            db.execute(text("""
                UPDATE takedown_requests SET received_at = now() - make_interval(days => :d)
                WHERE xid = CAST(:x AS uuid)
            """).bindparams(d=len(names) - index, x=filed["xid"]))
        db.flush()
        body = _ok(client.get("/api/v1/admin/takedowns?status=open&limit=200",
                              headers=auth(platform_admin.xid)))
        assert [i["claimant_name"] for i in body["items"]] == names
        assert [i["received_at"] for i in body["items"]] \
            == sorted(i["received_at"] for i in body["items"])

    def test_a_subject_that_does_not_resolve_still_reaches_the_queue(
            self, client, platform_admin):
        """Filing is unauthenticated and stores `subject_id = 0` when the named
        subject is not found, so the claim survives a mistyped identifier. The
        panel says the subject does not exist rather than rendering an empty
        cell — deciding on the description alone is a real outcome, and losing
        the claim is not."""
        _file_takedown(client, subject_xid=_uuid.uuid4())
        row = _ok(client.get("/api/v1/admin/takedowns?status=open&limit=200",
                             headers=auth(platform_admin.xid)))["items"][0]
        assert row["subject_xid"] is None
        assert row["subject_title"] is None
        assert row["claimant_name"] == "Cambridge University Press"

    def test_a_subject_type_outside_the_vocabulary_still_reaches_the_queue(
            self, client, db, seed, platform_admin):
        """`subjectLabel` falls back to the raw word for this row. `POST
        /takedowns` does not validate `subject_type` against `_SUBJECT_TABLES` at
        all, so a claim about something we do not model is filed anyway — and a
        blank cell would hide a real allegation from the one person who reads
        them."""
        _file_takedown(client, subject_type="video_lesson",
                       subject_xid=_passage_xid(db, seed))
        row = _ok(client.get("/api/v1/admin/takedowns?status=open&limit=200",
                             headers=auth(platform_admin.xid)))["items"][0]
        assert row["subject_type"] == "video_lesson"
        assert row["subject_title"] is None

    def test_the_queue_does_not_carry_the_sworn_statement(
            self, client, db, seed, platform_admin):
        """Which is why the panel states the RULE rather than showing a tick.
        `file_takedown` refuses a request whose `sworn_statement` is false, so
        every row that exists has one — but the column is not selected, and
        drawing a tick from its absence would show a guess as a fact."""
        _file_takedown(client, subject_xid=_passage_xid(db, seed))
        row = _ok(client.get("/api/v1/admin/takedowns?status=open&limit=200",
                             headers=auth(platform_admin.xid)))["items"][0]
        assert "sworn_statement" not in row
        refused = client.post("/api/v1/takedowns", json={
            "claimant_name": "Nobody", "claimant_email": "n@example.org",
            "rights_basis": "None", "sworn_statement": False,
            "subject_type": "passage", "subject_xid": _passage_xid(db, seed),
            "description": "Unsworn.",
        })
        assert refused.status_code == 403, refused.text

    def test_a_version_xid_files_against_nothing_and_says_nothing(
            self, client, db, seed, platform_admin):
        """A trap for whoever builds the public filing page, and the reason the
        claim panel has an explicit "does not exist" branch.

        A rights holder reading a paper sees a VERSION. `_SUBJECT_TABLES` maps
        `passage` to `passages`, so filing with the version's xid is accepted,
        stored with `subject_id = 0`, and arrives here indistinguishable from a
        typo — no error at filing, and nothing on the row to say which it was.
        """
        _file_takedown(client, subject_xid=seed["passage_version"].xid)
        row = _ok(client.get("/api/v1/admin/takedowns?status=open&limit=200",
                             headers=auth(platform_admin.xid)))["items"][0]
        assert row["subject_type"] == "passage"
        assert row["subject_xid"] is None
        assert row["subject_title"] is None

    def test_no_internal_id_reaches_the_screen(self, client, db, seed, platform_admin):
        _file_takedown(client, subject_xid=_passage_xid(db, seed))
        row = _ok(client.get("/api/v1/admin/takedowns?status=open&limit=200",
                             headers=auth(platform_admin.xid)))["items"][0]
        assert "subject_id" not in row

    def test_a_centre_admin_cannot_read_the_queue(
            self, client, db, seed, centre_admin):
        """The reason this screen refuses instead of showing an empty table. A
        queue of allegations against a centre's material, readable by that
        centre's own staff, is a notification service for "we are about to be
        caught" — and a centre admin is senior enough that the mistake would look
        deliberate."""
        _file_takedown(client, subject_xid=_passage_xid(db, seed))
        refused = client.get("/api/v1/admin/takedowns?status=open&limit=200",
                             headers=auth(centre_admin.xid))
        assert refused.status_code == 403, refused.text

    def test_a_teacher_cannot_read_the_queue(self, client, db, seed):
        _file_takedown(client, subject_xid=_passage_xid(db, seed))
        refused = client.get("/api/v1/admin/takedowns?status=open&limit=200",
                             headers=auth(seed["author"].xid))
        assert refused.status_code == 403, refused.text

    def test_a_centre_admin_cannot_decide_one_either(
            self, client, db, seed, centre_admin):
        filed = _file_takedown(client, subject_xid=_passage_xid(db, seed))
        refused = client.patch(f"/api/v1/admin/takedowns/{filed['xid']}",
                               json={"status": "rejected", "outcome_note": "No."},
                               headers=auth(centre_admin.xid))
        assert refused.status_code == 403, refused.text


# ── what the copy on these screens may not say ───────────────────────


class TestWhatTheseScreensMayNotPromise:
    """These tests are load-bearing in an unusual direction: they pin behaviour
    the screens describe in words, so wiring the behaviour up fails them and
    forces the copy to be corrected with it.

    One of the two has now been wired up — a `view` grant reaches the content —
    and this is what that looks like when it happens.
    """

    def test_a_view_grant_makes_the_content_visible(
            self, client, seed, centre_admin, rival):
        """`policy.filter_content` documents four visibility routes and the
        fourth — "anything explicitly shared via `content_grants`" — is reached
        through a `grant_ids` argument that NO CALLER PASSED. `copy` on a test
        was the only permission any handler read, in `_require_copy_grant`, so
        the marketplace seam listed a grant correctly and did nothing.

        `authz.grants` is the reader, wired into `scoped()` — the one choke
        point every listing goes through — and permission is a hierarchy, so
        `copy` and `assign` satisfy a read as well.
        """
        _ok(_share(client, seed, centre_admin, rival["org"].xid,
                   permission="view"), 201)
        theirs = _ok(client.get("/api/v1/tests?limit=100",
                                headers=auth(rival["admin"].xid)))
        assert [t["title"] for t in theirs["items"]] == [seed["test"].title]

    def test_the_soft_hide_hides_nothing_yet(self, client, db, seed, platform_admin):
        """Filing writes `takedown_requests.hidden_at` and nothing reads it. The
        composition loader never populates `PassageRef.under_takedown`, so the
        publish gate's `TAKEDOWN_OPEN` finding cannot fire either, and the
        passage stays in every listing that showed it before.

        The claim panel therefore says a hide was RECORDED and that the material
        is still reachable, rather than repeating the design.
        """
        filed = _file_takedown(client, subject_xid=_passage_xid(db, seed))
        assert filed["hidden_at"] is not None

        listed = _ok(client.get("/api/v1/passages?limit=100",
                                headers=auth(seed["author"].xid)))
        assert "Cartography" in [p["title"] for p in listed["items"]]
