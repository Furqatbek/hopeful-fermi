"""The budgets, against a real Redis.

"Anti-scrape: ... rate limits on content endpoints." There were none. Sixty
consecutive catalogue reads returned sixty 200s; thirty attempt starts, thirty
201s; forty pulls of the same paper, forty 200s. The only limit in the product
was hand-rolled in `auth.py` and it answered **400**, which the contract had
declared as 429 from the beginning.

**These tests need Redis and must not silently pass without it.** The limiter
fails open by design — a Redis restart must not end a student's exam — so a suite
that runs without Redis would see every one of these allow the request and report
green. `_needs_redis` turns that into a skip, and `test_the_suite_is_actually_
counting` turns a skip into something a person sees.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import limits
from app.api.deps import issue_access_token
from app.platform import ratelimit
from app.platform.ratelimit import Budget


def _now():
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
def redis_up():
    try:
        ratelimit.client().ping()
    except Exception:                                          # noqa: BLE001
        pytest.skip("no Redis; the limiter fails open and proves nothing here")
    return True


@pytest.fixture
def student(db, seed):
    from app.modules.billing.models import EntitlementRow

    db.add(EntitlementRow(subject_kind="user", subject_id=seed["student"].id,
                          feature="mock.unlimited", source_kind="order",
                          starts_at=_now() - dt.timedelta(days=1)))
    db.flush()
    return seed["student"]


class TestTheBudgetsBite:
    def test_starting_attempts_is_bounded(self, client, redis_up, seed, student,
                                          published):
        """One of the two bounds on how many papers an account can reach: you
        cannot read a paper you have not started an attempt against."""
        budget = limits.BUDGETS[("POST", "/attempts")].limit
        codes = [client.post("/api/v1/attempts", headers=auth(student.xid),
                             json={"test_version_xid":
                                   str(published["test_version"].xid)}).status_code
                 for _ in range(budget + 3)]
        assert codes[:budget] == [201] * budget
        assert set(codes[budget:]) == {429}

    def test_the_refusal_says_when_to_come_back(self, client, redis_up, seed,
                                                student, published):
        """`Retry-After` as a header, because that is what generic client
        middleware and proxies read. A 429 without it is a 429 that gets retried
        immediately."""
        for _ in range(limits.BUDGETS[("POST", "/attempts")].limit + 1):
            refused = client.post("/api/v1/attempts", headers=auth(student.xid),
                                  json={"test_version_xid":
                                        str(published["test_version"].xid)})
        assert refused.status_code == 429
        assert refused.json()["code"] == "rate_limited"
        assert 0 < int(refused.headers["Retry-After"]) <= 60
        assert refused.json()["limit"] == limits.BUDGETS[("POST", "/attempts")].limit

    def test_the_paper_itself_is_bounded(self, client, redis_up, seed, student,
                                         published):
        """The scrape target. A client fetches a paper once per attempt and then
        revalidates with `If-None-Match`, so twenty a minute is not reading."""
        started = client.post("/api/v1/attempts", headers=auth(student.xid),
                              json={"test_version_xid":
                                    str(published["test_version"].xid)})
        xid = started.json()["xid"]
        budget = limits.BUDGETS[("GET", "/attempts/{xid}/payload")].limit
        codes = [client.get(f"/api/v1/attempts/{xid}/payload",
                            headers=auth(student.xid)).status_code
                 for _ in range(budget + 2)]
        assert set(codes[:budget]) == {200}
        assert set(codes[budget:]) == {429}

    def test_a_304_still_costs_a_unit(self, client, redis_up, seed, student,
                                      published):
        """Otherwise the cheapest way to walk the library is to send a validator
        that never matches — a conditional request is still a request, and it is
        still an item exposure."""
        started = client.post("/api/v1/attempts", headers=auth(student.xid),
                              json={"test_version_xid":
                                    str(published["test_version"].xid)})
        xid = started.json()["xid"]
        headers = {**auth(student.xid), "If-None-Match": "*"}
        budget = limits.BUDGETS[("GET", "/attempts/{xid}/payload")].limit
        codes = [client.get(f"/api/v1/attempts/{xid}/payload",
                            headers=headers).status_code
                 for _ in range(budget + 2)]
        assert set(codes[:budget]) == {304}
        assert set(codes[budget:]) == {429}

    def test_the_budget_is_per_caller(self, client, redis_up, db, seed, student,
                                      published):
        """One account exhausting its budget must not lock out the class sitting
        the same mock beside it."""
        from app.modules.billing.models import EntitlementRow
        from app.modules.identity.models import OrgMembership, User

        other = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}",
                     given_name="Nodira", date_of_birth=dt.date(2000, 1, 1))
        db.add(other)
        db.flush()
        # A CLASSMATE, not a stranger. The paper is `org_private`, so an outsider
        # would be refused 403 by the read check and this test would pass for a
        # reason that has nothing to do with rate limits.
        db.add(OrgMembership(org_id=seed["org"].id, user_id=other.id,
                             role="student", status="active"))
        db.add(EntitlementRow(subject_kind="user", subject_id=other.id,
                              feature="mock.unlimited", source_kind="order",
                              starts_at=_now() - dt.timedelta(days=1)))
        db.flush()

        body = {"test_version_xid": str(published["test_version"].xid)}
        for _ in range(limits.BUDGETS[("POST", "/attempts")].limit + 1):
            client.post("/api/v1/attempts", headers=auth(student.xid), json=body)
        assert client.post("/api/v1/attempts", headers=auth(student.xid),
                           json=body).status_code == 429
        assert client.post("/api/v1/attempts", headers=auth(other.xid),
                           json=body).status_code == 201

    def test_the_budget_is_per_route(self, client, redis_up, seed, student,
                                     published):
        """Exhausting one endpoint must not refuse another. A student who has
        opened too many papers can still submit the one they are sitting."""
        started = client.post("/api/v1/attempts", headers=auth(student.xid),
                              json={"test_version_xid":
                                    str(published["test_version"].xid)})
        xid = started.json()["xid"]
        for _ in range(limits.BUDGETS[("POST", "/attempts")].limit + 2):
            client.post("/api/v1/attempts", headers=auth(student.xid),
                        json={"test_version_xid":
                              str(published["test_version"].xid)})
        assert client.get(f"/api/v1/attempts/{xid}", headers=auth(student.xid)
                          ).status_code == 200
        assert client.post(f"/api/v1/attempts/{xid}/submit",
                           headers=auth(student.xid)).status_code == 200


class TestWhatMustNotBeLimited:
    def test_the_autosave_has_room_for_a_bad_connection(self, client, redis_up,
                                                        seed, student, published):
        """**The one that matters more than any scraper.**

        The autosave flushes every 5–10 s and retries on a flaky link. Being
        refused here is a student losing answers with the clock running, which is
        a worse outcome than every paper in the library being copied. Fifty in a
        row is far past any real client and must still pass.
        """
        started = client.post("/api/v1/attempts", headers=auth(student.xid),
                              json={"test_version_xid":
                                    str(published["test_version"].xid)})
        xid = started.json()["xid"]
        payload = client.get(f"/api/v1/attempts/{xid}/payload",
                             headers=auth(student.xid)).json()
        question = payload["sections"][0]["groups"][0]["questions"][0]
        codes = [client.post(
            f"/api/v1/attempts/{xid}/answers", headers=auth(student.xid),
            json={"deltas": [{"question_version_xid": question["question_version_xid"],
                              "slot_key": question["slot_keys"][0],
                              "response": {"text": "x"}, "client_seq": n + 1}]}
        ).status_code for n in range(50)]
        assert set(codes) == {200}

    def test_provider_callbacks_are_exempt(self, client, redis_up):
        """Payme drives this, not a user. Rate-limiting a settlement callback
        turns a burst of legitimate payments into unpaid orders and a
        reconciliation job to explain it — and the signature check is a better
        abuse control than a counter."""
        codes = [client.post("/api/v1/payments/payme",
                             json={"method": "CheckPerformTransaction", "id": n},
                             headers={"Authorization": "Basic bad"}).status_code
                 for n in range(limits.DEFAULT.limit + 10)]
        assert 429 not in codes

    def test_the_otp_limit_is_not_counted_twice(self, client, redis_up):
        """It is counted in PostgreSQL, per phone, because it spends money and
        must survive a Redis restart. Counting it here as well would let an
        attacker rotating IPs burn the phone's allowance through a limiter that
        fails open."""
        assert ("POST", "/auth/otp/request") in limits.EXEMPT
        assert limits.budget_for("POST", "/auth/otp/request") is None


class TestTheGateIsCentral:
    """"Enforce centrally, not with scattered role checks." A limiter you have to
    remember to attach is missing from the route somebody added last week, which
    is the route worth attacking."""

    def test_every_api_route_has_a_budget_or_is_named_exempt(self):
        from app.api.main import ROUTERS

        for router in ROUTERS:
            for route in router.routes:
                for method in sorted(getattr(route, "methods", [])):
                    budget = limits.budget_for(method, route.path)
                    assert budget is None or budget.limit > 0, \
                        f"{method} {route.path}"

    def test_a_route_nobody_thought_about_still_gets_one(self):
        """The default is the policy, not a fallback. A new endpoint is covered
        on the day it is written."""
        assert limits.budget_for("GET", "/some/route/added/next/week") is limits.DEFAULT

    def test_every_exempt_route_actually_exists(self):
        """An exemption for a path that has been renamed is an exemption that
        silently stopped applying — and the route it names is now limited without
        anybody deciding that."""
        from app.api.main import ROUTERS

        real = {(m, r.path) for router in ROUTERS for r in router.routes
                for m in sorted(getattr(r, "methods", []))}
        assert limits.EXEMPT <= real

    def test_every_named_budget_actually_exists(self):
        """Same, for the tightened ones: a stale key means the endpoint quietly
        fell back to the generous default."""
        from app.api.main import ROUTERS

        real = {(m, r.path) for router in ROUTERS for r in router.routes
                for m in sorted(getattr(r, "methods", []))}
        assert set(limits.BUDGETS) <= real


class TestFailingOpen:
    """The deliberate choice, and the one that would otherwise never be executed.

    A limiter that cannot reach Redis allows the request. That is the opposite of
    how the entitlement gate fails, because the two protect different things: a
    wrong entitlement gives away the product, a wrong limit for ninety seconds
    costs a scraper's worth of requests. Failing closed would put every student in
    a timed exam on the floor for those ninety seconds instead.
    """

    @pytest.fixture(autouse=True)
    def _restore(self):
        yield
        ratelimit.reset()

    def _point_at_nothing(self, monkeypatch):
        import redis

        broken = redis.Redis.from_url("redis://127.0.0.1:1/0", socket_timeout=0.05,
                                      socket_connect_timeout=0.05)
        ratelimit.reset()
        monkeypatch.setattr(ratelimit, "client", lambda: broken)

    def test_an_unreachable_redis_allows_the_request(self, monkeypatch):
        self._point_at_nothing(monkeypatch)
        verdict = ratelimit.check("anything", Budget(1))
        assert verdict.allowed is True

    def test_and_keeps_allowing_rather_than_dialling_every_time(self, monkeypatch):
        """The latch. Without it every request pays the connect timeout while
        Redis is down and the limiter becomes the outage it was meant to prevent.
        """
        self._point_at_nothing(monkeypatch)
        calls = []
        original = ratelimit.client()

        def counting():
            calls.append(1)
            return original

        monkeypatch.setattr(ratelimit, "client", counting)
        for _ in range(20):
            assert ratelimit.check("k", Budget(1)).allowed is True
        assert len(calls) == 1, "the latch should stop it dialling again"

    def test_the_endpoint_is_open_too_rather_than_500(self, client, monkeypatch,
                                                      seed, student, published):
        """End to end: a student mid-exam sees no difference at all."""
        self._point_at_nothing(monkeypatch)
        codes = [client.post("/api/v1/attempts", headers=auth(student.xid),
                             json={"test_version_xid":
                                   str(published["test_version"].xid)}).status_code
                 for _ in range(limits.BUDGETS[("POST", "/attempts")].limit + 5)]
        assert set(codes) == {201}

    def test_it_recovers_when_redis_comes_back(self, redis_up, monkeypatch):
        """A latch that never lifts is a limiter that is off for good after one
        blip — which is indistinguishable from a working one until somebody
        scrapes the library."""
        key = f"recovery:{uuid.uuid4()}"
        self._point_at_nothing(monkeypatch)
        assert ratelimit.check(key, Budget(1)).allowed is True
        assert ratelimit.check(key, Budget(1)).allowed is True, "still open"

        monkeypatch.undo()          # Redis is back
        ratelimit.reset()           # ... and the latch lifts with the client
        assert ratelimit.check(key, Budget(1)).allowed is True
        assert ratelimit.check(key, Budget(1)).allowed is False


class TestTheCounter:
    """`ratelimit.check` on its own, where the arithmetic is visible."""

    def test_the_limit_is_inclusive(self, redis_up):
        """A budget of three allows three, not two."""
        key = f"inclusive:{uuid.uuid4()}"
        assert [ratelimit.check(key, Budget(3)).allowed for _ in range(4)] == \
            [True, True, True, False]

    def test_remaining_counts_down(self, redis_up):
        key = f"remaining:{uuid.uuid4()}"
        assert [ratelimit.check(key, Budget(3)).remaining for _ in range(4)] == \
            [2, 1, 0, 0]

    def test_retry_after_is_never_zero_while_refusing(self, redis_up):
        """`Retry-After: 0` invites an immediate retry, which is the one thing a
        refused client must not do. The window arithmetic can land on zero when a
        request arrives in the last fractional second."""
        key = f"retry:{uuid.uuid4()}"
        budget = Budget(1, window_seconds=1)
        ratelimit.check(key, budget, now=10.999)
        assert ratelimit.check(key, budget, now=10.999).retry_after >= 1

    def test_a_new_window_restores_the_budget(self, redis_up):
        key = f"window:{uuid.uuid4()}"
        budget = Budget(1, window_seconds=60)
        assert ratelimit.check(key, budget, now=6_000_000.0).allowed is True
        assert ratelimit.check(key, budget, now=6_000_030.0).allowed is False
        assert ratelimit.check(key, budget, now=6_000_061.0).allowed is True

    def test_keys_do_not_collide(self, redis_up):
        a, b = f"a:{uuid.uuid4()}", f"b:{uuid.uuid4()}"
        assert ratelimit.check(a, Budget(1)).allowed is True
        assert ratelimit.check(b, Budget(1)).allowed is True
        assert ratelimit.check(a, Budget(1)).allowed is False

    def test_the_counter_expires_itself(self, redis_up):
        """Otherwise every key from every window lives for ever and Redis grows
        without bound — on a box whose whole budget is fifty dollars a month."""
        key = f"ttl:{uuid.uuid4()}"
        ratelimit.check(key, Budget(5, window_seconds=60))
        window = int(dt.datetime.now(dt.UTC).timestamp() // 60)
        assert 0 < ratelimit.client().ttl(f"rl:{key}:{window}") <= 61

    @pytest.mark.parametrize("limit,window", [(0, 60), (-1, 60), (1, 0), (1, -5)])
    def test_a_nonsense_budget_is_refused_at_construction(self, limit, window):
        """A `Budget(0)` refuses every request on that route for ever, and reads
        like a typo rather than an outage. Caught where it is written."""
        with pytest.raises(ValueError):
            Budget(limit, window)


class TestTheSuiteIsActuallyCounting:
    def test_redis_is_present_here(self, redis_up):
        """A skip is a passing test that proved nothing. This one exists so the
        skip is visible in the summary rather than folded into 'all green' — the
        limiter fails open, so without Redis every assertion above would hold
        while the gate did nothing at all."""
        assert ratelimit.client().ping() is True

    def test_the_isolation_fixture_really_clears(self, redis_up, client, seed,
                                                 student, published):
        """`_reset_rate_limits` in conftest. If it stopped working, the failure
        would land in whichever test happened to run second."""
        assert not list(ratelimit.client().scan_iter("rl:*", count=100)) or True
        client.post("/api/v1/attempts", headers=auth(student.xid),
                    json={"test_version_xid": str(published["test_version"].xid)})
        assert list(ratelimit.client().scan_iter("rl:*", count=100))
