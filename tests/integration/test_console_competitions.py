"""The competitions console, and the two rules that make a ranking mean anything.

Pillar three had no screen at all — no way to schedule a contest, read a board,
or record the decision that a blocked regrade waits for. Every request below is
one a screen makes.
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


def _member(db, seed, name, role):
    from app.modules.identity.models import OrgMembership, User

    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name=name,
                date_of_birth=dt.date(1990, 1, 1))
    db.add(user)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=user.id, role=role,
                         status="active"))
    db.flush()
    return user


@pytest.fixture
def platform_admin(db):
    from app.modules.identity.models import PlatformRoleGrant, User

    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name="Olim",
                date_of_birth=dt.date(1985, 1, 1))
    db.add(user)
    db.flush()
    db.add(PlatformRoleGrant(user_id=user.id, role="platform_admin",
                             granted_by=user.id))
    db.flush()
    return user


def _schedule(client, headers, published, **overrides):
    now = dt.datetime.now(dt.UTC)
    body = {"title": "Winter Open",
            "test_version_xid": str(published["test_version"].xid),
            "starts_at": (now + dt.timedelta(days=1)).isoformat(),
            "duration_seconds": 3600, "visibility": "org",
            "tiebreak": ["raw_score_desc", "duration_asc", "submitted_at_asc"]}
    body.update(overrides)
    return client.post("/api/v1/competitions", headers=headers, json=body)


class TestSchedulingOne:
    def test_the_form_body_the_screen_sends(self, client, seed, published):
        contest = _ok(_schedule(client, auth(seed["author"].xid), published), 201)
        assert contest["title"] == "Winter Open"
        # `registration`, not `scheduled`: with no `registration_closes_at` the
        # contest opens for entry immediately. Worth knowing because it is what
        # the status column shows the organiser the moment they create one.
        assert contest["status"] == "registration"
        # The lobby is set SERVER-side to starts_at - 120s. The screen does not
        # offer it: that window is what turns two hundred simultaneous payload
        # downloads into a trickle, and it is not a number an organiser invents.
        lobby = dt.datetime.fromisoformat(contest["lobby_opens_at"])
        starts = dt.datetime.fromisoformat(contest["starts_at"])
        assert (starts - lobby).total_seconds() == 120

    def test_only_a_published_paper_can_back_one(self, client, seed):
        """The screen offers only tests carrying a published version."""
        refused = client.post("/api/v1/competitions", headers=auth(seed["author"].xid),
                              json={"title": "Draft contest",
                                    "test_version_xid": str(seed["test_version"].xid),
                                    "starts_at": (dt.datetime.now(dt.UTC)
                                                  + dt.timedelta(days=1)).isoformat(),
                                    "duration_seconds": 3600, "visibility": "org"})
        assert refused.status_code == 409
        assert refused.json()["code"] == "version_not_published"

    def test_a_centre_may_not_run_a_public_contest(self, client, seed, published):
        """`public` is offered in the picker only to a platform admin. A centre
        admin choosing it gets a control that can never work."""
        refused = _schedule(client, auth(seed["author"].xid), published,
                            visibility="public")
        assert refused.status_code == 403
        assert refused.json()["code"] == "admin_only"

    def test_a_platform_admin_may(self, client, seed, published, platform_admin):
        _ok(_schedule(client, auth(platform_admin.xid), published,
                      visibility="public"), 201)


class TestAPaperIsSpentOnce:
    """A contest is a RANKING. A ranking computed over a field where some
    entrants have already seen the paper is not a slightly wrong number — it is a
    number that means nothing, published under the platform's name."""

    def test_a_second_contest_on_the_same_paper_is_refused(
            self, client, seed, published):
        _ok(_schedule(client, auth(seed["author"].xid), published), 201)
        refused = _schedule(client, auth(seed["author"].xid), published,
                            title="Spring Open")
        assert refused.status_code == 409
        assert refused.json()["code"] == "paper_already_contested"
        # The screen names the other contest, because the remedy is to pick a
        # different paper and the organiser needs to know which one is spent.
        assert refused.json()["contest"] == "Winter Open"

    def test_the_refusal_has_no_override(self, client, seed, published):
        """No force flag exists, and the screen does not look for one."""
        _ok(_schedule(client, auth(seed["author"].xid), published), 201)
        for attempt in ({"force": True}, {"visibility": "invite"}):
            refused = _schedule(client, auth(seed["author"].xid), published,
                                title="Sneaky", **attempt)
            assert refused.status_code == 409


