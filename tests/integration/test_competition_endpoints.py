"""Competitions: registration, capacity, and the board that gets screenshotted.

The fairness machinery — the two-phase start, the server-authoritative clock, the
key freeze — is covered by `test_competition_and_speaking.py` and is correct. What
was never executed is everything around it: the whole of `register` and `withdraw`,
most of `create_competition`, and the leaderboard's ordering.

Four things were wrong in that gap.

**A capacity-capped contest could not be entered at all.** The check was
`SELECT count(*) ... FOR UPDATE`, which PostgreSQL refuses outright — *FOR UPDATE
is not allowed with aggregate functions*. Every registration for a contest with
`max_participants` set returned 500.

**The leaderboard ignored the contest's own tiebreak.** `tiebreak` is stored per
contest and `materialize()` ranks by it; the display query hardcoded the default
order and ignored the `rank` it had just been given, so a contest configured
`band_desc` showed rank numbers that disagreed with the order of the rows they
were attached to — and with `LIMIT`, could omit the winner.

**`around_me` did nothing.** Documented in the OpenAPI schema, accepted by the
handler, never read. A student ranked 340th of 500 got rows 1-50 and their own
number with no way to see their neighbourhood.

**No `ETag`, though the spec documents one and a 304.** The spec calls this
polling path "the fallback for clients that cannot keep a socket open, which on
these networks is a meaningful fraction" — and then made each of them re-download
the whole board every few seconds.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


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


@pytest.fixture
def admin(db, seed):
    from app.modules.identity.models import PlatformRoleGrant

    db.add(PlatformRoleGrant(user_id=seed["author"].id, role="platform_admin",
                             granted_by=seed["author"].id))
    db.flush()
    return auth(seed["author"].xid)


def _entitle(db, user_id: int, feature: str = "competition.entry") -> None:
    from app.modules.billing.models import EntitlementRow

    db.add(EntitlementRow(subject_kind="user", subject_id=user_id, feature=feature,
                          source_kind="order", starts_at=_now() - dt.timedelta(days=1)))
    db.flush()


def _student(db, name: str, *, org_id=None, entitled: bool = False):
    """A user. `org_id` only when they need to authenticate against a contest;
    the board tests want rows on a leaderboard, not principals."""
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, family_name, date_of_birth, status)
        VALUES (:p, :n, 'Karimova', CAST('2000-01-01' AS date), 'active')
        RETURNING id, xid
    """).bindparams(p=f"+99891{uuid.uuid4().int % 10**7:07d}", n=name)).mappings().one()
    if org_id:
        db.execute(text("""
            INSERT INTO org_memberships (org_id, user_id, role, status)
            VALUES (:o, :u, 'student', 'active')
        """).bindparams(o=org_id, u=row["id"]))
    db.flush()
    if entitled:
        _entitle(db, row["id"])
    return row


def _competition(db, published, *, starts_in: int = 3600, duration: int = 1800,
                 status: str = "registration", visibility: str = "org",
                 max_participants=None, registration_closes_in=None,
                 tiebreak=None) -> dict:
    starts = _now() + dt.timedelta(seconds=starts_in)
    return db.execute(text("""
        INSERT INTO competitions (org_id, test_version_id, title, lobby_opens_at,
                                  starts_at, ends_at, duration_seconds, status,
                                  visibility, max_participants,
                                  registration_closes_at, payload_key_id, created_by,
                                  tiebreak)
        VALUES (:org, :tv, 'Friday Contest', :lobby, :starts,
                :starts + make_interval(secs => :dur), :dur, :status, :vis, :maxp,
                :reg_close, 'k-test-1', :by,
                coalesce(CAST(:tiebreak AS jsonb),
                         '["raw_score_desc","duration_asc","submitted_at_asc"]'))
        RETURNING id, xid
    """).bindparams(
        org=published["org"].id, tv=published["test_version"].id,
        lobby=starts - dt.timedelta(seconds=120), starts=starts, dur=duration,
        status=status, vis=visibility, maxp=max_participants,
        reg_close=(_now() + dt.timedelta(seconds=registration_closes_in)
                   if registration_closes_in is not None else None),
        by=published["author"].id,
        tiebreak=None if tiebreak is None else __import__("json").dumps(tiebreak),
    )).mappings().one()


# ── registration ─────────────────────────────────────────────────────

