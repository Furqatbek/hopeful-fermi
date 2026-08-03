"""The acting user's own screens: profile, consents, devices, invites, orgs.

Ten endpoints the console had never called, and one of them is a security fix
rather than a new screen.

**"Sign out" did not sign anybody out.** `App.tsx` called `clearSession()` — it
empties `localStorage` and forgets the in-memory access token — and stopped
there. `auth_sessions.revoked_at` stayed NULL, so the refresh token remained
mintable for its full ninety days. `TestSigningOut` proves both halves: that the
token still works when only the browser is cleared, and that `POST /auth/logout`
is what actually closes it. It also proves the ORDERING the fix depends on —
logout authenticates with the access token, so a client that cleared its session
first would send the revocation unsigned and have it refused, with the button
looking identical either way.

The rest is what the three screens send, in the order they send it. Two things
they must NOT be able to do are covered as hard as the happy paths: recording
stranger-matching consent for a minor without a parent, and creating an
organization without the platform-admin grant.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid as _uuid

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


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


def _no_content(response) -> None:
    """204 has no body, so it cannot go through `_ok`."""
    assert response.status_code == 204, response.text


def _phone() -> str:
    return f"+9989{_uuid.uuid4().int % 10**8:08d}"


def _user(db, name: str, *, dob: str = "1990-01-01", verified: bool = True,
          phone: str | None = None):
    """`verified` is the SMS-code step, and it defaults to done.

    Console sign-in is `POST /auth/otp/verify`, which sets `phone_verified_at`,
    so a signed-in member of staff normally has it. The invite tests say when
    they want the other case.
    """
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, family_name, date_of_birth, status,
                           phone_verified_at)
        VALUES (:p, :n, 'Karimova', CAST(:d AS date), 'active',
                CASE WHEN :v THEN now() END)
        RETURNING id, xid, phone
    """).bindparams(p=phone or _phone(), n=name, d=dob, v=verified)).mappings().one()
    db.flush()
    return row


def _session(db, user_id: int, *, revoked: bool = False) -> tuple[str, str]:
    """An open session, written the way `auth._open_session` writes one.

    Returns the raw refresh token and the session xid — the raw token because
    only its hash is stored and `POST /auth/refresh` needs the original.
    """
    raw = secrets.token_urlsafe(48)
    xid = db.execute(text("""
        INSERT INTO auth_sessions (user_id, token_hash, expires_at, revoked_at)
        VALUES (:u, :h, now() + interval '90 days',
                CASE WHEN :r THEN now() END)
        RETURNING xid
    """).bindparams(u=user_id, h=hashlib.sha256(raw.encode()).hexdigest(),
                    r=revoked)).scalar()
    db.flush()
    return raw, str(xid)


def _invite(db, org_id: int, phone: str, role: str = "teacher", *,
            expires_days: int = 14, revoked: bool = False,
            accepted: bool = False) -> tuple[str, str]:
    """What `POST /orgs/{xid}/invites` writes. Returns the raw token and xid."""
    raw = secrets.token_urlsafe(32)
    xid = db.execute(text("""
        INSERT INTO org_invites (org_id, role, phone, token_hash, created_by,
                                 expires_at, revoked_at, accepted_at)
        VALUES (:o, :r, :p, :h, (SELECT id FROM users ORDER BY id LIMIT 1),
                now() + make_interval(days => :days),
                CASE WHEN :rev THEN now() END,
                CASE WHEN :acc THEN now() END)
        RETURNING xid
    """).bindparams(o=org_id, r=role, p=phone,
                    h=hashlib.sha256(raw.encode()).hexdigest(),
                    days=expires_days, rev=revoked, acc=accepted)).scalar()
    db.flush()
    return raw, str(xid)


@pytest.fixture
def staff(db, seed):
    """The signed-in member of staff the Account screen is about."""
    return _user(db, "Dilnoza", dob="1990-04-01")


# ── profile ──────────────────────────────────────────────────────────

