"""Profiles, consents, devices, organizations, cohorts and invites.

The last of the large routers at 70%. Two defects in the gap, and the first one
is the same mistake in the sibling of an endpoint that was already fixed:

  * **`GET /orgs/{xid}/members` returned every phone number to any member.**
    `list_cohort_members` was corrected to hand classmates names only; this one
    was not, and it is worse — the whole centre rather than one class, and it
    includes `is_minor`, which turns a directory into a targeting list.
  * **Accepting an invite you are already a member of returned 500 forever.**
    `org_memberships` is UNIQUE on `(org_id, user_id)`; the insert was
    unconditional, and because the failed transaction rolled back, the invite
    was never marked accepted, so every retry produced the same 500.

Everything else here was already right — notably parental consent, which is the
one place the brief's minor-safety rule is written down in this module.
"""

from __future__ import annotations

import datetime as dt
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


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


def _user(db, phone: str, name: str, *, dob: str = "1995-01-01",
          verified: bool = True):
    """`verified` is the SMS-code step, and it defaults to done.

    Every test that is about something else wants an ordinary signed-in account;
    the ones that are about the proof say so. Defaulting it the other way would
    have every unrelated test carrying a line of setup for a rule it is not
    testing, which is how setup stops being read.
    """
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status,
                           phone_verified_at)
        VALUES (:p, :n, CAST(:d AS date), 'active',
                CASE WHEN :v THEN now() END)
        RETURNING id, xid
    """).bindparams(p=phone, n=name, d=dob, v=verified)).mappings().one()
    db.flush()
    return row


def _verify(db, user_id: int) -> None:
    """What `POST /auth/otp/verify` does on success.

    `expire_all` because the request handler reads the same `User` through the
    same session: a raw UPDATE leaves the cached ORM object holding the old
    value, and the endpoint sees an unverified account it just verified.
    """
    db.execute(text("UPDATE users SET phone_verified_at = now() WHERE id = :u")
               .bindparams(u=user_id))
    db.flush()
    db.expire_all()


def _org_with_admin(client, db, slug: str, phone: str) -> dict:
    """A second, unrelated centre — the competitor in the isolation tests."""
    org = db.execute(text("""
        INSERT INTO organizations (name, slug, kind, status, created_by)
        VALUES (:n, :s, 'prep_centre', 'active',
                (SELECT id FROM users ORDER BY id LIMIT 1))
        RETURNING id, xid
    """).bindparams(n=slug, s=slug)).mappings().one()
    admin = _member(db, org["id"], _user(db, phone, "Rival Admin"), "centre_admin")
    return {"org_xid": org["xid"], "org_id": org["id"], "admin": admin}


def _member(db, org_id: int, row, role: str = "student"):
    db.execute(text("""
        INSERT INTO org_memberships (org_id, user_id, role, status)
        VALUES (:o, :u, :r, 'active')
    """).bindparams(o=org_id, u=row["id"], r=role))
    db.flush()
    return row


@pytest.fixture
def centre_admin(db, seed):
    row = _user(db, "+998908000001", "Centre Admin", dob="1985-01-01")
    return _member(db, seed["org"].id, row, "centre_admin")


@pytest.fixture
def platform_admin(db):
    row = _user(db, "+998908000002", "Platform Admin", dob="1980-01-01")
    db.execute(text("""
        INSERT INTO platform_role_grants (user_id, role, granted_by)
        VALUES (:u, 'platform_admin', :u)
    """).bindparams(u=row["id"]))
    db.flush()
    return row


@pytest.fixture
def minor(db):
    fifteen = (dt.date.today() - dt.timedelta(days=365 * 15)).isoformat()
    return _user(db, "+998908000003", "Aziza", dob=fifteen)


# ── the roster leak ──────────────────────────────────────────────────

class TestTheOrgRosterIsNotADirectory:
    """`list_cohort_members` was fixed to hand classmates names only. Its sibling
    was not, and it covers the whole centre."""

    def test_a_student_does_not_get_phone_numbers(self, client, seed):
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(seed["student"].xid))
        assert response.status_code == 200
        assert response.json()["items"]
        assert all(i["user"]["phone"] is None for i in response.json()["items"])

    def test_nor_telegram_usernames(self, client, seed):
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(seed["student"].xid))
        assert all(i["user"]["telegram_username"] is None
                   for i in response.json()["items"])

    def test_nor_who_is_a_minor(self, client, db, seed, minor):
        """A roster of names is a directory. A roster of names, phone numbers and
        `is_minor` is a targeting list."""
        _member(db, seed["org"].id, minor)
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(seed["student"].xid))
        assert all(i["user"]["is_minor"] is None for i in response.json()["items"])

    def test_the_actual_phone_number_is_nowhere_in_the_body(self, client, seed):
        """Belt and braces: assert on the response TEXT, so a future field that
        happens to carry the number is caught too."""
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(seed["student"].xid))
        assert seed["author"].phone not in response.text

    def test_a_student_still_sees_who_is_in_the_centre(self, client, seed):
        """Names are the point; removing the endpoint would be the wrong fix."""
        names = [i["user"]["given_name"]
                 for i in client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                                     headers=auth(seed["student"].xid)).json()["items"]]
        assert seed["author"].given_name in names

    def test_a_teacher_gets_the_full_record(self, client, seed):
        """They administer the roster and need to phone a parent."""
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(seed["author"].xid))
        assert any(i["user"]["phone"] for i in response.json()["items"])

    def test_a_centre_admin_does_too(self, client, seed, centre_admin):
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                              headers=auth(centre_admin["xid"]))
        assert any(i["user"]["phone"] for i in response.json()["items"])

    def test_it_can_be_filtered_by_role(self, client, seed):
        response = client.get(f"/api/v1/orgs/{seed['org'].xid}/members?role=teacher",
                              headers=auth(seed["author"].xid))
        assert [i["role"] for i in response.json()["items"]] == ["teacher"]

    def test_a_rival_centre_gets_a_404(self, client, db, seed):
        outsider = _user(db, "+998908000009", "Outsider")
        assert client.get(f"/api/v1/orgs/{seed['org'].xid}/members",
                          headers=auth(outsider["xid"])).status_code == 404


# ── invites ──────────────────────────────────────────────────────────

class TestInvites:
    def test_a_centre_admin_creates_one(self, client, seed, centre_admin):
        response = client.post(f"/api/v1/orgs/{seed['org'].xid}/invites",
                               headers=auth(centre_admin["xid"]),
                               json={"phone": "+998909000001", "role": "student"})
        assert response.status_code == 201
        assert response.json()["token"]

    def test_a_teacher_cannot(self, client, seed):
        """Inviting sets who is at the centre, which is org administration."""
        assert client.post(f"/api/v1/orgs/{seed['org'].xid}/invites",
                           headers=auth(seed["author"].xid),
                           json={"phone": "+998909000002",
                                 "role": "teacher"}).status_code == 403

    def test_only_the_hash_is_stored(self, client, db, seed, centre_admin):
        """A database dump must not be a pile of usable invite links."""
        token = _invite(client, seed, centre_admin, phone="+998909900001")
        stored = db.scalar(text("SELECT token_hash FROM org_invites"))
        assert stored != token and len(stored) == 64

    @pytest.mark.parametrize("phone", ["998909000001", "+998 90 900 00 01",
                                       "+998909000001 ", "90 900 00 01", ""])
    def test_a_number_the_product_cannot_store_is_refused_at_the_door(
            self, client, seed, centre_admin, phone):
        """`phone: str` accepted anything, which was harmless while nothing read
        the column back.

        Now redemption compares it against `users.phone`, and every spelling here
        is the same handset as `+998909000001` while being a different string. An
        invite carrying one is not a slightly-wrong invite — it is an invite that
        can never be accepted by anyone, discovered by a student a fortnight
        later when it expires. Refuse it while the admin is still looking at the
        form.
        """
        assert client.post(f"/api/v1/orgs/{seed['org'].xid}/invites",
                           headers=auth(centre_admin["xid"]),
                           json={"phone": phone, "role": "student"}
                           ).status_code == 422

    def test_accepting_joins_the_organization(self, client, db, seed, centre_admin):
        newcomer = _user(db, "+998909000003", "Newcomer")
        token = _invite(client, seed, centre_admin, phone="+998909000003")
        response = client.post("/api/v1/invites/accept", headers=auth(newcomer["xid"]),
                               json={"token": token})
        assert response.status_code == 200
        assert response.json()["role"] == "student"

    def test_accepting_also_joins_the_named_cohort(self, client, db, seed,
                                                   centre_admin):
        cohort_xid = client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                                 headers=auth(centre_admin["xid"]),
                                 json={"name": "Evening IELTS"}).json()["xid"]
        newcomer = _user(db, "+998909000004", "Newcomer")
        token = _invite(client, seed, centre_admin, phone="+998909000004",
                        cohort_xid=cohort_xid)
        client.post("/api/v1/invites/accept", headers=auth(newcomer["xid"]),
                    json={"token": token})
        assert db.scalar(text("SELECT count(*) FROM cohort_members WHERE user_id = :u")
                         .bindparams(u=newcomer["id"])) == 1

    def test_an_unknown_token_is_a_404(self, client, db):
        newcomer = _user(db, "+998909000005", "Newcomer")
        assert client.post("/api/v1/invites/accept", headers=auth(newcomer["xid"]),
                           json={"token": "not-a-real-token"}).status_code == 404

    def test_a_token_cannot_be_used_twice(self, client, db, seed, centre_admin):
        """Both attempts are now by the SAME person, because the invited number is
        the only one that can make either — which is the point: reuse is a
        student double-tapping a link, not a second student racing them to it."""
        holder = _user(db, "+998909000006", "Holder")
        token = _invite(client, seed, centre_admin, phone="+998909000006")
        assert client.post("/api/v1/invites/accept", headers=auth(holder["xid"]),
                           json={"token": token}).status_code == 200
        reused = client.post("/api/v1/invites/accept", headers=auth(holder["xid"]),
                             json={"token": token})
        assert reused.status_code == 410
        assert reused.json()["code"] == "invite_unusable"

    def test_an_expired_invite_is_refused(self, client, db, seed, centre_admin):
        newcomer = _user(db, "+998909000008", "Newcomer")
        token = _invite(client, seed, centre_admin, phone="+998909000008")
        db.execute(text("UPDATE org_invites SET expires_at = now() - interval '1 day'"))
        db.flush()
        assert client.post("/api/v1/invites/accept", headers=auth(newcomer["xid"]),
                           json={"token": token}).status_code == 410


class TestAnInviteIsBoundToTheNumberItWasSentTo:
    """**The defect, and the suite that read as if it were testing for it.**

    `org_invites.phone` was written by `create_invite` and selected by nothing.
    Redemption asked one question — does this token hash exist — so whoever held
    the token took the role named on it. There is no delivery in this product:
    `create_invite` returns the token to the admin and hardcodes
    `delivered_via: "telegram"`, so the link is forwarded by hand through group
    chats. A `centre_admin` invite is worth forwarding.

    Every invite test above used to address `+998909900001` and then redeem it as
    a user with a different number — twelve of them, all green. A suite can only
    catch a rule it states, and none of them stated this one.
    """

    def test_someone_else_cannot_use_the_link(self, client, db, seed, centre_admin):
        _user(db, "+998909900010", "Invited")
        bystander = _user(db, "+998909900011", "Bystander")
        token = _invite(client, seed, centre_admin, phone="+998909900010",
                        role="centre_admin")
        refused = client.post("/api/v1/invites/accept", headers=auth(bystander["xid"]),
                              json={"token": token})
        assert refused.status_code == 403
        assert refused.json()["code"] == "invite_not_yours"

    def test_and_the_token_was_perfectly_good(self, client, db, seed, centre_admin):
        """The other half of the pair. Without it, the refusal above is equally
        consistent with a redemption path that is simply broken."""
        invited = _user(db, "+998909900012", "Invited")
        bystander = _user(db, "+998909900013", "Bystander")
        token = _invite(client, seed, centre_admin, phone="+998909900012",
                        role="centre_admin")
        assert client.post("/api/v1/invites/accept", headers=auth(bystander["xid"]),
                           json={"token": token}).status_code == 403
        accepted = client.post("/api/v1/invites/accept", headers=auth(invited["xid"]),
                               json={"token": token})
        assert accepted.status_code == 200
        assert accepted.json()["role"] == "centre_admin"

    def test_the_bystander_gains_nothing_at_all(self, client, db, seed, centre_admin):
        """A 403 that still wrote the membership row would pass the test above."""
        _user(db, "+998909900014", "Invited")
        bystander = _user(db, "+998909900015", "Bystander")
        token = _invite(client, seed, centre_admin, phone="+998909900014",
                        role="centre_admin")
        client.post("/api/v1/invites/accept", headers=auth(bystander["xid"]),
                    json={"token": token})
        assert db.scalar(text("SELECT count(*) FROM org_memberships WHERE user_id = :u")
                         .bindparams(u=bystander["id"])) == 0

    def test_and_the_invite_is_still_there_for_the_person_it_was_for(
            self, client, db, seed, centre_admin):
        """A refusal that consumed the invite would lock the student out of their
        own centre with no way back — worse than the hole it closes."""
        invited = _user(db, "+998909900016", "Invited")
        bystander = _user(db, "+998909900017", "Bystander")
        token = _invite(client, seed, centre_admin, phone="+998909900016")
        client.post("/api/v1/invites/accept", headers=auth(bystander["xid"]),
                    json={"token": token})
        assert client.post("/api/v1/invites/accept", headers=auth(invited["xid"]),
                           json={"token": token}).status_code == 200

    def test_the_refusal_does_not_name_the_invited_number(self, client, db, seed,
                                                          centre_admin):
        """Otherwise a leaked token becomes a lookup of one student's phone
        number, and half of them are fifteen."""
        _user(db, "+998909900018", "Invited")
        bystander = _user(db, "+998909900019", "Bystander")
        token = _invite(client, seed, centre_admin, phone="+998909900018")
        refused = client.post("/api/v1/invites/accept", headers=auth(bystander["xid"]),
                              json={"token": token})
        assert "+998909900018" not in refused.text


class TestTheNumberHasToBeProven:
    """`users.phone` is self-declared: `telegram_verify` takes it from
    `requestContact`, which is client-side, and says so. Binding an invite to a
    number nobody proved would be a lock whose key is "type the number you want"
    — worse than no lock, because it reads like one.

    So redemption requires `phone_verified_at`, which the OTP path sets. It did
    not set it: `telegram_verify` writes `phone_verified_at=None` with the comment
    "`phone_verified_at` is set by the OTP path", and the OTP path assigned it
    nowhere. The column was written null and read by nothing.
    """

    def test_an_unproven_number_cannot_redeem_even_its_own_invite(
            self, client, db, seed, centre_admin):
        claimant = _user(db, "+998909900020", "Claimant", verified=False)
        token = _invite(client, seed, centre_admin, phone="+998909900020")
        refused = client.post("/api/v1/invites/accept", headers=auth(claimant["xid"]),
                              json={"token": token})
        assert refused.status_code == 403
        assert refused.json()["code"] == "phone_not_verified"

    def test_and_can_once_it_is_proven(self, client, db, seed, centre_admin):
        claimant = _user(db, "+998909900021", "Claimant", verified=False)
        token = _invite(client, seed, centre_admin, phone="+998909900021")
        assert client.post("/api/v1/invites/accept", headers=auth(claimant["xid"]),
                           json={"token": token}).status_code == 403
        _verify(db, claimant["id"])
        assert client.post("/api/v1/invites/accept", headers=auth(claimant["xid"]),
                           json={"token": token}).status_code == 200

    def test_entering_the_sms_code_is_what_proves_it(self, client, db, seed):
        """The end of the chain, through the real endpoint rather than an UPDATE.

        `verify_otp` is the ONLY place a number is proven, and it recorded
        nothing. Every guard above would have been unreachable in production:
        no user could ever have redeemed an invite.
        """
        import hashlib as _h

        user = _user(db, "+998909900022", "Prover", verified=False)
        challenge = db.execute(text("""
            INSERT INTO otp_challenges (phone, purpose, code_hash, channel, expires_at)
            VALUES (:p, 'login', :h, 'sms', now() + interval '5 minutes')
            RETURNING xid
        """).bindparams(p="+998909900022",
                        h=_h.sha256(b"placeholder").hexdigest())).scalar()
        db.execute(text("UPDATE otp_challenges SET code_hash = :h WHERE xid = :x")
                   .bindparams(h=_h.sha256(f"{challenge}:123456".encode()).hexdigest(),
                               x=challenge))
        db.flush()

        assert client.post("/api/v1/auth/otp/verify",
                           json={"challenge_xid": str(challenge),
                                 "code": "123456"}).status_code == 200
        db.expire_all()
        assert db.scalar(text("SELECT phone_verified_at FROM users WHERE id = :u")
                         .bindparams(u=user["id"])) is not None


class TestWithdrawingAnInvite:
    """`org_invites.revoked_at` has existed since migration 0003 and nothing wrote
    it and nothing read it, so the only way to un-send an invite was to wait
    fourteen days. The role-promotion comment in the router already names the
    scenario — "a centre admin pasting a `student` link into a group chat" — and
    assumed this remedy existed."""

    def _issue(self, client, seed, admin, phone: str) -> tuple[str, str]:
        body = client.post(f"/api/v1/orgs/{seed['org'].xid}/invites",
                           headers=auth(admin["xid"]),
                           json={"phone": phone, "role": "student"}).json()
        return body["xid"], body["token"]

    def test_a_withdrawn_invite_is_refused(self, client, db, seed, centre_admin):
        invited = _user(db, "+998909900030", "Invited")
        xid, token = self._issue(client, seed, centre_admin, "+998909900030")
        assert client.delete(f"/api/v1/orgs/{seed['org'].xid}/invites/{xid}",
                             headers=auth(centre_admin["xid"])).status_code == 204
        refused = client.post("/api/v1/invites/accept", headers=auth(invited["xid"]),
                              json={"token": token})
        assert refused.status_code == 410
        assert refused.json()["code"] == "invite_revoked"

    def test_and_it_worked_a_moment_earlier(self, client, db, seed, centre_admin):
        invited = _user(db, "+998909900031", "Invited")
        _xid, token = self._issue(client, seed, centre_admin, "+998909900031")
        assert client.post("/api/v1/invites/accept", headers=auth(invited["xid"]),
                           json={"token": token}).status_code == 200

    def test_a_teacher_cannot_withdraw_one(self, client, seed, centre_admin):
        xid, _token = self._issue(client, seed, centre_admin, "+998909900032")
        assert client.delete(f"/api/v1/orgs/{seed['org'].xid}/invites/{xid}",
                             headers=auth(seed["author"].xid)).status_code == 403

    def test_another_centre_cannot_withdraw_it(self, client, db, seed, centre_admin):
        """"A centre's material must never leak to competitor centres" is the
        same rule one door along: nor may a competitor touch its roster."""
        xid, token = self._issue(client, seed, centre_admin, "+998909900033")
        rival = _org_with_admin(client, db, "rival-centre", "+998909900034")
        assert client.delete(f"/api/v1/orgs/{rival['org_xid']}/invites/{xid}",
                             headers=auth(rival["admin"]["xid"])).status_code == 404
        invited = _user(db, "+998909900033", "Invited")
        assert client.post("/api/v1/invites/accept", headers=auth(invited["xid"]),
                           json={"token": token}).status_code == 200

    def test_an_accepted_invite_cannot_be_withdrawn(self, client, db, seed,
                                                    centre_admin):
        """Revoking a spent invite would read as "membership removed" and do
        nothing of the kind. Remove the membership instead."""
        invited = _user(db, "+998909900035", "Invited")
        xid, token = self._issue(client, seed, centre_admin, "+998909900035")
        client.post("/api/v1/invites/accept", headers=auth(invited["xid"]),
                    json={"token": token})
        assert client.delete(f"/api/v1/orgs/{seed['org'].xid}/invites/{xid}",
                             headers=auth(centre_admin["xid"])).status_code == 404

    def test_withdrawing_twice_is_a_404(self, client, seed, centre_admin):
        xid, _token = self._issue(client, seed, centre_admin, "+998909900036")
        url = f"/api/v1/orgs/{seed['org'].xid}/invites/{xid}"
        assert client.delete(url, headers=auth(centre_admin["xid"])).status_code == 204
        assert client.delete(url, headers=auth(centre_admin["xid"])).status_code == 404


class TestPendingInvitesArriveByPhoneNumber:
    """Nothing in this product delivers an invite. `create_invite` returns the
    token to the admin who made it and hardcodes `delivered_via: "telegram"`;
    there is no sender behind it. A centre onboarding forty students has forty
    numbers and no way to reach any of them, which is exactly why the link ends
    up in a group chat.

    So the number becomes the channel: sign in, confirm it, and the invitations
    sent to it are here. Migration 0003 built
    `org_invites (phone) WHERE accepted_at IS NULL` for this query and no code had
    ever issued it.
    """

    def test_an_invitation_to_my_number_is_listed(self, client, db, seed,
                                                  centre_admin):
        newcomer = _user(db, "+998909900040", "Newcomer")
        _invite(client, seed, centre_admin, phone="+998909900040")
        listed = client.get("/api/v1/invites/pending",
                            headers=auth(newcomer["xid"])).json()
        assert [i["role"] for i in listed] == ["student"]
        assert listed[0]["org"]["name"] == seed["org"].name

    def test_someone_else_s_is_not(self, client, db, seed, centre_admin):
        bystander = _user(db, "+998909900041", "Bystander")
        _invite(client, seed, centre_admin, phone="+998909900042")
        assert client.get("/api/v1/invites/pending",
                          headers=auth(bystander["xid"])).json() == []

    def test_a_spent_one_is_not(self, client, db, seed, centre_admin):
        newcomer = _user(db, "+998909900043", "Newcomer")
        token = _invite(client, seed, centre_admin, phone="+998909900043")
        client.post("/api/v1/invites/accept", headers=auth(newcomer["xid"]),
                    json={"token": token})
        assert client.get("/api/v1/invites/pending",
                          headers=auth(newcomer["xid"])).json() == []

    def test_nor_a_withdrawn_one(self, client, db, seed, centre_admin):
        newcomer = _user(db, "+998909900044", "Newcomer")
        xid = client.post(f"/api/v1/orgs/{seed['org'].xid}/invites",
                          headers=auth(centre_admin["xid"]),
                          json={"phone": "+998909900044", "role": "student"}
                          ).json()["xid"]
        client.delete(f"/api/v1/orgs/{seed['org'].xid}/invites/{xid}",
                      headers=auth(centre_admin["xid"]))
        assert client.get("/api/v1/invites/pending",
                          headers=auth(newcomer["xid"])).json() == []

    def test_nor_an_expired_one(self, client, db, seed, centre_admin):
        newcomer = _user(db, "+998909900045", "Newcomer")
        _invite(client, seed, centre_admin, phone="+998909900045")
        db.execute(text("UPDATE org_invites SET expires_at = now() - interval '1 day'"))
        db.flush()
        assert client.get("/api/v1/invites/pending",
                          headers=auth(newcomer["xid"])).json() == []

    def test_the_listing_carries_no_token(self, client, db, seed, centre_admin):
        """It is read on the strength of holding the NUMBER. Handing back the
        secret that was mailed to it would make this endpoint a way to collect
        links for numbers you have claimed but not proven — except that proving
        is required, which is what keeps it safe. Do not hand it back anyway."""
        newcomer = _user(db, "+998909900046", "Newcomer")
        token = _invite(client, seed, centre_admin, phone="+998909900046")
        body = client.get("/api/v1/invites/pending",
                          headers=auth(newcomer["xid"])).text
        assert token not in body and "token" not in body

    def test_the_xid_from_the_listing_redeems_it(self, client, db, seed,
                                                 centre_admin):
        """The half that makes the listing worth having: no token ever reached
        this student, and they can still join."""
        newcomer = _user(db, "+998909900047", "Newcomer")
        _invite(client, seed, centre_admin, phone="+998909900047")
        pending = client.get("/api/v1/invites/pending",
                             headers=auth(newcomer["xid"])).json()[0]
        accepted = client.post("/api/v1/invites/accept", headers=auth(newcomer["xid"]),
                               json={"xid": pending["xid"]})
        assert accepted.status_code == 200
        assert accepted.json()["role"] == "student"

    def test_an_xid_for_somebody_else_s_invite_does_not_exist(
            self, client, db, seed, centre_admin):
        """An xid is not a secret — `create_invite` hands it to the admin. So the
        lookup is scoped to the caller's own number and the answer is 404, not
        the 403 the token path gives: there is nothing here to confirm."""
        bystander = _user(db, "+998909900048", "Bystander")
        _user(db, "+998909900049", "Invited")
        xid = client.post(f"/api/v1/orgs/{seed['org'].xid}/invites",
                          headers=auth(centre_admin["xid"]),
                          json={"phone": "+998909900049", "role": "centre_admin"}
                          ).json()["xid"]
        assert client.post("/api/v1/invites/accept", headers=auth(bystander["xid"]),
                           json={"xid": xid}).status_code == 404

    def test_an_unproven_number_is_told_to_prove_it(self, client, db, seed,
                                                    centre_admin):
        """Not an empty list. An empty list tells a student with an invitation
        waiting that they have none, and gives the client nothing to act on."""
        claimant = _user(db, "+998909900050", "Claimant", verified=False)
        _invite(client, seed, centre_admin, phone="+998909900050")
        refused = client.get("/api/v1/invites/pending", headers=auth(claimant["xid"]))
        assert refused.status_code == 403
        assert refused.json()["code"] == "phone_not_verified"


class TestTheRedemptionBodyIsTyped:
    """It was `body: dict`, read once as `body.get("token", "")` — so a request
    with no token at all reached the database as a hash of the empty string."""

    @pytest.mark.parametrize("body", [{}, {"token": "t", "xid": str(uuid.uuid4())}])
    def test_exactly_one_key_or_it_is_a_422(self, client, db, body):
        caller = _user(db, "+998909900051", "Caller")
        assert client.post("/api/v1/invites/accept", headers=auth(caller["xid"]),
                           json=body).status_code == 422


@pytest.fixture
def enrolled(db, seed):
    """The seed student, with the SMS step done.

    Every invite below is addressed to their own number, because that is now the
    only number that can redeem one."""
    _verify(db, seed["student"].id)
    return seed["student"]


class TestAcceptingAnInviteTwiceOver:
    """The 500. `org_memberships` is UNIQUE on `(org_id, user_id)` and the insert
    was unconditional, so an existing member clicking a new link got a 500 — and
    because the transaction rolled back, the invite stayed unaccepted and every
    retry produced the same 500. A student already enrolled, sent a link for a
    second cohort, hit exactly that."""

    def test_an_existing_member_can_accept(self, client, seed, centre_admin,
                                           enrolled):
        token = _invite(client, seed, centre_admin, phone=enrolled.phone)
        response = client.post("/api/v1/invites/accept",
                               headers=auth(enrolled.xid), json={"token": token})
        assert response.status_code == 200

    def test_and_does_not_gain_a_second_membership(self, client, db, seed,
                                                   centre_admin, enrolled):
        token = _invite(client, seed, centre_admin, phone=enrolled.phone)
        client.post("/api/v1/invites/accept", headers=auth(enrolled.xid),
                    json={"token": token})
        db.expire_all()
        assert db.scalar(text("""
            SELECT count(*) FROM org_memberships WHERE org_id = :o AND user_id = :u
        """).bindparams(o=seed["org"].id, u=seed["student"].id)) == 1

    def test_it_joins_them_to_the_new_cohort(self, client, db, seed, centre_admin,
                                             enrolled):
        """The realistic case: already at the centre, invited to a second class."""
        cohort_xid = client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                                 headers=auth(centre_admin["xid"]),
                                 json={"name": "Saturday intensive"}).json()["xid"]
        token = _invite(client, seed, centre_admin, phone=enrolled.phone,
                        cohort_xid=cohort_xid)
        client.post("/api/v1/invites/accept", headers=auth(enrolled.xid),
                    json={"token": token})
        assert db.scalar(text("""
            SELECT count(*) FROM cohort_members WHERE user_id = :u
        """).bindparams(u=seed["student"].id)) == 1

    def test_a_higher_role_is_granted(self, client, db, seed, centre_admin,
                                      enrolled):
        """The invite names a role and its author holds MANAGE_ORG, so a
        promotion is the intent."""
        token = _invite(client, seed, centre_admin, phone=enrolled.phone,
                        role="teacher")
        response = client.post("/api/v1/invites/accept",
                               headers=auth(enrolled.xid), json={"token": token})
        assert response.json()["role"] == "teacher"

    def test_a_lower_role_is_not_applied(self, client, db, seed, centre_admin):
        """A centre admin sending a `student` invite to the wrong number must not
        demote the teacher it reaches.

        Narrower than it was — the group-chat version of this is now impossible,
        since only the invited number can redeem — but a mistyped digit still
        lands a student invite on a teacher's handset, and demotion is still the
        wrong answer.
        """
        _verify(db, seed["author"].id)
        token = _invite(client, seed, centre_admin, phone=seed["author"].phone,
                        role="student")
        response = client.post("/api/v1/invites/accept",
                               headers=auth(seed["author"].xid), json={"token": token})
        assert response.json()["role"] == "teacher"

    def test_accepting_twice_still_burns_the_token(self, client, seed, centre_admin,
                                                   enrolled):
        token = _invite(client, seed, centre_admin, phone=enrolled.phone)
        client.post("/api/v1/invites/accept", headers=auth(enrolled.xid),
                    json={"token": token})
        again = client.post("/api/v1/invites/accept",
                            headers=auth(enrolled.xid), json={"token": token})
        assert again.status_code == 410


# ── consent ──────────────────────────────────────────────────────────

class TestConsent:
    """"Parental-consent flag on minor accounts." Consent is evidence: the
    document version and its hash are stored with the grant."""

    def test_an_adult_consents_for_themselves(self, client, seed):
        response = client.post("/api/v1/me/consents", headers=auth(seed["student"].xid),
                               json={"kind": "stranger_matching",
                                     "doc_version": "terms-2026-07"})
        assert response.status_code == 201
        assert response.json()["granted_by_kind"] == "self"

    def test_a_minor_cannot_self_consent_to_stranger_matching(self, client, minor):
        """"A general terms acceptance does not cover voice calls with strangers,
        and a regulator will not read it that way." """
        response = client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                               json={"kind": "stranger_matching",
                                     "doc_version": "terms-2026-07"})
        assert response.status_code == 403
        assert response.json()["code"] == "parental_consent_required"

    def test_a_parent_without_contact_details_is_not_enough(self, client, minor):
        """"With contact details" is the operative part: an unreachable parent is
        an unverifiable claim."""
        response = client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                               json={"kind": "stranger_matching",
                                     "doc_version": "terms-2026-07",
                                     "granted_by_kind": "parent",
                                     "parent_name": "Nodira"})
        assert response.status_code == 403

    def test_a_parent_with_contact_details_is(self, client, minor):
        response = client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                               json={"kind": "stranger_matching",
                                     "doc_version": "terms-2026-07",
                                     "granted_by_kind": "parent",
                                     "parent_name": "Nodira",
                                     "parent_phone": "+998901234567"})
        assert response.status_code == 201
        assert response.json()["granted_by_kind"] == "parent"

    def test_a_minor_may_still_accept_ordinary_terms_alone(self, client, minor):
        """The rule is about voice calls with strangers, not about everything."""
        assert client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                           json={"kind": "terms",
                                 "doc_version": "terms-2026-07"}).status_code == 201

    def test_the_document_is_hashed_with_the_grant(self, client, db, seed):
        """"Consent is evidence, not a boolean." "They agreed" is not a defence;
        "they agreed to THIS text, whose hash is X" is."""
        client.post("/api/v1/me/consents", headers=auth(seed["student"].xid),
                    json={"kind": "terms", "doc_version": "terms-2026-07"})
        row = db.execute(text("""
            SELECT doc_version, doc_hash FROM consents
        """)).mappings().one()
        assert row["doc_version"] == "terms-2026-07"
        assert len(row["doc_hash"]) == 64

    def test_the_parents_details_are_stored(self, client, db, minor):
        client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                    json={"kind": "stranger_matching", "doc_version": "t",
                          "granted_by_kind": "parent", "parent_name": "Nodira",
                          "parent_phone": "+998901234567"})
        row = db.execute(text("""
            SELECT parent_name, parent_phone FROM consents
        """)).mappings().one()
        assert (row["parent_name"], row["parent_phone"]) == ("Nodira", "+998901234567")

    def test_they_are_listed_back(self, client, seed):
        client.post("/api/v1/me/consents", headers=auth(seed["student"].xid),
                    json={"kind": "terms", "doc_version": "terms-2026-07"})
        listed = client.get("/api/v1/me/consents",
                            headers=auth(seed["student"].xid)).json()
        assert [c["kind"] for c in listed] == ["terms"]