class TestRegistration:
    @pytest.fixture(autouse=True)
    def _entitled_seed(self, db, seed):
        _entitle(db, seed["student"].id)

    def test_a_registered_student_gets_an_entry(self, client, db, published):
        contest = _competition(db, published)
        response = client.post(f"/api/v1/competitions/{contest['xid']}/register",
                               headers=auth(published["student"].xid))
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "registered"
        assert db.scalar(text("SELECT count(*) FROM competition_entries")) == 1

    def test_registering_twice_is_idempotent(self, client, db, published):
        contest = _competition(db, published)
        for _ in range(2):
            client.post(f"/api/v1/competitions/{contest['xid']}/register",
                        headers=auth(published["student"].xid))
        assert db.scalar(text("SELECT count(*) FROM competition_entries")) == 1

    def test_a_contest_that_has_started_refuses_registration(self, client, db,
                                                             published):
        contest = _competition(db, published, status="live")
        refused = client.post(f"/api/v1/competitions/{contest['xid']}/register",
                              headers=auth(published["student"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "registration_closed"

    def test_a_closed_registration_window_refuses(self, client, db, published):
        """`registration_closes_at` is checked against the SERVER clock, like
        every other deadline in this product."""
        contest = _competition(db, published, registration_closes_in=-60)
        refused = client.post(f"/api/v1/competitions/{contest['xid']}/register",
                              headers=auth(published["student"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "registration_closed"

    def test_a_contest_past_its_start_refuses_even_if_the_tick_is_behind(
            self, client, db, published):
        """`registration_open` checks three things and this endpoint used to check
        two of them, missing `now < starts_at`.

        The state machine moves `registration → lobby` at T-120s, so a contest only
        sits in `registration` past its own start time when the scheduler is down
        or behind — which is exactly when nobody is watching. A student registering
        then gets an entry for a contest that has already begun.
        """
        contest = _competition(db, published, starts_in=-60, status="registration")
        refused = client.post(f"/api/v1/competitions/{contest['xid']}/register",
                              headers=auth(published["student"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "registration_closed"

    def test_a_scheduled_contest_still_in_the_future_is_open(self, client, db,
                                                             published):
        contest = _competition(db, published, status="scheduled")
        assert client.post(f"/api/v1/competitions/{contest['xid']}/register",
                           headers=auth(published["student"].xid)).status_code == 201

    def test_an_unentitled_student_is_refused(self, client, db, published):
        outsider = _student(db, "Nodira", org_id=published["org"].id)
        contest = _competition(db, published)
        refused = client.post(f"/api/v1/competitions/{contest['xid']}/register",
                              headers=auth(outsider["xid"]))
        assert refused.status_code in (402, 403), refused.text

    def test_an_org_contest_is_invisible_to_another_centre(self, client, db,
                                                           published):
        """A 404, not a 403: confirming a rival centre's contest exists is itself
        the leak."""
        rival_org = db.scalar(text("""
            INSERT INTO organizations (name, slug, status)
            VALUES ('Rival', 'rival-comp', 'active') RETURNING id
        """))
        stranger = db.execute(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES ('+998915550001', 'Sardor', CAST('1999-01-01' AS date), 'active')
            RETURNING id, xid
        """)).mappings().one()
        db.execute(text("""
            INSERT INTO org_memberships (org_id, user_id, role, status)
            VALUES (:o, :u, 'student', 'active')
        """).bindparams(o=rival_org, u=stranger["id"]))
        _entitle(db, stranger["id"])
        contest = _competition(db, published)
        assert client.post(f"/api/v1/competitions/{contest['xid']}/register",
                           headers=auth(stranger["xid"])).status_code == 404

    def test_an_unknown_competition_is_a_404(self, client, published):
        assert client.post(f"/api/v1/competitions/{uuid.uuid4()}/register",
                           headers=auth(published["student"].xid)).status_code == 404


class TestCapacity:
    """`max_participants` had never been exercised. The check was
    `SELECT count(*) ... FOR UPDATE`, which PostgreSQL rejects outright:

        ERROR:  FOR UPDATE is not allowed with aggregate functions

    So every registration for a capped contest returned 500 — the feature was not
    merely unenforced, it made the endpoint unusable.
    """

    def test_a_capped_contest_can_be_entered(self, client, db, seed, published):
        _entitle(db, seed["student"].id)
        contest = _competition(db, published, max_participants=2)
        response = client.post(f"/api/v1/competitions/{contest['xid']}/register",
                               headers=auth(published["student"].xid))
        assert response.status_code == 201, response.text

    def test_a_full_contest_is_refused(self, client, db, seed, published):
        _entitle(db, seed["student"].id)
        second = _student(db, "Kamola", org_id=published["org"].id, entitled=True)
        contest = _competition(db, published, max_participants=1)
        assert client.post(f"/api/v1/competitions/{contest['xid']}/register",
                           headers=auth(published["student"].xid)).status_code == 201
        refused = client.post(f"/api/v1/competitions/{contest['xid']}/register",
                              headers=auth(second["xid"]))
        assert refused.status_code == 409
        assert refused.json()["code"] == "competition_full"

    def test_a_withdrawal_frees_the_place(self, client, db, seed, published):
        """`status <> 'withdrawn'` is in the count for a reason: a student who
        pulls out should not hold a seat nobody can use."""
        _entitle(db, seed["student"].id)
        second = _student(db, "Kamola", org_id=published["org"].id, entitled=True)
        contest = _competition(db, published, max_participants=1)
        client.post(f"/api/v1/competitions/{contest['xid']}/register",
                    headers=auth(published["student"].xid))
        assert client.delete(f"/api/v1/competitions/{contest['xid']}/register",
                             headers=auth(published["student"].xid)).status_code == 204
        assert client.post(f"/api/v1/competitions/{contest['xid']}/register",
                           headers=auth(second["xid"])).status_code == 201

    def test_rejoining_after_withdrawing_is_allowed_while_a_place_is_free(
            self, client, db, seed, published):
        _entitle(db, seed["student"].id)
        contest = _competition(db, published, max_participants=1)
        xid = contest["xid"]
        client.post(f"/api/v1/competitions/{xid}/register",
                    headers=auth(published["student"].xid))
        client.delete(f"/api/v1/competitions/{xid}/register",
                      headers=auth(published["student"].xid))
        again = client.post(f"/api/v1/competitions/{xid}/register",
                            headers=auth(published["student"].xid))
        assert again.status_code == 201
        assert again.json()["status"] == "registered"


class TestWithdrawal:
    @pytest.fixture(autouse=True)
    def _entitled_seed(self, db, seed):
        _entitle(db, seed["student"].id)

    def test_withdrawing_marks_the_entry(self, client, db, published):
        contest = _competition(db, published)
        client.post(f"/api/v1/competitions/{contest['xid']}/register",
                    headers=auth(published["student"].xid))
        assert client.delete(f"/api/v1/competitions/{contest['xid']}/register",
                             headers=auth(published["student"].xid)).status_code == 204
        assert db.scalar(text("SELECT status FROM competition_entries")) == "withdrawn"

    @pytest.mark.parametrize("state", ["live", "grading", "final"])
    def test_you_cannot_withdraw_once_it_has_started(self, client, db, published,
                                                     state):
        """Withdrawing from a contest you have already sat is how a bad score
        disappears. It has to be refused."""
        contest = _competition(db, published, status=state)
        refused = client.delete(f"/api/v1/competitions/{contest['xid']}/register",
                                headers=auth(published["student"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "competition_started"

    def test_withdrawing_without_an_entry_is_a_no_op(self, client, db, published):
        contest = _competition(db, published)
        assert client.delete(f"/api/v1/competitions/{contest['xid']}/register",
                             headers=auth(published["student"].xid)).status_code == 204


# ── creation ─────────────────────────────────────────────────────────

def _create(client, headers, published, **overrides):
    body = {"title": "Friday Contest",
            "test_version_xid": str(published["test_version"].xid),
            "starts_at": (_now() + dt.timedelta(hours=2)).isoformat(),
            "duration_seconds": 1800, "visibility": "org"}
    body.update(overrides)
    return client.post("/api/v1/competitions", headers=headers, json=body)


class TestCreation:
    def test_a_teacher_can_open_an_org_contest(self, client, db, published):
        author = auth(published["author"].xid)
        response = _create(client, author, published)
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "registration"
        assert response.json()["org_xid"] == str(published["org"].xid)

    def test_the_lobby_defaults_to_two_minutes_before(self, client, published):
        """"A five-second lobby would put 200 clients on the payload at once,
        which is exactly what the two-phase start exists to avoid." """
        starts = _now() + dt.timedelta(hours=2)
        body = _create(client, auth(published["author"].xid), published,
                       starts_at=starts.isoformat()).json()
        lobby = dt.datetime.fromisoformat(body["lobby_opens_at"])
        assert (dt.datetime.fromisoformat(body["starts_at"]) - lobby) \
            == dt.timedelta(seconds=120)

    def test_a_lobby_after_the_start_is_refused(self, client, published):
        starts = _now() + dt.timedelta(hours=2)
        refused = _create(client, auth(published["author"].xid), published,
                          starts_at=starts.isoformat(),
                          lobby_opens_at=(starts + dt.timedelta(minutes=5)).isoformat())
        assert refused.status_code == 409
        assert refused.json()["code"] == "invalid_lobby_window"

    def test_a_draft_version_cannot_back_a_contest(self, client, db, seed):
        """A contest over an unpublished paper is a contest over a paper that can
        still change under it."""
        refused = _create(client, auth(seed["author"].xid), seed)
        assert refused.status_code == 409
        assert refused.json()["code"] == "version_not_published"

    def test_a_student_cannot_open_one(self, client, db, published):
        refused = _create(client, auth(published["student"].xid), published)
        assert refused.status_code == 403
        assert refused.json()["code"] == "not_a_centre"

    def test_a_public_contest_is_a_platform_admin_action(self, client, published):
        """A public contest reaches every user of the platform. A single centre
        must not be able to publish one."""
        refused = _create(client, auth(published["author"].xid), published,
                          visibility="public")
        assert refused.status_code == 403
        assert refused.json()["code"] == "admin_only"

    def test_an_admin_can_open_a_public_contest(self, client, admin, published):
        response = _create(client, admin, published, visibility="public")
        assert response.status_code == 201, response.text
        assert response.json()["org_xid"] is None

    def test_an_unknown_test_version_is_a_404(self, client, published):
        assert _create(client, auth(published["author"].xid), published,
                       test_version_xid=str(uuid.uuid4())).status_code == 404


# ── the list ─────────────────────────────────────────────────────────

class TestTheList:
    def test_an_org_contest_is_listed_to_its_own_centre(self, client, db, published):
        _competition(db, published)
        listed = client.get("/api/v1/competitions",
                            headers=auth(published["student"].xid)).json()
        assert [c["title"] for c in listed] == ["Friday Contest"]

    def test_an_org_contest_is_not_listed_to_another_centre(self, client, db,
                                                            published):
        rival_org = db.scalar(text("""
            INSERT INTO organizations (name, slug, status)
            VALUES ('Rival', 'rival-list', 'active') RETURNING id
        """))
        stranger = db.execute(text("""
            INSERT INTO users (phone, given_name, date_of_birth, status)
            VALUES ('+998915550002', 'Sardor', CAST('1999-01-01' AS date), 'active')
            RETURNING id, xid
        """)).mappings().one()
        db.execute(text("""
            INSERT INTO org_memberships (org_id, user_id, role, status)
            VALUES (:o, :u, 'student', 'active')
        """).bindparams(o=rival_org, u=stranger["id"]))
        db.flush()
        _competition(db, published)
        assert client.get("/api/v1/competitions",
                          headers=auth(stranger["xid"])).json() == []

    def test_an_invite_contest_is_not_discoverable(self, client, db, published):
        """"An invite-only contest never appears here — it reaches its
        participants by invitation." """
        _competition(db, published, visibility="invite")
        assert client.get("/api/v1/competitions",
                          headers=auth(published["student"].xid)).json() == []

    def test_an_invite_contest_you_are_entered_in_is_listed(self, client, db,
                                                            published):
        """Once you hold an entry it is yours to see — otherwise a contest you
        were invited to is unreachable from the app."""
        contest = _competition(db, published, visibility="invite")
        db.execute(text("""
            INSERT INTO competition_entries (competition_id, user_id) VALUES (:c, :u)
        """).bindparams(c=contest["id"], u=published["student"].id))
        db.flush()
        listed = client.get("/api/v1/competitions",
                            headers=auth(published["student"].xid)).json()
        assert [c["title"] for c in listed] == ["Friday Contest"]
        assert listed[0]["my_entry"]["status"] == "registered"

    @pytest.mark.parametrize(("state", "status", "visible"), [
        ("upcoming", "registration", True),
        ("upcoming", "cancelled", False),
        ("live", "live", True),
        ("live", "registration", False),
        ("finished", "final", True),
        ("finished", "live", False),
    ])
    def test_the_state_filter(self, client, db, published, state, status, visible):
        starts_in = 3600 if state == "upcoming" else -3600
        _competition(db, published, status=status, starts_in=starts_in)
        listed = client.get(f"/api/v1/competitions?state={state}",
                            headers=auth(published["student"].xid)).json()
        assert bool(listed) is visible

    def test_the_countdown_is_rendered_from_the_server_clock(self, client, db,
                                                             published):
        """"Every countdown in the client is rendered from this delta, never from
        the device clock." """
        _competition(db, published)
        listed = client.get("/api/v1/competitions",
                            headers=auth(published["student"].xid)).json()
        assert listed[0]["server_now"]


def _enter(db, contest, user_id: int, *, status: str = "registered") -> None:
    db.execute(text("""
        INSERT INTO competition_entries (competition_id, user_id, status)
        VALUES (:c, :u, :s)
    """).bindparams(c=contest["id"], u=user_id, s=status))
    db.flush()


class TestTheKeyRelease:
    """Step two of the two-phase start. The refusals here are the ones that decide
    who is allowed to be holding a decryption key at T-0."""

    def test_a_disqualified_entry_gets_no_key(self, client, db, published):
        """The one refusal that is an anti-cheat control rather than a state
        check: a disqualification during the contest has to take the paper away,
        not just annotate the result afterwards."""
        contest = _competition(db, published, starts_in=-60, status="live")
        _enter(db, contest, published["student"].id, status="disqualified")
        refused = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                              headers=auth(published["student"].xid))
        assert refused.status_code == 403
        assert refused.json()["code"] == "disqualified"

    def test_an_unregistered_user_gets_no_key(self, client, db, published):
        contest = _competition(db, published, starts_in=-60, status="live")
        refused = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                              headers=auth(published["student"].xid))
        assert refused.status_code == 403
        assert refused.json()["code"] == "not_registered"

    def test_a_finished_contest_releases_nothing(self, client, db, published):
        """After `ends_at` there is no legitimate reason to hand out the key, and
        every reason not to: the paper is reusable and the answers are not."""
        contest = _competition(db, published, starts_in=-7200, duration=1800,
                               status="grading")
        _enter(db, contest, published["student"].id)
        refused = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                              headers=auth(published["student"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "competition_finished"

    def test_the_lobby_refuses_a_paper_with_no_snapshot(self, client, db, seed):
        """A contest over an unpublished version has nothing to encrypt. It is
        created through the endpoint only from a published version, so reaching
        this means the version was created around the check."""
        contest = _competition(db, seed, starts_in=-60, status="lobby")
        _enter(db, contest, seed["student"].id)
        refused = client.get(f"/api/v1/competitions/{contest['xid']}/lobby",
                             headers=auth(seed["student"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "payload_missing"

    def test_the_keys_are_frozen_once_per_contest_not_once_per_student(
            self, client, db, published):
        """ADR-0001 §8.4. `frozen_at` must record when the CONTEST started, not
        when the last competitor happened to ask for their key — otherwise the
        recorded scoring inputs drift with the slowest connection in the room."""
        contest = _competition(db, published, starts_in=-60, status="live")
        second = _student(db, "Kamola", org_id=published["org"].id, entitled=True)
        _enter(db, contest, published["student"].id)
        _enter(db, contest, second["id"])

        first = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                            headers=auth(published["student"].xid))
        assert first.status_code == 200, first.text
        frozen = db.scalar(text("SELECT key_freeze->>'frozen_at' FROM competitions"))
        assert frozen

        later = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                            headers=auth(second["xid"]))
        assert later.status_code == 200, later.text
        assert db.scalar(text("SELECT key_freeze->>'frozen_at' FROM competitions")) \
            == frozen

    def test_asking_twice_returns_the_same_attempt(self, client, db, published):
        """A retry on a flaky connection must not mint a second attempt — that
        would be two papers and two clocks for one competitor."""
        contest = _competition(db, published, starts_in=-60, status="live")
        _enter(db, contest, published["student"].id)
        headers = auth(published["student"].xid)
        first = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                            headers=headers).json()
        again = client.post(f"/api/v1/competitions/{contest['xid']}/key",
                            headers=headers).json()
        assert first["attempt"]["xid"] == again["attempt"]["xid"]
        assert db.scalar(text("SELECT count(*) FROM attempts")) == 1


class TestIdempotentRegistration:
    def test_a_replayed_key_returns_the_stored_response(self, client, db, seed,
                                                        published):
        """"A replay with the same key and the same body returns the stored
        response." The mobile client will retry this on a dropped connection."""
        _entitle(db, seed["student"].id)
        contest = _competition(db, published)
        headers = {**auth(published["student"].xid),
                   "Idempotency-Key": "retry-me-0001"}
        first = client.post(f"/api/v1/competitions/{contest['xid']}/register",
                            headers=headers)
        again = client.post(f"/api/v1/competitions/{contest['xid']}/register",
                            headers=headers)
        assert first.status_code == again.status_code == 201
        assert first.json() == again.json()
        assert db.scalar(text("SELECT count(*) FROM competition_entries")) == 1


# ── the board ────────────────────────────────────────────────────────

def _result(db, contest, user, *, raw, band, duration_ms, rank, submitted=None):
    attempt = db.scalar(text("""
        INSERT INTO attempts (user_id, test_version_id, competition_id, mode, status,
                              started_at, submitted_at)
        VALUES (:u, (SELECT test_version_id FROM competitions WHERE id = :c), :c,
                'exam', 'scored', now(), coalesce(:sub, now()))
        RETURNING id
    """).bindparams(u=user["id"], c=contest["id"], sub=submitted))
    run = db.scalar(text("""
        INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions,
                                raw_score, max_raw, band)
        VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, :raw, 40, :band) RETURNING id
    """).bindparams(a=attempt, raw=raw, band=band))
    db.execute(text("""
        INSERT INTO competition_results (competition_id, user_id, attempt_id,
                                         score_run_id, raw_score, band, duration_ms,
                                         submitted_at, rank, is_provisional)
        VALUES (:c, :u, :a, :r, :raw, :band, :dur, coalesce(:sub, now()), :rank, false)
    """).bindparams(c=contest["id"], u=user["id"], a=attempt, r=run, raw=raw,
                    band=band, dur=duration_ms, rank=rank, sub=submitted))
    db.flush()


class TestTheBoardHonoursTheContestsOwnTiebreak:
    """`tiebreak` is stored per contest and `materialize()` ranks by it — "data
    rather than code so a centre can run 'highest score, then fastest' without a
    deploy". The display query hardcoded the DEFAULT order and ignored the `rank`
    it had been handed, so a contest configured any other way showed rank numbers
    that disagreed with the order of the rows they were printed next to.
    """

    def test_the_board_is_ordered_by_the_stored_rank(self, client, db, seed,
                                                     published):
        """Ranked by band, which the hardcoded ORDER BY has no term for at all.
        Faster-but-lower-band must not be shown first."""
        contest = _competition(db, published, status="final",
                              tiebreak=["band_desc", "duration_asc"])
        slow_high = _student(db, "Malika")
        fast_low = _student(db, "Jasur")
        # The band winner was slower, so raw/duration order puts them second.
        _result(db, contest, slow_high, raw=30, band=8.0, duration_ms=1_700_000, rank=1)
        _result(db, contest, fast_low, raw=32, band=6.5, duration_ms=600_000, rank=2)

        board = client.get(f"/api/v1/competitions/{contest['xid']}/leaderboard",
                           headers=auth(published["student"].xid)).json()
        assert [e["rank"] for e in board["entries"]] == [1, 2]
        assert [e["user"]["display_name"] for e in board["entries"]] \
            == ["Malika K.", "Jasur K."]

    def test_the_winner_is_not_dropped_by_the_limit(self, client, db, published):
        """With the wrong ordering, `LIMIT` cuts from the wrong end: the actual
        winner could be missing from the top of their own board."""
        contest = _competition(db, published, status="final",
                              tiebreak=["band_desc", "duration_asc"])
        winner = _student(db, "Malika")
        for i, name in enumerate(("Jasur", "Kamola", "Nodira")):
            _result(db, contest, _student(db, name), raw=39 - i, band=6.0,
                    duration_ms=600_000, rank=i + 2)
        _result(db, contest, winner, raw=20, band=9.0, duration_ms=1_700_000, rank=1)

        board = client.get(f"/api/v1/competitions/{contest['xid']}/leaderboard?limit=1",
                           headers=auth(published["student"].xid)).json()
        assert [e["user"]["display_name"] for e in board["entries"]] == ["Malika K."]

    def test_a_shared_rank_stays_deterministic(self, client, db, published):
        """"Genuine ties share a rank — inventing a winner between two identical
        papers is the one thing a leaderboard must never do." Two rows at rank 1
        must still come back in a stable order."""
        contest = _competition(db, published, status="final")
        for name in ("Jasur", "Kamola"):
            _result(db, contest, _student(db, name), raw=30, band=7.0,
                    duration_ms=900_000, rank=1)
        first = client.get(f"/api/v1/competitions/{contest['xid']}/leaderboard",
                           headers=auth(published["student"].xid)).json()
        second = client.get(f"/api/v1/competitions/{contest['xid']}/leaderboard",
                            headers=auth(published["student"].xid)).json()
        assert [e["rank"] for e in first["entries"]] == [1, 1]
        assert first["entries"] == second["entries"]

    def test_an_unranked_row_sorts_last(self, client, db, published):
        """`rank` is nullable — a row materialized before ranking must not float
        to the top of the board."""
        contest = _competition(db, published, status="final")
        _result(db, contest, _student(db, "Jasur"), raw=10, band=5.0,
                duration_ms=900_000, rank=None)
        _result(db, contest, _student(db, "Kamola"), raw=30, band=7.0,
                duration_ms=900_000, rank=1)
        board = client.get(f"/api/v1/competitions/{contest['xid']}/leaderboard",
                           headers=auth(published["student"].xid)).json()
        assert [e["rank"] for e in board["entries"]] == [1, None]


class TestAroundMe:
    """Documented in the OpenAPI schema, accepted by the handler, and never read.
    A student ranked 340th of 500 got rows 1-50 and their own number."""

    @pytest.fixture
    def crowded(self, db, seed, published):
        contest = _competition(db, published, status="final")
        _result(db, contest, {"id": seed["student"].id}, raw=20, band=6.0,
                duration_ms=900_000, rank=6)
        for i, name in enumerate(("A", "B", "C", "D", "E", "F", "G", "H", "I", "J")):
            rank = i + 1 if i < 5 else i + 2
            _result(db, contest, _student(db, f"Rival{name}"), raw=40 - i, band=7.0,
                    duration_ms=900_000, rank=rank)
        return contest

    def test_it_returns_the_neighbourhood_not_the_top(self, client, crowded,
                                                     published):
        board = client.get(
            f"/api/v1/competitions/{crowded['xid']}/leaderboard"
            f"?around_me=true&limit=5", headers=auth(published["student"].xid)).json()
        ranks = [e["rank"] for e in board["entries"]]
        assert board["my_rank"] == 6
        assert 6 in ranks
        assert ranks == sorted(ranks)
        assert 1 not in ranks, "this is the top of the board, not my neighbourhood"

    def test_without_it_the_top_is_returned(self, client, crowded, published):
        board = client.get(
            f"/api/v1/competitions/{crowded['xid']}/leaderboard?limit=5",
            headers=auth(published["student"].xid)).json()
        assert [e["rank"] for e in board["entries"]] == [1, 2, 3, 4, 5]

    def test_an_unranked_viewer_falls_back_to_the_top(self, client, db, crowded,
                                                      published):
        """Someone who did not sit the contest has no neighbourhood. They get the
        board rather than an empty list."""
        onlooker = _student(db, "Onlooker", org_id=published["org"].id)
        board = client.get(
            f"/api/v1/competitions/{crowded['xid']}/leaderboard?around_me=true&limit=5",
            headers=auth(onlooker["xid"])).json()
        assert board["my_rank"] is None
        assert [e["rank"] for e in board["entries"]] == [1, 2, 3, 4, 5]


class TestConditionalRequests:
    """The spec documents `ETag` on every response and `If-None-Match` → 304, and
    calls this path "the fallback for clients that cannot keep a socket open,
    which on these networks is a meaningful fraction". Neither existed."""

    @pytest.fixture
    def board(self, db, published):
        contest = _competition(db, published, status="final")
        _result(db, contest, _student(db, "Jasur"), raw=30, band=7.0,
                duration_ms=900_000, rank=1)
        return contest

    def test_a_response_carries_an_etag(self, client, board, published):
        response = client.get(f"/api/v1/competitions/{board['xid']}/leaderboard",
                              headers=auth(published["student"].xid))
        assert response.headers.get("ETag")

    def test_an_unchanged_board_is_a_304(self, client, board, published):
        headers = auth(published["student"].xid)
        etag = client.get(f"/api/v1/competitions/{board['xid']}/leaderboard",
                          headers=headers).headers["ETag"]
        again = client.get(f"/api/v1/competitions/{board['xid']}/leaderboard",
                           headers={**headers, "If-None-Match": etag})
        assert again.status_code == 304
        assert not again.content

    def test_a_changed_board_is_a_200_with_a_new_etag(self, client, db, board,
                                                      published):
        headers = auth(published["student"].xid)
        etag = client.get(f"/api/v1/competitions/{board['xid']}/leaderboard",
                          headers=headers).headers["ETag"]
        _result(db, board, _student(db, "Kamola"), raw=35, band=8.0,
                duration_ms=800_000, rank=1)
        db.execute(text("UPDATE competition_results SET rank = 2 WHERE raw_score = 30"))
        db.flush()
        again = client.get(f"/api/v1/competitions/{board['xid']}/leaderboard",
                           headers={**headers, "If-None-Match": etag})
        assert again.status_code == 200
        assert again.headers["ETag"] != etag

    def test_two_views_that_return_different_rows_get_different_etags(
            self, client, db, published):
        """Otherwise a client switching between the top and its own neighbourhood
        is served a 304 for the other one."""
        contest = _competition(db, published, status="final")
        _result(db, contest, {"id": published["student"].id}, raw=20, band=6.0,
                duration_ms=900_000, rank=8)
        for i in range(9):
            _result(db, contest, _student(db, f"Rival{i}"), raw=40 - i, band=7.0,
                    duration_ms=900_000, rank=i + 1 if i < 7 else i + 2)
        headers = auth(published["student"].xid)
        top = client.get(f"/api/v1/competitions/{contest['xid']}/leaderboard?limit=3",
                         headers=headers)
        near = client.get(
            f"/api/v1/competitions/{contest['xid']}/leaderboard?limit=3&around_me=true",
            headers=headers)
        assert [e["rank"] for e in top.json()["entries"]] == [1, 2, 3]
        assert 8 in [e["rank"] for e in near.json()["entries"]]
        assert top.headers["ETag"] != near.headers["ETag"]

    def test_the_tag_is_over_the_body_not_the_query(self, client, board, published):
        """A one-row board looks the same from either view, so both may reuse the
        same cached copy. The tag describes what was sent, not what was asked."""
        headers = auth(published["student"].xid)
        top = client.get(f"/api/v1/competitions/{board['xid']}/leaderboard?limit=50",
                         headers=headers)
        near = client.get(
            f"/api/v1/competitions/{board['xid']}/leaderboard?limit=50&around_me=true",
            headers=headers)
        assert top.json()["entries"] == near.json()["entries"]
        assert top.headers["ETag"] == near.headers["ETag"]

    def test_a_live_board_is_not_cached_by_proxies(self, client, db, published):
        """Provisional means it changes under you. Only a final board gets a
        `Cache-Control` lifetime."""
        contest = _competition(db, published, status="live")
        response = client.get(f"/api/v1/competitions/{contest['xid']}/leaderboard",
                              headers=auth(published["student"].xid))
        assert response.json()["is_provisional"] is True
        assert "max-age" not in response.headers.get("Cache-Control", "")


class TestResults:
    def test_an_unfinished_contest_has_no_results(self, client, db, published):
        contest = _competition(db, published, status="live")
        refused = client.get(f"/api/v1/competitions/{contest['xid']}/results",
                             headers=auth(published["student"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "not_finished"

    def test_a_final_contest_returns_the_board(self, client, db, published):
        contest = _competition(db, published, status="final")
        _result(db, contest, _student(db, "Jasur"), raw=30, band=7.0,
                duration_ms=900_000, rank=1)
        response = client.get(f"/api/v1/competitions/{contest['xid']}/results",
                              headers=auth(published["student"].xid))
        assert response.status_code == 200, response.text
        assert response.json()["is_provisional"] is False
        assert response.json()["entries"][0]["rank"] == 1


# ── the regrade decision ─────────────────────────────────────────────

class TestRegradeDecision:
    """"A leaderboard that changes by itself looks like fraud." The decision is
    the governance record that makes a change attributable."""

    @pytest.fixture
    def job(self, db, published):
        contest = _competition(db, published, status="final")
        job_xid = db.scalar(text("""
            INSERT INTO regrade_jobs (trigger, subject_type, subject_id,
                                      initiated_by, reason, dry_run, status,
                                      competition_impact)
            VALUES ('answer_key_change', 'question_version', :qv, :u,
                    'Question 12 key was wrong', false, 'completed',
                    '{"rank_changes": 3, "podium_changes": 1}'::jsonb)
            RETURNING xid
        """).bindparams(u=published["author"].id,
                        qv=published["question_versions"][0].id))
        db.flush()
        return contest, job_xid

    def test_a_student_cannot_decide_one(self, client, job, published):
        contest, job_xid = job
        refused = client.post(
            f"/api/v1/competitions/{contest['xid']}/regrade-decisions/{job_xid}",
            headers=auth(published["student"].xid),
            json={"decision": "leave_as_is", "rationale": "no"})
        assert refused.status_code == 403
        assert refused.json()["code"] == "admin_only"

    def test_republishing_requires_a_public_notice(self, client, admin, job):
        """"If a podium moves, the people on it are told, in writing." """
        contest, job_xid = job
        refused = client.post(
            f"/api/v1/competitions/{contest['xid']}/regrade-decisions/{job_xid}",
            headers=admin,
            json={"decision": "regrade_and_republish", "rationale": "bad key"})
        assert refused.status_code == 409
        assert refused.json()["code"] == "public_notice_required"

    def test_leaving_it_alone_is_recorded_with_its_reason(self, client, db, admin,
                                                          job):
        contest, job_xid = job
        response = client.post(
            f"/api/v1/competitions/{contest['xid']}/regrade-decisions/{job_xid}",
            headers=admin,
            json={"decision": "leave_as_is", "rationale": "one mark, no rank change"})
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "leave_as_is"
        assert response.json()["impact"]["podium_changes"] == 1
        audit = db.execute(text("""
            SELECT after, reason FROM audit_log
            WHERE action = 'competition.regrade_decision'
        """)).mappings().one()
        assert audit["reason"] == "one mark, no rank change"
        assert audit["after"]["decision"] == "leave_as_is"

    def test_republishing_emits_the_outbox_event(self, client, db, admin, job):
        """The republish itself is a worker's job. What the endpoint owes is a
        durable instruction to do it, in the same transaction as the decision."""
        contest, job_xid = job
        response = client.post(
            f"/api/v1/competitions/{contest['xid']}/regrade-decisions/{job_xid}",
            headers=admin,
            json={"decision": "regrade_and_republish", "rationale": "key was wrong",
                  "public_notice": "Question 12 was re-marked."})
        assert response.status_code == 200, response.text
        row = db.execute(text("""
            SELECT event_type, payload FROM outbox
            WHERE event_type = 'competition.regrade_approved'
        """)).mappings().one()
        assert row["payload"]["public_notice"] == "Question 12 was re-marked."

    def test_deciding_twice_overwrites_rather_than_duplicating(self, client, db,
                                                               admin, job):
        contest, job_xid = job
        url = f"/api/v1/competitions/{contest['xid']}/regrade-decisions/{job_xid}"
        client.post(url, headers=admin,
                    json={"decision": "leave_as_is", "rationale": "first look"})
        again = client.post(url, headers=admin,
                            json={"decision": "regrade_and_republish",
                                  "rationale": "second look",
                                  "public_notice": "Corrected."})
        assert again.status_code == 200, again.text
        assert db.scalar(text(
            "SELECT count(*) FROM competition_regrade_decisions")) == 1
        assert db.scalar(text(
            "SELECT decision FROM competition_regrade_decisions")) \
            == "regrade_and_republish"

    def test_an_unknown_job_is_a_404(self, client, admin, job):
        contest, _ = job
        assert client.post(
            f"/api/v1/competitions/{contest['xid']}/regrade-decisions/{uuid.uuid4()}",
            headers=admin,
            json={"decision": "leave_as_is", "rationale": "x"}).status_code == 404