class TestTheProfileScreen:
    def test_what_get_me_gives_the_screen_to_render(self, client, staff):
        body = _ok(client.get("/api/v1/me", headers=auth(staff["xid"])))
        assert set(body) == {
            "xid", "phone", "given_name", "family_name", "locale", "timezone",
            "telegram_username", "target_band", "is_minor", "created_at",
        }
        assert body["is_minor"] is False

    def test_date_of_birth_never_leaves_the_server(self, client, staff):
        """The screen shows `is_minor` because it cannot show the date.

        Collected for the 18 boundary and nothing else. If it ever appeared in
        this response it would be in a B2B export the next week.
        """
        body = _ok(client.get("/api/v1/me", headers=auth(staff["xid"])))
        assert "date_of_birth" not in body

    def test_saving_the_four_fields_the_form_offers(self, client, staff):
        body = _ok(client.patch("/api/v1/me", headers=auth(staff["xid"]), json={
            "given_name": "Dilnoza", "family_name": "Yusupova",
            "locale": "ru", "target_band": 7.5,
        }))
        assert (body["given_name"], body["family_name"]) == ("Dilnoza", "Yusupova")
        assert (body["locale"], body["target_band"]) == ("ru", 7.5)
        # Read back, because the screen invalidates `["me"]` and renders the
        # second response rather than the first.
        again = _ok(client.get("/api/v1/me", headers=auth(staff["xid"])))
        assert again["target_band"] == 7.5

    def test_a_date_of_birth_in_the_body_changes_nothing(self, client, db, staff):
        """Why the form explains instead of offering an input.

        `UserUpdate` has no such field, so the value is dropped silently. A form
        with a date box would look as if it had saved.
        """
        _ok(client.patch("/api/v1/me", headers=auth(staff["xid"]),
                         json={"given_name": "Dilnoza",
                               "date_of_birth": "2015-01-01"}))
        stored = db.execute(text("SELECT date_of_birth FROM users WHERE id = :u")
                            .bindparams(u=staff["id"])).scalar()
        assert stored == dt.date(1990, 4, 1)
        assert _ok(client.get("/api/v1/me",
                              headers=auth(staff["xid"])))["is_minor"] is False

    def test_a_target_band_off_the_scale_is_refused(self, client, staff):
        """The bounds the form mirrors. IELTS runs 1 to 9."""
        refused = client.patch("/api/v1/me", headers=auth(staff["xid"]),
                               json={"target_band": 11})
        assert refused.status_code == 422, refused.text


# ── consents ─────────────────────────────────────────────────────────