# ── profile and devices ──────────────────────────────────────────────

class TestProfile:
    def test_the_date_of_birth_never_appears(self, client, seed):
        """"Collected for the 18 boundary and nothing else, and never appears in
        a response, a leaderboard or a B2B export." """
        response = client.get("/api/v1/me", headers=auth(seed["student"].xid))
        assert "date_of_birth" not in response.text
        assert "is_minor" in response.json()

    def test_a_profile_can_be_updated(self, client, seed):
        response = client.patch("/api/v1/me", headers=auth(seed["student"].xid),
                                json={"given_name": "Azizaxon", "target_band": 7.5})
        assert response.json()["given_name"] == "Azizaxon"
        assert response.json()["target_band"] == 7.5

    def test_the_date_of_birth_is_not_editable(self, client, db, seed):
        """"Changing it moves a user across the minor/adult boundary and silently
        alters which speaking pools they can enter." """
        before = db.scalar(text("SELECT date_of_birth FROM users WHERE id = :u")
                           .bindparams(u=seed["student"].id))
        client.patch("/api/v1/me", headers=auth(seed["student"].xid),
                     json={"date_of_birth": "2015-01-01"})
        db.expire_all()
        assert db.scalar(text("SELECT date_of_birth FROM users WHERE id = :u")
                         .bindparams(u=seed["student"].id)) == before

    def test_an_impossible_target_band_is_rejected(self, client, seed):
        for band in (0.5, 9.5, -1):
            assert client.patch("/api/v1/me", headers=auth(seed["student"].xid),
                                json={"target_band": band}).status_code == 422