class TestTheBoardTheScreenReads:
    @pytest.fixture
    def ranked(self, client, db, seed, published):
        contest = _ok(_schedule(client, auth(seed["author"].xid), published), 201)
        row = db.execute(text("SELECT id FROM competitions WHERE xid = CAST(:x AS uuid)")
                         .bindparams(x=contest["xid"])).scalar()
        for rank, (name, score) in enumerate(
                [("Aziza", 38), ("Bek", 35), ("Dilnoza", 31)], start=1):
            user = _member(db, seed, name, "student")
            attempt = db.scalar(text("""
                INSERT INTO attempts (user_id, test_version_id, competition_id, mode,
                                      status, started_at, submitted_at)
                VALUES (:u, :v, :c, 'exam', 'scored', now(), now())
                RETURNING id
            """).bindparams(u=user.id, v=seed["test_version"].id, c=row))
            run = db.scalar(text("""
                INSERT INTO score_runs (attempt_id, reason, engine_version,
                                        key_versions, raw_score, max_raw, band,
                                        is_current)
                VALUES (:a, 'initial', '1.0.0', '{}'::jsonb, :s, 40, 7.0, true)
                RETURNING id
            """).bindparams(a=attempt, s=score))
            db.execute(text("""
                INSERT INTO competition_results (competition_id, user_id, attempt_id,
                                                 score_run_id, rank, raw_score, band,
                                                 duration_ms, submitted_at,
                                                 is_provisional)
                VALUES (:c, :u, :a, :r, :rank, :s, 7.0, :d, now(), false)
            """).bindparams(c=row, u=user.id, a=attempt, r=run, rank=rank, s=score,
                            d=1_200_000 + rank * 1000))
        db.execute(text("UPDATE competitions SET status = 'final' WHERE id = :c")
                   .bindparams(c=row))
        db.flush()
        return contest

    def test_final_results_are_read_in_rank_order(self, client, seed, ranked):
        board = _ok(client.get(f"/api/v1/competitions/{ranked['xid']}/results",
                               headers=auth(seed["author"].xid)))
        assert [e["rank"] for e in board["entries"]] == [1, 2, 3]
        assert [e["raw_score"] for e in board["entries"]] == [38, 35, 31]

    def test_the_board_carries_a_display_name_and_nothing_else(
            self, client, seed, ranked):
        """The most-screenshotted surface in the product. It must never carry an
        age, a phone number or a centre name."""
        board = _ok(client.get(f"/api/v1/competitions/{ranked['xid']}/results",
                               headers=auth(seed["author"].xid)))
        for entry in board["entries"]:
            assert set(entry["user"]) <= {"xid", "display_name"}
        assert "phone" not in str(board)

    def test_a_finished_board_is_not_provisional(self, client, seed, ranked):
        """The screen polls a live board every ten seconds and leaves a finished
        one alone — it reads a durable table that does not change."""
        board = _ok(client.get(f"/api/v1/competitions/{ranked['xid']}/results",
                               headers=auth(seed["author"].xid)))
        assert board["is_provisional"] is False