class TestConsents:
    def test_the_ledger_starts_empty(self, client, staff):
        assert _ok(client.get("/api/v1/me/consents",
                              headers=auth(staff["xid"]))) == []

    def test_recording_one_and_reading_it_back(self, client, staff):
        recorded = _ok(client.post("/api/v1/me/consents", headers=auth(staff["xid"]),
                                   json={"kind": "privacy",
                                         "doc_version": "privacy-2026-01",
                                         "granted_by_kind": "self",
                                         "channel": "web"}), 201)
        assert recorded["kind"] == "privacy"
        assert recorded["granted_by_kind"] == "self"
        listed = _ok(client.get("/api/v1/me/consents", headers=auth(staff["xid"])))
        assert [row["doc_version"] for row in listed] == ["privacy-2026-01"]
        # The field the screen renders "withdrawn" from. Nothing in this product
        # writes it — there is no revoke endpoint — so it is null on every row.
        assert listed[0]["revoked_at"] is None

    def test_a_second_version_leaves_both_rows(self, client, staff):
        """Why the screen folds the list instead of showing it raw.

        Re-accepting an updated notice appends; it does not replace. Two
        `privacy` rows both look current, and only the newer one is the answer to
        "which document did they accept".
        """
        for version in ("privacy-2026-01", "privacy-2026-06"):
            _ok(client.post("/api/v1/me/consents", headers=auth(staff["xid"]),
                            json={"kind": "privacy", "doc_version": version,
                                  "granted_by_kind": "self"}), 201)
        listed = _ok(client.get("/api/v1/me/consents", headers=auth(staff["xid"])))
        assert len(listed) == 2
        assert {row["doc_version"] for row in listed} == \
            {"privacy-2026-01", "privacy-2026-06"}

    def test_an_adult_may_consent_to_stranger_matching_themselves(
            self, client, staff):
        _ok(client.post("/api/v1/me/consents", headers=auth(staff["xid"]),
                        json={"kind": "stranger_matching",
                              "doc_version": "matching-2026-01",
                              "granted_by_kind": "self"}), 201)

    def test_a_minor_may_not_consent_to_stranger_matching_alone(self, client, db):
        """The refusal the form is built so it cannot provoke.

        A general terms acceptance does not cover voice calls with strangers, and
        a regulator will not read it that way.
        """
        minor = _user(db, "Aziza", dob="2012-05-01")
        refused = client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                              json={"kind": "stranger_matching",
                                    "doc_version": "matching-2026-01",
                                    "granted_by_kind": "self"})
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "parental_consent_required"

    def test_a_parent_without_a_number_is_still_refused(self, client, db):
        """`granted_by_kind: parent` on its own is not evidence of a parent."""
        minor = _user(db, "Aziza", dob="2012-05-01")
        refused = client.post("/api/v1/me/consents", headers=auth(minor["xid"]),
                              json={"kind": "stranger_matching",
                                    "doc_version": "matching-2026-01",
                                    "granted_by_kind": "parent",
                                    "parent_name": "Nodira Karimova"})
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "parental_consent_required"

    def test_a_parent_with_contact_details_is_recorded(self, client, db):
        minor = _user(db, "Aziza", dob="2012-05-01")
        recorded = _ok(client.post(
            "/api/v1/me/consents", headers=auth(minor["xid"]),
            json={"kind": "stranger_matching", "doc_version": "matching-2026-01",
                  "granted_by_kind": "parent", "parent_name": "Nodira Karimova",
                  "parent_phone": "+998901234567", "channel": "paper"}), 201)
        assert recorded["granted_by_kind"] == "parent"

    def test_the_hash_that_makes_it_evidence_is_stored_and_not_returned(
            self, client, db, staff):
        """`doc_hash` is what distinguishes evidence from a checkbox, and no
        endpoint returns it — so the screen can show the version string and
        nothing more."""
        recorded = _ok(client.post("/api/v1/me/consents", headers=auth(staff["xid"]),
                                   json={"kind": "terms",
                                         "doc_version": "terms-2026-01",
                                         "granted_by_kind": "self"}), 201)
        assert "doc_hash" not in recorded
        stored = db.execute(text("SELECT doc_hash FROM consents WHERE user_id = :u")
                            .bindparams(u=staff["id"])).scalar()
        assert stored == hashlib.sha256(b"terms-2026-01").hexdigest()


# ── devices ──────────────────────────────────────────────────────────

