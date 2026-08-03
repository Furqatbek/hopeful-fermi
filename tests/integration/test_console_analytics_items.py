"""The two console screens that find a broken question, driven as the console
drives them.

`test_analytics_endpoints.py` already proves the arithmetic: that a p-value is a
p-value, that `common_wrong` is ordered by frequency, that a bad key shows as a
negative point-biserial. What is not covered is the sequence a person actually
performs — pick a test, pick a version, read the analysis, switch the scope —
and the shape the screen has to render from once it gets there.

Three of the assertions below exist because the SCREEN cannot work without them
and the contract does not promise them:

  * `common_wrong` arrives with a count and no share. `stats.analyse` computes a
    share and the DTO drops it, so the console derives one from `n_responses`.
    Ten students writing the same thing is a missing key alternative out of
    twenty-five and noise out of four hundred, and the count alone cannot tell
    those apart.
  * The response is ordered by question number, and `/content/flagged-items` by
    ascending p-value. Neither puts a negative correlation first, which is the
    one finding on the page that is almost always a wrong key.
  * A class below twenty responses carries real numbers and `flagged: false`,
    because `MIN_RESPONSES` suppresses the flag and not the statistics. A centre
    running classes of fifteen is under that line on every item it ever sits, so
    a console that rendered `flagged` alone would show it a clean paper for ever.

`_sit` is imported from `test_analytics_endpoints` rather than reinvented: it
writes score rows the way the scoring engine leaves them, and a second fixture
that drifted from it would test a shape the product never produces.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid as _uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token
from tests.integration.test_analytics_endpoints import _sit


@pytest.fixture
def client(db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


def _ok(response, *expected):
    assert response.status_code in (expected or (200,)), response.text
    return response.json()


@pytest.fixture
def rival(db):
    """A centre_admin at a different organization. Content defaults to
    `org_private`, and that is a contractual promise, not a preference."""
    from app.modules.identity.models import Organization, OrgMembership, User

    org = Organization(name="Rival Centre", slug=f"rv-{_uuid.uuid4().hex[:6]}",
                       status="active")
    user = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name="Rustam",
                date_of_birth=dt.date(1985, 1, 1))
    db.add_all([org, user])
    db.flush()
    db.add(OrgMembership(org_id=org.id, user_id=user.id, role="centre_admin",
                         status="active"))
    db.flush()
    return auth(user.xid)


def _bad_key(db, seed, published, *, item=0, sitters=12, time_ms=None):
    """The scenario the screen exists for: the key only accepts `bike`, and the
    students who knew the answer wrote `bicycle`.

    Strong students (total 3 of 3) are marked WRONG; weak ones (total 0) guessed
    the accepted word and are marked right. That is a negative point-biserial,
    which is what a wrong key looks like from the outside.
    """
    _sit(db, seed, published, [("bicycle", "incorrect")] * sitters, item=item,
         raw_score=lambda n, v: 3, time_ms=time_ms)
    _sit(db, seed, published, [("bike", "correct")] * sitters, item=item,
         raw_score=lambda n, v: 0, time_ms=time_ms)


def _sound(db, seed, published, *, item=1, sitters=12):
    """The same paper's healthy item: the strong students get it right."""
    _sit(db, seed, published, [("bike", "correct")] * sitters, item=item,
         raw_score=lambda n, v: 3)
    _sit(db, seed, published, [("kayak", "incorrect")] * sitters, item=item,
         raw_score=lambda n, v: 0)


class TestTheItemAnalysisScreenReachesAPaper:
    def test_the_pickers_walk_a_test_to_a_version_that_has_been_sat(
            self, client, db, seed, published):
        """The screen's opening sequence, in order: list the tests, list that
        test's versions, analyse the published one. The version picker opens on
        `published` because a draft has never been sat and analyses to an empty
        screen — which reads as a paper with nothing wrong with it."""
        _bad_key(db, seed, published)

        tests = _ok(client.get("/api/v1/tests?limit=50",
                               headers=auth(seed["author"].xid)))
        mine = [t for t in tests["items"] if t["title"] == "Mock 1"]
        assert mine, "the seeded centre's own test is not in its library"

        versions = _ok(client.get(f"/api/v1/tests/{mine[0]['xid']}/versions",
                                  headers=auth(seed["author"].xid)))
        publishable = [v for v in versions if v["status"] == "published"]
        assert publishable, "nothing to open the analysis on"

        body = _ok(client.get(
            f"/api/v1/test-versions/{publishable[0]['xid']}"
            "/item-analysis?org_scope=mine", headers=auth(seed["author"].xid)))
        assert body["n_attempts"] == 24
        assert body["items"]