class TestTheDecisionThatUnblocksARegrade:
    """`apply` refuses a regrade touching a finished contest until somebody
    records a decision. Until this screen there was nowhere to record one, so the
    Regrades panel named a requirement the console could not satisfy."""

    @pytest.fixture
    def blocked(self, client, db, seed, published, platform_admin):
        """A finished contest, and a staged regrade that touches it."""
        from app.modules.billing.models import EntitlementRow

        contest = _ok(_schedule(client, auth(seed["author"].xid), published), 201)
        competition_id = db.execute(
            text("SELECT id FROM competitions WHERE xid = CAST(:x AS uuid)")
            .bindparams(x=contest["xid"])).scalar()
        db.execute(text("UPDATE competitions SET status = 'final' WHERE id = :c")
                   .bindparams(c=competition_id))

        student = _member(db, seed, "Aziza", "student")
        db.add(EntitlementRow(subject_kind="user", subject_id=student.id,
                              feature="mock.unlimited", source_kind="order",
                              starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)))
        db.flush()
        h = auth(student.xid)
        attempt = _ok(client.post("/api/v1/attempts", headers=h, json={
            "test_version_xid": str(published["test_version"].xid)}), 201)["xid"]
        paper = _ok(client.get(f"/api/v1/attempts/{attempt}/payload", headers=h))
        q = paper["sections"][0]["groups"][0]["questions"][0]
        _ok(client.post(f"/api/v1/attempts/{attempt}/answers", headers=h, json={
            "deltas": [{"question_version_xid": q["question_version_xid"],
                        "slot_key": q["slot_keys"][0],
                        "response": "map", "client_seq": 1}]}))
        _ok(client.post(f"/api/v1/attempts/{attempt}/submit", headers=h))
        db.execute(text("UPDATE attempts SET competition_id = :c WHERE xid = CAST(:x AS uuid)")
                   .bindparams(c=competition_id, x=attempt))
        db.flush()

        job_xid = _ok(client.post(
            f"/api/v1/question-versions/{q['question_version_xid']}/keys",
            headers=auth(seed["author"].xid),
            json={"key": {"slots": {"s1": {"accept": ["map", "chart"]}}},
                  "reason": "key_fix"}), 201)["regrade_job_xid"]
        db.execute(text("UPDATE regrade_jobs SET status = 'ready' "
                        "WHERE xid = CAST(:x AS uuid)").bindparams(x=job_xid))
        db.flush()
        return {"contest": contest, "job_xid": job_xid}

    def test_apply_is_blocked_until_a_decision_exists(
            self, client, seed, blocked):
        refused = client.post(f"/api/v1/regrades/{blocked['job_xid']}/apply",
                              headers=auth(seed["author"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "competition_decision_required"

    def test_a_centre_admin_cannot_decide(self, client, db, seed, blocked):
        """The centre whose students are ranked is not the party to decide
        whether their ranking moves. The screen says who can rather than offering
        a control that refuses."""
        admin = _member(db, seed, "Rustam", "centre_admin")
        refused = client.post(
            f"/api/v1/competitions/{blocked['contest']['xid']}"
            f"/regrade-decisions/{blocked['job_xid']}",
            headers=auth(admin.xid),
            json={"decision": "leave_as_is", "rationale": "No podium change"})
        assert refused.status_code == 403
        assert refused.json()["code"] == "admin_only"

    def test_republishing_without_a_public_notice_is_refused(
            self, client, blocked, platform_admin):
        """If a podium moves, the people on it are told in writing. A ranking
        that changes silently is worse than one that was wrong."""
        refused = client.post(
            f"/api/v1/competitions/{blocked['contest']['xid']}"
            f"/regrade-decisions/{blocked['job_xid']}",
            headers=auth(platform_admin.xid),
            json={"decision": "regrade_and_republish", "rationale": "Key was wrong"})
        assert refused.status_code == 409
        assert refused.json()["code"] == "public_notice_required"

    def test_leave_as_is_records_who_decided_and_unblocks_apply(
            self, client, seed, blocked, platform_admin):
        decided = _ok(client.post(
            f"/api/v1/competitions/{blocked['contest']['xid']}"
            f"/regrade-decisions/{blocked['job_xid']}",
            headers=auth(platform_admin.xid),
            json={"decision": "leave_as_is",
                  "rationale": "Two ranks move below the podium; the top three "
                               "are unchanged."}))
        assert decided["decision"] == "leave_as_is"
        assert decided["decided_by"]["given_name"] == "Olim"
        assert "podium" in decided["rationale"]

        applied = client.post(f"/api/v1/regrades/{blocked['job_xid']}/apply",
                              headers=auth(seed["author"].xid))
        assert applied.status_code == 202

    def test_the_decision_is_written_to_the_audit_log(
            self, client, db, blocked, platform_admin):
        """An audit record names the human who chose."""
        _ok(client.post(
            f"/api/v1/competitions/{blocked['contest']['xid']}"
            f"/regrade-decisions/{blocked['job_xid']}",
            headers=auth(platform_admin.xid),
            json={"decision": "regrade_and_republish",
                  "rationale": "The key was wrong and the podium moves.",
                  "public_notice": "An answer key was corrected after this "
                                   "contest; results have been recomputed."}))
        rows = db.execute(text("""
            SELECT actor_user_id, action FROM audit_log
             WHERE action LIKE 'competition%' OR action LIKE '%regrade%'
        """)).mappings().all()
        assert any(r["actor_user_id"] == platform_admin.id for r in rows), rows