class TestDevices:
    def test_only_live_sessions_are_listed(self, client, db, staff):
        _session(db, staff["id"])
        _session(db, staff["id"], revoked=True)
        listed = _ok(client.get("/api/v1/me/devices", headers=auth(staff["xid"])))
        assert len(listed) == 1

    def test_the_two_columns_the_screen_cannot_fill(self, client, db, staff):
        """Why the device table says what it says.

        `label` reads `auth_sessions.device_label`, which no code path writes —
        `_open_session` never sets it. `platform` is hardcoded null and has no
        column behind it at all. And `current` is hardcoded false, because the
        access token carries only `sub`: the server cannot tell which session
        this request came from, so the screen must not claim to know either.
        """
        _session(db, staff["id"])
        row = _ok(client.get("/api/v1/me/devices", headers=auth(staff["xid"])))[0]
        assert row["label"] is None
        assert row["platform"] is None
        assert row["current"] is False
        assert row["last_seen_at"] is not None

    def test_forgetting_one_revokes_it_and_drops_it_from_the_list(
            self, client, db, staff):
        _session(db, staff["id"])
        _, xid = _session(db, staff["id"])
        gone = client.delete(f"/api/v1/me/devices/{xid}", headers=auth(staff["xid"]))
        assert gone.status_code == 204, gone.text
        db.expire_all()
        listed = _ok(client.get("/api/v1/me/devices", headers=auth(staff["xid"])))
        assert xid not in [row["xid"] for row in listed]
        assert len(listed) == 1
        reason = db.execute(text("""
            SELECT revoked_reason FROM auth_sessions WHERE xid = CAST(:x AS uuid)
        """).bindparams(x=xid)).scalar()
        assert reason == "user_forgot_device"

    def test_forgetting_a_session_ends_its_refresh_token(self, client, db, staff):
        """The point of the button. A device you no longer have must not be able
        to mint another access token."""
        raw, xid = _session(db, staff["id"])
        _no_content(client.delete(f"/api/v1/me/devices/{xid}",
                                  headers=auth(staff["xid"])))
        db.expire_all()
        refused = client.post("/api/v1/auth/refresh", json={"refresh_token": raw})
        assert refused.status_code == 403, refused.text

    def test_somebody_elses_session_is_not_mine_to_forget(self, client, db, staff):
        """Scoped by `user_id` as well as by xid, and 404 rather than 403 — the
        caller learns nothing about whether that session exists."""
        other = _user(db, "Rustam")
        _, xid = _session(db, other["id"])
        refused = client.delete(f"/api/v1/me/devices/{xid}",
                                headers=auth(staff["xid"]))
        assert refused.status_code == 404, refused.text

    def test_an_unknown_device_is_a_404(self, client, staff):
        refused = client.delete(f"/api/v1/me/devices/{_uuid.uuid4()}",
                                headers=auth(staff["xid"]))
        assert refused.status_code == 404, refused.text


# ── signing out ──────────────────────────────────────────────────────

class TestSigningOut:
    def test_clearing_the_browser_alone_leaves_the_session_live(
            self, client, db, staff):
        """**The bug this package fixes.**

        `clearSession()` touches `localStorage` and a module variable. Nothing
        else. A copy of the refresh token taken from that browser goes on working
        for ninety days, which on the shared computer at a centre's front desk is
        the whole exposure.
        """
        raw, _ = _session(db, staff["id"])
        assert client.post("/api/v1/auth/refresh",
                           json={"refresh_token": raw}).status_code == 200

    def test_logout_closes_it_server_side(self, client, db, staff):
        raw, _ = _session(db, staff["id"])
        out = client.post("/api/v1/auth/logout", headers=auth(staff["xid"]))
        assert out.status_code == 204, out.text
        db.expire_all()
        refused = client.post("/api/v1/auth/refresh", json={"refresh_token": raw})
        assert refused.status_code == 403, refused.text

    def test_logout_without_a_token_is_refused(self, client, db, staff):
        """Why `signOut` calls the server BEFORE clearing the browser.

        This request authenticates with the access token. Clear the session first
        and it goes out unsigned, is refused here, and the revocation never
        happens — with the button behaving identically either way.
        """
        _session(db, staff["id"])
        refused = client.post("/api/v1/auth/logout")
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "unauthenticated"

    def test_it_closes_every_session_not_only_this_one(self, client, db, staff):
        """The contract calls this "revoke the current session" and the handler
        revokes them all. Recorded because the console's copy has to be true:
        signing out at the front desk also signs the teacher out on their phone.
        """
        first, _ = _session(db, staff["id"])
        second, _ = _session(db, staff["id"])
        _no_content(client.post("/api/v1/auth/logout",
                                headers=auth(staff["xid"])))
        db.expire_all()
        assert _ok(client.get("/api/v1/me/devices",
                              headers=auth(staff["xid"]))) == []
        for token in (first, second):
            assert client.post("/api/v1/auth/refresh",
                               json={"refresh_token": token}).status_code == 403


# ── invitations ──────────────────────────────────────────────────────