class TestDevices:
    def test_live_sessions_are_listed(self, client, db, seed):
        db.execute(text("""
            INSERT INTO auth_sessions (user_id, token_hash, expires_at, device_label)
            VALUES (:u, 'h1', now() + interval '90 days', 'Redmi Note 12')
        """).bindparams(u=seed["student"].id))
        db.flush()
        listed = client.get("/api/v1/me/devices",
                            headers=auth(seed["student"].xid)).json()
        assert [d["label"] for d in listed] == ["Redmi Note 12"]

    def test_forgetting_one_revokes_its_session(self, client, db, seed):
        """A shared computer in a lab is the case this exists for."""
        db.execute(text("""
            INSERT INTO auth_sessions (user_id, token_hash, expires_at, device_label)
            VALUES (:u, 'h2', now() + interval '90 days', 'Lab PC')
        """).bindparams(u=seed["student"].id))
        db.flush()
        xid = client.get("/api/v1/me/devices",
                         headers=auth(seed["student"].xid)).json()[0]["xid"]
        assert client.delete(f"/api/v1/me/devices/{xid}",
                             headers=auth(seed["student"].xid)).status_code == 204
        db.expire_all()
        assert db.scalar(text(
            "SELECT revoked_reason FROM auth_sessions")) == "user_forgot_device"

    def test_you_cannot_forget_someone_elses_device(self, client, db, seed):
        db.execute(text("""
            INSERT INTO auth_sessions (user_id, token_hash, expires_at, device_label)
            VALUES (:u, 'h3', now() + interval '90 days', 'Teacher laptop')
        """).bindparams(u=seed["author"].id))
        db.flush()
        xid = client.get("/api/v1/me/devices",
                         headers=auth(seed["author"].xid)).json()[0]["xid"]
        assert client.delete(f"/api/v1/me/devices/{xid}",
                             headers=auth(seed["student"].xid)).status_code == 404

    def test_forgetting_an_unknown_device_is_a_404(self, client, seed):
        assert client.delete(f"/api/v1/me/devices/{uuid.uuid4()}",
                             headers=auth(seed["student"].xid)).status_code == 404