class TestWhatTheItemAnalysisScreenDraws:
    def _items(self, client, seed, published, *, scope="mine", who=None):
        return _ok(client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}"
            f"/item-analysis?org_scope={scope}",
            headers=auth(who or seed["author"].xid)))["items"]

    def test_a_bad_key_arrives_as_a_negative_correlation(self, client, db, seed,
                                                         published):
        """Every field the finding block renders, in one response."""
        _bad_key(db, seed, published, time_ms=51_000)
        item = self._items(client, seed, published)[0]
        assert item["discrimination"] is not None and item["discrimination"] < 0
        assert item["p_value"] == pytest.approx(0.5)
        assert item["n_responses"] == 24
        assert item["mean_time_ms"] == 51_000
        assert item["common_wrong"][0]["value"] == "bicycle"
        assert item["flag_reasons"] == ["negative_discrimination",
                                        "common_wrong_answer"]

    def test_common_wrong_arrives_without_the_share_the_screen_needs(
            self, client, db, seed, published):
        """`stats.analyse` puts a `share` on every entry and `_item_dto` keeps
        only `value` and `count`. The console derives the share from
        `n_responses`, so both of those have to be here — and the day the DTO
        starts sending a share, this is where the duplication shows up."""
        _bad_key(db, seed, published)
        item = self._items(client, seed, published)[0]
        assert item["common_wrong"] == [{"value": "bicycle", "count": 12}]
        assert item["n_responses"] == 24

    def test_the_response_is_ordered_by_question_number_not_by_concern(
            self, client, db, seed, published):
        """Which is why the screen reorders. Question 1 is sound and question 2
        has a wrong key; in paper order the finding is second, and on a
        forty-question paper it is thirty-eighth."""
        _sound(db, seed, published, item=0)
        _bad_key(db, seed, published, item=1)
        items = self._items(client, seed, published)
        assert [i["number"] for i in items] == [1, 2]
        assert items[0]["discrimination"] > 0
        assert items[1]["discrimination"] < 0

    def test_a_class_too_small_to_flag_still_carries_its_numbers(
            self, client, db, seed, published):
        """`MIN_RESPONSES` suppresses the FLAG, not the statistics. A centre
        running a class of fifteen is under that line on every item of every
        mock it ever sets, and a console that rendered `flagged` alone would
        tell it the paper is clean for ever."""
        _bad_key(db, seed, published, sitters=7)
        item = self._items(client, seed, published)[0]
        assert item["n_responses"] == 14
        assert item["flagged"] is False
        assert item["flag_reasons"] == []
        assert item["discrimination"] < 0
        assert item["common_wrong"][0]["count"] == 7

    def test_the_scope_toggle_changes_the_numbers(self, client, db, seed,
                                                  published):
        """"My centre" and "every centre" are different questions. A sitting
        with no organization behind it is self-serve practice and belongs to
        neither centre, so it appears only in the wider count."""
        _bad_key(db, seed, published)
        _sit(db, seed, published, [("bike", "correct")] * 6, org=None)
        assert self._items(client, seed, published, scope="mine")[0]["n_responses"] == 24
        assert self._items(client, seed, published, scope="global")[0]["n_responses"] == 30

    def test_a_version_nobody_has_sat_is_an_empty_list_not_an_error(
            self, client, seed, published):
        """The empty state the screen renders as "nobody has sat this version",
        with the hint to widen the scope. A 404 or a 500 here would read as a
        broken screen on a paper that is simply new."""
        body = _ok(client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}"
            "/item-analysis?org_scope=mine", headers=auth(seed["author"].xid)))
        assert body["items"] == []
        assert body["n_attempts"] == 0

    def test_an_unknown_version_is_a_404(self, client, seed):
        assert client.get(
            f"/api/v1/test-versions/{_uuid.uuid4()}/item-analysis?org_scope=mine",
            headers=auth(seed["author"].xid)).status_code == 404


class TestWhoMayOpenItemAnalysis:
    """This screen is staff only, and the reason is on the screen itself:
    `common_wrong` is a list of what other students typed, and a p-value is a map
    of which questions to spend revision time on."""

    def test_a_student_at_the_centre_is_refused(self, client, db, seed, published):
        _bad_key(db, seed, published)
        refused = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}"
            "/item-analysis?org_scope=mine", headers=auth(seed["student"].xid))
        assert refused.status_code == 403, refused.text
        assert "bicycle" not in refused.text

    def test_a_rival_centre_cannot_even_see_the_paper_exists(
            self, client, db, seed, published, rival):
        """404 rather than 403. Whether a competitor has a paper called Mock 1
        is theirs to know."""
        _bad_key(db, seed, published)
        refused = client.get(
            f"/api/v1/test-versions/{published['test_version'].xid}"
            "/item-analysis?org_scope=global", headers=rival)
        assert refused.status_code == 404, refused.text
        assert "bicycle" not in refused.text