class TestPendingInvites:
    def test_an_unconfirmed_number_is_told_what_to_do(self, client, db):
        """Not an error to shout about: an ordinary state, rendered as an
        explanation. Matching an invite against a number nobody has proved would
        be a lock whose key is "type the number you want"."""
        fresh = _user(db, "Kamola", verified=False)
        refused = client.get("/api/v1/invites/pending", headers=auth(fresh["xid"]))
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "phone_not_verified"

    def test_the_invitations_addressed_to_my_number(self, client, db, seed, staff):
        _invite(db, seed["org"].id, staff["phone"], "teacher")
        listed = _ok(client.get("/api/v1/invites/pending",
                                headers=auth(staff["xid"])))
        assert len(listed) == 1
        assert listed[0]["role"] == "teacher"
        # The whole Org object, which is what the screen renders in the first
        # column rather than an id the admin would have to look up.
        assert listed[0]["org"]["name"] == seed["org"].name
        assert listed[0]["expires_at"] is not None

    def test_somebody_elses_invitation_is_not_listed(self, client, db, seed, staff):
        _invite(db, seed["org"].id, _phone(), "centre_admin")
        assert _ok(client.get("/api/v1/invites/pending",
                              headers=auth(staff["xid"]))) == []

    def test_spent_withdrawn_and_expired_are_left_out(self, client, db, seed, staff):
        """A to-do list, not a history. A dead row here is a button that can only
        fail."""
        _invite(db, seed["org"].id, staff["phone"], revoked=True)
        _invite(db, seed["org"].id, staff["phone"], accepted=True)
        _invite(db, seed["org"].id, staff["phone"], expires_days=-1)
        assert _ok(client.get("/api/v1/invites/pending",
                              headers=auth(staff["xid"]))) == []


class TestAcceptingAnInvite:
    def test_accepting_the_one_read_off_the_list(self, client, db, seed, staff):
        _, xid = _invite(db, seed["org"].id, staff["phone"], "teacher")
        body = _ok(client.post("/api/v1/invites/accept", headers=auth(staff["xid"]),
                               json={"xid": xid}))
        assert body["role"] == "teacher"
        assert body["status"] == "active"
        assert body["org"]["name"] == seed["org"].name
        # Gone from the list, which is what the screen invalidates for.
        assert _ok(client.get("/api/v1/invites/pending",
                              headers=auth(staff["xid"]))) == []

    def test_accepting_a_pasted_token(self, client, db, seed, staff):
        raw, _ = _invite(db, seed["org"].id, staff["phone"], "centre_admin")
        body = _ok(client.post("/api/v1/invites/accept", headers=auth(staff["xid"]),
                               json={"token": raw}))
        assert body["role"] == "centre_admin"

    def test_both_keys_at_once_is_refused(self, client, db, seed, staff):
        """Why the screen has two submits rather than one form with two boxes."""
        raw, xid = _invite(db, seed["org"].id, staff["phone"])
        refused = client.post("/api/v1/invites/accept", headers=auth(staff["xid"]),
                              json={"token": raw, "xid": xid})
        assert refused.status_code == 422, refused.text

    def test_neither_key_is_refused(self, client, staff):
        """It used to read `body.get("token", "")`, so an empty request reached
        the database as a hash of the empty string."""
        refused = client.post("/api/v1/invites/accept", headers=auth(staff["xid"]),
                              json={})
        assert refused.status_code == 422, refused.text

    def test_a_token_sent_to_a_different_number(self, client, db, seed, staff):
        """Holding the token is not being the person it was sent to. Before this
        binding a forwarded `centre_admin` link made a centre admin of whoever
        opened it first."""
        raw, _ = _invite(db, seed["org"].id, _phone(), "centre_admin")
        refused = client.post("/api/v1/invites/accept", headers=auth(staff["xid"]),
                              json={"token": raw})
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "invite_not_yours"
        # Deliberately silent about WHICH number, or a leaked token becomes a
        # lookup of the invited person's phone.
        assert seed["student"].phone not in refused.text

    def test_a_withdrawn_invitation_is_gone_for_good(self, client, db, seed, staff):
        raw, _ = _invite(db, seed["org"].id, staff["phone"], revoked=True)
        refused = client.post("/api/v1/invites/accept", headers=auth(staff["xid"]),
                              json={"token": raw})
        assert refused.status_code == 410, refused.text
        assert refused.json()["code"] == "invite_revoked"

    def test_an_unconfirmed_number_cannot_redeem_either(self, client, db, seed):
        """The token box is refused for the same reason the list is empty, so the
        screen explains it once."""
        fresh = _user(db, "Kamola", verified=False)
        raw, _ = _invite(db, seed["org"].id, fresh["phone"])
        refused = client.post("/api/v1/invites/accept", headers=auth(fresh["xid"]),
                              json={"token": raw})
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "phone_not_verified"