# ── organizations and cohorts ────────────────────────────────────────

class TestOrganizations:
    def test_only_a_platform_admin_creates_one(self, client, seed, platform_admin):
        assert client.post("/api/v1/orgs", headers=auth(seed["author"].xid),
                           json={"name": "New Centre",
                                 "slug": "new-centre"}).status_code == 403
        assert client.post("/api/v1/orgs", headers=auth(platform_admin["xid"]),
                           json={"name": "New Centre",
                                 "slug": "new-centre"}).status_code == 201

    def test_a_malformed_slug_is_rejected(self, client, platform_admin):
        for slug in ("ab", "Has Capitals", "under_scores", "x" * 41):
            assert client.post("/api/v1/orgs", headers=auth(platform_admin["xid"]),
                               json={"name": "N", "slug": slug}).status_code == 422

    def test_settings_are_merged_not_replaced(self, client, seed, centre_admin):
        """Two flags set in two requests must both survive."""
        client.patch(f"/api/v1/orgs/{seed['org'].xid}", headers=auth(centre_admin["xid"]),
                     json={"settings": {"teacher_can_publish": True}})
        response = client.patch(f"/api/v1/orgs/{seed['org'].xid}",
                                headers=auth(centre_admin["xid"]),
                                json={"settings": {"content_edit_others": True}})
        assert response.json()["settings"]["teacher_can_publish"] is True
        assert response.json()["settings"]["content_edit_others"] is True

    def test_the_plain_fields_update_too(self, client, seed, centre_admin):
        response = client.patch(f"/api/v1/orgs/{seed['org'].xid}",
                                headers=auth(centre_admin["xid"]),
                                json={"name": "Tashkent Prep (Chilonzor)",
                                      "contact_phone": "+998712000000"})
        assert response.json()["name"] == "Tashkent Prep (Chilonzor)"

    def test_a_teacher_cannot_change_the_settings(self, client, seed):
        """Both flags widen who can affect published material."""
        assert client.patch(f"/api/v1/orgs/{seed['org'].xid}",
                            headers=auth(seed["author"].xid),
                            json={"settings": {"teacher_can_publish": True}}
                            ).status_code == 403

    def test_listing_is_scoped_to_memberships(self, client, db, seed,
                                              platform_admin):
        client.post("/api/v1/orgs", headers=auth(platform_admin["xid"]),
                    json={"name": "Other Centre", "slug": "other-centre"})
        mine = client.get("/api/v1/orgs", headers=auth(seed["student"].xid)).json()
        assert [o["xid"] for o in mine["items"]] == [str(seed["org"].xid)]

    def test_a_platform_admin_sees_them_all(self, client, platform_admin, seed):
        client.post("/api/v1/orgs", headers=auth(platform_admin["xid"]),
                    json={"name": "Other Centre", "slug": "other-centre"})
        listed = client.get("/api/v1/orgs", headers=auth(platform_admin["xid"])).json()
        assert len(listed["items"]) >= 2

    def test_an_unknown_org_is_a_404(self, client, seed):
        assert client.get(f"/api/v1/orgs/{uuid.uuid4()}",
                          headers=auth(seed["student"].xid)).status_code == 404