class TestTheFlaggedItemsScreen:
    """The cross-test dashboard, which reads the ninety-day `item_stats`
    projection rather than computing anything."""

    def _rows(self, client, headers):
        return _ok(client.get("/api/v1/content/flagged-items", headers=headers))

    def _flag(self, db, published, *, reasons, item=0, p_value=0.0,
              discrimination=None, common_wrong=()):
        qv = published["question_versions"][item]
        db.execute(text("""
            INSERT INTO item_stats (question_id, question_version_id, org_id,
                                    window_start, window_end, n_responses, n_correct,
                                    p_value, discrimination, mean_time_ms,
                                    option_distribution, common_wrong, flagged,
                                    flag_reasons)
            VALUES (:q, :qv, NULL, DATE '2026-05-01', DATE '2026-07-30', 30, 0,
                    :p, :d, NULL, '{}'::jsonb, CAST(:cw AS jsonb), true,
                    CAST(:reasons AS text[]))
        """).bindparams(q=qv.question_id, qv=qv.id, p=p_value, d=discrimination,
                        cw=json.dumps(list(common_wrong)), reasons=list(reasons)))
        db.flush()

    def test_the_projection_sweep_puts_a_real_bad_key_on_the_list(
            self, client, db, seed, published):
        """Built by running the real sweep over real sittings rather than by
        inserting a row, so the projection is exercised as well as the endpoint.
        Every column the screen renders is asserted here."""
        from app.modules.analytics import projections

        _bad_key(db, seed, published)
        projections.refresh_item_stats(db, now=dt.datetime.now(dt.UTC))
        db.flush()

        rows = self._rows(client, auth(seed["author"].xid))
        assert rows, "a wrong key sat by 24 students is not on the flagged list"
        # The endpoint returns a question once per statistics SCOPE — the
        # projection writes a centre row and a platform row for the same sitting
        # and this query filters on neither — so the row count is not the
        # question count. What the console needs is that every row for one
        # question names that question, which is what it de-duplicates on.
        assert len({r["question_xid"] for r in rows}) == 1
        row = rows[0]
        assert row["test_title"] == "Mock 1"
        assert row["number"] == 1
        assert row["discrimination"] < 0
        assert row["suggested_action"] == "review_key"
        assert row["common_wrong"][0]["value"] == "bicycle"
        assert "negative_discrimination" in row["flag_reasons"]

    def test_the_server_orders_by_difficulty_so_the_screen_must_reorder(
            self, client, db, seed, published):
        """`ORDER BY s.p_value NULLS LAST`. An item nobody could answer outranks
        an item the strong students got wrong — and the second is the one that is
        almost always a wrong key. The console sorts by concern instead."""
        self._flag(db, published, item=0, p_value=0.6, discrimination=-0.7,
                   reasons=["negative_discrimination"])
        self._flag(db, published, item=1, p_value=0.02, discrimination=0.1,
                   reasons=["near_zero_p"])
        rows = self._rows(client, auth(seed["author"].xid))
        assert [r["p_value"] for r in rows] == [0.02, 0.6]
        assert rows[0]["discrimination"] > 0, "the finding is second"

    def test_the_action_column_distinguishes_the_cases(self, client, db, seed,
                                                       published):
        """The column that says what to DO. Four reasons, four answers — the
        screen renders it verbatim rather than forming a second opinion."""
        self._flag(db, published, item=0, reasons=["near_one_p"], p_value=1.0)
        assert self._rows(client, auth(seed["author"].xid))[0][
            "suggested_action"] == "retire_too_easy"

    def test_a_question_on_no_paper_still_lists(self, client, db, seed, published):
        """Rendered as "not on a paper". A question flagged from practice use and
        never placed on a mock must not vanish from the dashboard that exists to
        find broken questions — and `""` with a `0` is what arrives, not null."""
        db.execute(text("DELETE FROM test_version_groups"))
        db.flush()
        self._flag(db, published, reasons=["near_zero_p"])
        row = self._rows(client, auth(seed["author"].xid))[0]
        assert row["test_title"] == ""
        assert row["number"] == 0

    def test_a_rival_centres_broken_question_does_not_appear(
            self, client, db, seed, published, rival):
        """Content defaults to `org_private` and this list is what a competitor
        would learn the most from: which of our questions are weak, and what our
        students type into them."""
        self._flag(db, published, reasons=["negative_discrimination"],
                   discrimination=-0.6,
                   common_wrong=[{"value": "bicycle", "count": 18, "share": 0.6}])
        assert self._rows(client, auth(seed["author"].xid)), "the owner sees it"
        assert self._rows(client, rival) == []

    def test_nothing_flagged_is_an_empty_list_not_an_error(self, client, seed):
        """The empty state, which is also the state of a centre whose sweep has
        not run yet — the screen says so rather than claiming the bank is clean."""
        assert self._rows(client, auth(seed["author"].xid)) == []