# ── organizations ────────────────────────────────────────────────────

@pytest.fixture
def platform_admin(db):
    row = _user(db, "Platform Admin", dob="1980-01-01")
    db.execute(text("""
        INSERT INTO platform_role_grants (user_id, role, granted_by)
        VALUES (:u, 'platform_admin', :u)
    """).bindparams(u=row["id"]))
    db.flush()
    return row


class TestOrganizations:
    def test_the_listing_shape_the_screen_reuses(self, client, platform_admin):
        body = _ok(client.get("/api/v1/orgs?limit=25",
                              headers=auth(platform_admin["xid"])))
        assert set(body) == {"items", "next_cursor"}
        # Hardcoded null: there is no second page to fetch however many rows
        # exist, which is why the screen says so rather than paging.
        assert body["next_cursor"] is None

    def test_a_platform_admin_sees_every_organization(
            self, client, db, seed, platform_admin):
        assert seed["org"].xid is not None
        names = [o["name"] for o in _ok(client.get(
            "/api/v1/orgs?limit=25",
            headers=auth(platform_admin["xid"])))["items"]]
        assert seed["org"].name in names

    def test_a_teacher_sees_only_their_own(self, client, db, seed):
        """The same call, scoped by membership. A centre must not read the
        platform's customer list."""
        outsider = _user(db, "Rustam")
        assert _ok(client.get("/api/v1/orgs?limit=25",
                              headers=auth(outsider["xid"])))["items"] == []
        mine = _ok(client.get("/api/v1/orgs?limit=25",
                              headers=auth(seed["author"].xid)))["items"]
        assert [o["name"] for o in mine] == [seed["org"].name]

    def test_a_teacher_may_not_create_one(self, client, seed):
        """Why the screen shows a sentence instead of a form. A centre cannot
        create the entity that governs it, so a disabled button would be an
        invitation to keep trying."""
        refused = client.post("/api/v1/orgs", headers=auth(seed["author"].xid),
                              json={"name": "Rival Prep", "slug": "rival-prep"})
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "create_not_permitted"

    def test_a_platform_admin_creates_one(self, client, platform_admin):
        body = _ok(client.post("/api/v1/orgs", headers=auth(platform_admin["xid"]),
                               json={"name": "Tashkent Prep Centre",
                                     "slug": "tashkent-prep-centre",
                                     "kind": "prep_centre",
                                     "legal_name": "MChJ Tashkent Prep",
                                     "contact_phone": "+998901234567"}), 201)
        assert body["slug"] == "tashkent-prep-centre"
        # Active on creation, not the model's "pending" default — so there is no
        # approval step for the screen to offer.
        assert body["status"] == "active"
        listed = _ok(client.get("/api/v1/orgs?limit=25",
                                headers=auth(platform_admin["xid"])))["items"]
        assert "tashkent-prep-centre" in [o["slug"] for o in listed]

    def test_the_slug_pattern_the_form_mirrors(self, client, platform_admin):
        """`^[a-z0-9-]{3,40}$`, and permanent — `OrgUpdate` does not carry it.

        The form suggests one from the name and refuses to submit when nothing
        usable survives, because these are the requests that would come back.
        """
        for slug in ("Tashkent Prep", "ab", "a" * 41, "prep_centre", ""):
            refused = client.post("/api/v1/orgs",
                                  headers=auth(platform_admin["xid"]),
                                  json={"name": "Somewhere", "slug": slug})
            assert refused.status_code == 422, f"{slug!r}: {refused.text}"