class TestCohorts:
    def test_a_centre_admin_creates_and_lists_them(self, client, seed, centre_admin):
        created = client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                              headers=auth(centre_admin["xid"]),
                              json={"name": "Evening IELTS",
                                    "academic_year": "2026"})
        assert created.status_code == 201
        listed = client.get(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                            headers=auth(centre_admin["xid"])).json()
        assert [c["name"] for c in listed] == ["Evening IELTS"]

    def test_a_teacher_cannot_create_one(self, client, seed):
        assert client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                           headers=auth(seed["author"].xid),
                           json={"name": "Mine"}).status_code == 403

    def test_a_member_of_the_org_can_be_added(self, client, seed, centre_admin):
        cohort_xid = _cohort(client, seed, centre_admin)
        response = client.post(f"/api/v1/cohorts/{cohort_xid}/members",
                               headers=auth(centre_admin["xid"]),
                               json={"user_xids": [str(seed["student"].xid)]})
        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_someone_from_outside_the_org_cannot_be(self, client, db, seed,
                                                    centre_admin):
        """"A centre could otherwise add anyone's account to its reporting." """
        outsider = _user(db, "+998909100001", "Outsider")
        cohort_xid = _cohort(client, seed, centre_admin)
        response = client.post(f"/api/v1/cohorts/{cohort_xid}/members",
                               headers=auth(centre_admin["xid"]),
                               json={"user_xids": [str(outsider["xid"])]})
        assert response.status_code == 409
        assert response.json()["code"] == "not_an_org_member"

    def test_adding_the_same_student_twice_is_not_an_error(self, client, seed,
                                                           centre_admin):
        cohort_xid = _cohort(client, seed, centre_admin)
        for _ in range(2):
            response = client.post(f"/api/v1/cohorts/{cohort_xid}/members",
                                   headers=auth(centre_admin["xid"]),
                                   json={"user_xids": [str(seed["student"].xid)]})
        assert response.status_code == 200
        assert len(response.json()) == 1


# ── helpers ──────────────────────────────────────────────────────────

def _invite(client, seed, admin, *, phone: str, role: str = "student",
            cohort_xid=None) -> str:
    """`phone` is keyword-only and REQUIRED, which is the whole change.

    It used to default to `+998909900001` and every caller took it — then
    redeemed the invite as somebody else, twelve times, green. A default here is
    a test suite quietly agreeing that the number does not matter.
    """
    body = {"phone": phone, "role": role}
    if cohort_xid:
        body["cohort_xid"] = str(cohort_xid)
    return client.post(f"/api/v1/orgs/{seed['org'].xid}/invites",
                       headers=auth(admin["xid"]), json=body).json()["token"]


def _cohort(client, seed, admin) -> str:
    return client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                       headers=auth(admin["xid"]),
                       json={"name": "Evening IELTS"}).json()["xid"]


class TestRegisteringByInvitation:
    """**Until this existed, no account could be created at all.**

    The only path that inserted a `User` was `POST /auth/telegram/verify`, which
    neither client calls. Signing in by code answers "No account exists for this
    number"; accepting an invitation requires already being signed in. A centre
    could be created, a class filled, a paper published, and not one student
    could get in.

    The invitation is the authority — issued by someone with MANAGE_ORG, bound
    to one number, expiring, stored as a hash — and the one-time code proves the
    caller holds that number. Neither half is sufficient alone, which is what
    these tests pin.
    """

    def _code(self, db, phone: str, code: str = "424242") -> tuple[str, str]:
        """A live challenge for a number, with a code this test knows.

        Minted directly rather than through `POST /auth/otp/request`, which
        returns the code only under `pilot_open_signin` — a pilot escape hatch
        that will be switched off, and a test that needs it switched on is a
        test of the hatch. The row is built exactly as the handler builds it, so
        `_consume_challenge` is still the thing under test.
        """
        import hashlib
        import uuid as _uuid

        challenge = str(_uuid.uuid4())
        db.execute(text("""
            INSERT INTO otp_challenges (xid, phone, purpose, code_hash, channel,
                                        expires_at, max_attempts)
            VALUES (CAST(:x AS uuid), :p, 'login', :h, 'sms',
                    now() + interval '5 minutes', 5)
        """).bindparams(
            x=challenge, p=phone,
            h=hashlib.sha256(f"{challenge}:{code}".encode()).hexdigest()))
        db.flush()
        return challenge, code

    def test_an_invited_stranger_gets_an_account_and_a_session(
            self, client, db, seed, centre_admin):
        phone = "+998909100001"
        token = _invite(client, seed, centre_admin, phone=phone)
        challenge, code = self._code(db, phone)

        response = client.post("/api/v1/auth/invite/redeem", json={
            "token": token, "challenge_xid": challenge, "code": code,
            "date_of_birth": "2005-06-01", "given_name": "Nodira"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["access_token"]
        assert body["principal"]["user"]["given_name"] == "Nodira"
        assert body["joined"]["role"] == "student"
        # The number is proven in this very request, and `accept_invite`
        # requires that of everybody else.
        assert db.scalar(text("SELECT phone_verified_at FROM users WHERE phone = :p")
                         .bindparams(p=phone)) is not None

    def test_the_code_alone_is_not_enough(self, client, db, seed):
        """No invite, no account. Otherwise this is open registration."""
        challenge, code = self._code(db, "+998909100002")
        assert client.post("/api/v1/auth/invite/redeem", json={
            "token": "not-a-real-token", "challenge_xid": challenge,
            "code": code, "date_of_birth": "2005-06-01"}).status_code == 404

    def test_the_invite_alone_is_not_enough(self, client, db, seed, centre_admin):
        """Holding a forwarded link must not create the account it names."""
        token = _invite(client, seed, centre_admin, phone="+998909100003")
        assert client.post("/api/v1/auth/invite/redeem", json={
            "token": token, "challenge_xid": str(uuid.uuid4()), "code": "000000",
            "date_of_birth": "2005-06-01"}).status_code == 401

    def test_proving_a_different_number_is_refused(self, client, db, seed, centre_admin):
        """The forwarded-link case. Whoever opens it can prove their OWN number
        and must still not take the role."""
        token = _invite(client, seed, centre_admin, phone="+998909100004")
        challenge, code = self._code(db, "+998909100005")
        response = client.post("/api/v1/auth/invite/redeem", json={
            "token": token, "challenge_xid": challenge, "code": code,
            "date_of_birth": "2005-06-01"})
        assert response.status_code == 403
        assert response.json()["code"] == "invite_not_yours"

    def test_a_bad_token_does_not_spend_somebody_elses_code(
            self, client, db, seed, centre_admin):
        """The invite is checked BEFORE the code. Otherwise a stranger with a
        guessed challenge could burn attempts against a real student's code."""
        challenge, code = self._code(db, "+998909100006")
        client.post("/api/v1/auth/invite/redeem", json={
            "token": "nope", "challenge_xid": challenge, "code": code,
            "date_of_birth": "2005-06-01"})
        token = _invite(client, seed, centre_admin, phone="+998909100006")
        assert client.post("/api/v1/auth/invite/redeem", json={
            "token": token, "challenge_xid": challenge, "code": code,
            "date_of_birth": "2005-06-01"}).status_code == 200

    def test_registering_without_a_date_of_birth_is_refused(
            self, client, db, seed, centre_admin):
        """`adult_at` is generated from it and every minor rule reads that
        column, so an account cannot exist without one."""
        phone = "+998909100007"
        token = _invite(client, seed, centre_admin, phone=phone)
        challenge, code = self._code(db, phone)
        response = client.post("/api/v1/auth/invite/redeem", json={
            "token": token, "challenge_xid": challenge, "code": code})
        assert response.status_code == 403
        assert response.json()["code"] == "date_of_birth_required"

    def test_an_existing_account_is_joined_not_re_registered(
            self, client, db, seed, centre_admin):
        """A student already on the platform, invited to a second centre. It
        must not read as an error, and must not need a date of birth."""
        existing = _user(db, "+998909100008", "Aziza")
        token = _invite(client, seed, centre_admin, phone="+998909100008")
        challenge, code = self._code(db, "+998909100008")
        response = client.post("/api/v1/auth/invite/redeem", json={
            "token": token, "challenge_xid": challenge, "code": code})
        assert response.status_code == 200, response.text
        assert response.json()["principal"]["user"]["xid"] == str(existing["xid"])
        assert db.scalar(text("SELECT count(*) FROM users WHERE phone = :p")
                         .bindparams(p="+998909100008")) == 1

    def test_a_spent_invite_cannot_be_redeemed_twice(
            self, client, db, seed, centre_admin):
        phone = "+998909100009"
        token = _invite(client, seed, centre_admin, phone=phone)
        challenge, code = self._code(db, phone)
        assert client.post("/api/v1/auth/invite/redeem", json={
            "token": token, "challenge_xid": challenge, "code": code,
            "date_of_birth": "2005-06-01"}).status_code == 200
        challenge2, code2 = self._code(db, phone)
        assert client.post("/api/v1/auth/invite/redeem", json={
            "token": token, "challenge_xid": challenge2, "code": code2,
            "date_of_birth": "2005-06-01"}).status_code == 410

    def test_the_preview_says_who_invited_you_without_naming_the_number(
            self, client, seed, centre_admin):
        """The token names a phone, and tokens get forwarded. A link that reveals
        a student's number to whoever opens it is a leak this flow does not need
        to take."""
        token = _invite(client, seed, centre_admin, phone="+998909100010")
        body = client.post("/api/v1/auth/invite/preview",
                           json={"token": token}).json()
        assert body["org"]["name"] == seed["org"].name
        assert body["role"] == "student"
        assert body["needs_account"] is True
        assert body["phone_hint"] == "•••• 0010"
        assert "+998909100010" not in str(body)

    def test_the_preview_refuses_a_withdrawn_invite(self, client, db, seed,
                                                    centre_admin):
        token = _invite(client, seed, centre_admin, phone="+998909100011")
        db.execute(text("UPDATE org_invites SET revoked_at = now()"))
        db.flush()
        assert client.post("/api/v1/auth/invite/preview",
                           json={"token": token}).status_code == 410
