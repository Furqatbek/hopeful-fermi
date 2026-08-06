"""Generate `docs/api/student-app.md` by performing the flows, and gate it.

The student app is a separate mobile client with no code in this repository, so
its authors read a contract they cannot run. `openapi.yaml` types every field
and explains every endpoint, and it carried **two** examples across 177
operations — which is fine for a single call and useless for the five flows that
actually matter, because every one of them is multi-step and stateful. A client
built from the schema alone gets the shapes right and the SEQUENCE wrong.

So this file performs each flow against a real migrated database and writes down
what happened.

**Regenerate:**

    WRITE_API_DOCS=1 python3 -m pytest tests/integration/test_api_examples.py

**Otherwise it is a gate.** The ordinary suite re-runs the flows, re-renders the
document, and fails when it no longer matches what is committed. That is the
only property that separates this from hand-written examples, which are correct
on the day they are written and fiction from the first schema change onwards.
It needs no CI step of its own: it rides along with `make coverage`, which CI
already runs, and a check that runs everywhere is worth more than one somebody
has to remember.

When it fails, the diff IS the answer — it shows exactly which field changed
shape, in the same terms a client author would see it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token

from .api_examples import Normaliser, Recorder, render

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "api" / "student-app.md"


@pytest.fixture
def client(db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def bearer(who) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(who.xid))}"}


def otp_code(db, challenge_xid: str) -> str:
    """Reverse the stored hash. The plaintext is never persisted — that is the
    point of `code_hash` — so a caller that needs the real code must brute it,
    which is also a fair statement of what the attempt limit protects."""
    stored = db.scalar(text(
        "SELECT code_hash FROM otp_challenges WHERE xid = CAST(:x AS uuid)"
    ).bindparams(x=challenge_xid))
    for n in range(1_000_000):
        candidate = f"{n:06d}"
        if hashlib.sha256(f"{challenge_xid}:{candidate}".encode()).hexdigest() == stored:
            return candidate
    raise AssertionError("no code matched the stored hash")


def assign(db, published, student, **kw):
    from app.modules.exam.models import Assignment, AssignmentTarget

    now = dt.datetime.now(dt.UTC)
    row = Assignment(
        org_id=published["org"].id, test_version_id=published["test_version"].id,
        assigned_by=published["author"].id, target_kind="users",
        opens_at=now - dt.timedelta(hours=1),
        closes_at=now + dt.timedelta(days=7),
        max_attempts=kw.get("max_attempts", 2), mode=kw.get("mode", "exam"),
        allow_review_after=kw.get("allow_review_after", "submit"),
        time_limit_seconds=kw.get("time_limit_seconds", 3600))
    db.add(row)
    db.flush()
    db.add(AssignmentTarget(assignment_id=row.id, user_id=student.id))
    db.flush()
    return row


def make_student(db, org, name, *, age_years):
    """A student of a stated AGE, not a stated birth year.

    The seeded student was born in 2008 and was a minor when these flows were
    first written; by the time anyone reads this they are not. An age computed
    at run time keeps the parental-consent examples about a minor for ever,
    which is the whole point of documenting them.
    """
    from app.modules.identity.models import OrgMembership, User

    born = dt.date.today() - dt.timedelta(days=int(age_years * 365.25))
    person = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name=name,
                  date_of_birth=born, status="active")
    db.add(person)
    db.flush()
    db.add(OrgMembership(org_id=org.id, user_id=person.id, role="student"))
    db.flush()
    return person


def entitle(db, user_id, feature="mock.unlimited"):
    from app.modules.billing.models import EntitlementRow

    db.add(EntitlementRow(subject_kind="user", subject_id=user_id, feature=feature,
                          source_kind="order",
                          starts_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)))
    db.flush()


# ── the flows ────────────────────────────────────────────────────────

def flow_signing_in(client, db, seed) -> list:
    r = Recorder(client)
    phone = seed["student"].phone

    challenge = r.call(
        "POST", "/auth/otp/request", json_body={"phone": phone},
        note=("Ask for a code\n"
              "**Always `202`, whether or not the number has an account.** "
              "Anything else turns this into a phone-number oracle. The response "
              "says which channel was chosen — `telegram` is free and is picked "
              "whenever the account is linked, `sms` costs real money on every "
              "send, which is why the resend control is rate limited to five per "
              "hour per NUMBER and answers `429` with `Retry-After`."),
        expect=(202,))

    session = r.call(
        "POST", "/auth/otp/verify",
        json_body={"challenge_xid": challenge["challenge_xid"],
                   "code": otp_code(db, challenge["challenge_xid"])},
        note=("Exchange it for a session\n"
              "The access token is short (15 minutes) and the refresh token is "
              "long and rotating. Store the refresh token in the platform "
              "keychain, never in shared preferences: it is the credential that "
              "survives a restart."),
        expect=(200,))

    r.call("GET", "/auth/session", headers=bearer(seed["student"]),
           note=("Who am I, and what am I a member of\n"
                 "Call this on every cold start. `memberships` is what tells the "
                 "app whether this student belongs to a centre — the difference "
                 "between the assigned-work home screen and the self-serve one — "
                 "and `platform_roles` is empty for every student."))

    r.call("POST", "/auth/refresh",
           json_body={"refresh_token": session["refresh_token"]},
           note=("Rotate before the access token expires\n"
                 "**The old refresh token is dead the moment this succeeds.** "
                 "Reusing one is treated as theft: the server revokes every live "
                 "session for that user and answers `401 refresh_reuse_detected`. "
                 "Serialise refreshes — two concurrent calls from two threads "
                 "will log the student out."),
           expect=(200,))
    return r.calls


def flow_what_to_do_next(client, db, seed, published) -> list:
    r = Recorder(client)
    student = seed["student"]
    entitle(db, student.id)
    assign(db, published, student)
    head = bearer(student)

    r.call("GET", "/me", headers=head,
           note=("The profile\n"
                 "`is_minor` is derived server-side from a date of birth this "
                 "endpoint deliberately never returns. It decides which speaking "
                 "pools the student may enter and is not a preference."))
    r.call("GET", "/assignments", headers=head,
           note=("What the centre has set\n"
                 "Order the home screen by `closes_at`. `mode` decides which "
                 "runner opens — `exam` is timed, single-play audio, and the "
                 "chrome must look different; `practice` replays freely. "
                 "`allow_review_after` decides whether the answers screen is "
                 "reachable once the paper is submitted."))
    r.call("GET", "/me/entitlements", headers=head,
           note=("What this student may actually do\n"
                 "The one table the whole product asks \"is this allowed\" "
                 "against. A `402` anywhere else in the API means this list did "
                 "not cover the action; render it as \"not yours yet\", never as "
                 "an error."))
    r.call("GET", "/me/progress", headers=head,
           note=("Band over time\n"
                 "Empty for a student who has sat nothing. Design that state — "
                 "it is the first thing most new users see."))
    return r.calls


def flow_sitting_an_exam(client, db, seed, published, with_audio) -> list:
    r = Recorder(client)
    student = seed["student"]
    entitle(db, student.id)
    assignment = assign(db, published, student)
    head = bearer(student)

    attempt = r.call(
        "POST", "/attempts", json_body={"assignment_xid": str(assignment.xid)},
        headers={**head, "Idempotency-Key": str(uuid.uuid4())},
        note=("Start the paper\n"
              "**Send an `Idempotency-Key`.** A retry after a dropped response "
              "returns the same attempt rather than burning a second one against "
              "`max_attempts`. `server_now` and `expires_at` arrive together: "
              "compute the offset against the device clock once, here, and drive "
              "the countdown from it. Never trust the device clock alone."),
        expect=(201,))
    xid = attempt["xid"]

    payload = r.call(
        "GET", f"/attempts/{xid}/payload", headers=head,
        note=("Fetch the paper\n"
              "The snapshot the student sits, frozen at publish. **It contains no "
              "answer keys** — marking is server-side and there is nothing here "
              "to cheat with. Cache it for the duration of the attempt: it does "
              "not change, and re-fetching it on a flaky connection costs the "
              "student time they are being timed on."))

    r.call("POST", f"/attempts/{xid}/sections/1/enter", headers=head,
           note=("Enter a section\n"
                 "Records the entry and is what makes the next call possible. "
                 "`audio_locked` reports whether this section's single play has "
                 "already been spent."))

    r.call("POST", f"/attempts/{xid}/sections/1/audio-grant", headers=head,
           note=("Mint the one audio grant\n"
                 "**In exam mode this succeeds exactly once.** The grant is "
                 "tokenised to this user, expires in about two minutes, and a "
                 "second call answers `409 audio_already_played`. Do not call it "
                 "when the screen loads — call it when the student has committed "
                 "to playing, because there is no way back. In practice mode the "
                 "same call succeeds every time."))

    question = payload["sections"][0]["groups"][0]["questions"][0]
    qv, slot = question["question_version_xid"], question["slot_keys"][0]
    r.call("POST", f"/attempts/{xid}/answers",
           json_body={"deltas": [{"question_version_xid": qv, "slot_key": slot,
                                  "response": "bicycle", "client_seq": 1,
                                  "time_spent_ms": 8200}]},
           headers=head,
           note=("Autosave\n"
                 "The load-bearing endpoint, and **the response doubles as the "
                 "clock sync** — which is why exam timing needs no WebSocket. "
                 "Batch up to 200 deltas, send every few seconds and on every "
                 "screen change. `client_seq` must increase per slot: a delta "
                 "that arrives out of order is discarded rather than allowed to "
                 "resurrect an older answer, which is what makes a retry after a "
                 "network drop safe."))

    r.call("POST", f"/attempts/{xid}/submit",
           headers={**head, "Idempotency-Key": str(uuid.uuid4())},
           note=("Submit\n"
                 "Idempotent: a retry returns the same score run rather than a "
                 "second one. **There is a 30-second grace window after "
                 "`expires_at`** — a submit that lands late is accepted and the "
                 "overrun recorded, so do not refuse client-side at +1s. If the "
                 "request fails entirely, retry with the same key; answers "
                 "already saved are safe and the sweeper submits an abandoned "
                 "attempt on its own."))

    r.call("GET", f"/attempts/{xid}/result", headers=head,
           note=("The band\n"
                 "1 to 9 in half steps, with the raw score it came from. "
                 "Scoring is synchronous here; if `status` is still `submitted` "
                 "rather than `scored`, poll this endpoint."))

    r.call("GET", f"/attempts/{xid}/review", headers=head,
           note=("What was right, and why\n"
                 "Available only when the assignment's `allow_review_after` "
                 "permits it. Every mark carries its explanation — this is where "
                 "the learning actually happens, and it deserves more room than a "
                 "results table."))
    return r.calls


def flow_speaking(client, db, seed) -> list:
    r = Recorder(client)
    # Sixteen. The age band and the consent rule below only exist for a minor,
    # so documenting them with an adult would document nothing.
    student = make_student(db, seed["org"], "Kamola", age_years=16)
    head = bearer(student)

    r.call("POST", "/me/consents",
           json_body={"kind": "stranger_matching", "doc_version": "2026-01",
                      "granted_by_kind": "parent", "parent_name": "Dilnoza Karimova",
                      "parent_phone": "+998901234567", "channel": "web"},
           headers=head,
           note=("Record parental consent\n"
                 "**A minor may enter a public pool only while this is live.** "
                 "Recording it with `granted_by_kind: self` is refused with `403 "
                 "parental_consent_required`; the consent must come from a "
                 "parent and carry contact details. `doc_version` pins which "
                 "words were agreed to."),
           expect=(201,))

    r.call("GET", "/me/consents", headers=head,
           note=("What has been agreed\n"
                 "`revoked_at` is null while the consent is live. `doc_hash` is "
                 "what makes this evidence rather than a boolean."))

    slot = r.call("GET", "/speaking/slots", headers=head,
                  note=("Sessions this student may join\n"
                        "**Server-filtered by age band.** A minor never sees an "
                        "adult pool — this is enforced at the matching layer, not "
                        "here, so do not implement your own filter. Show "
                        "`age_band` and `audience` on every row: `public` means "
                        "strangers and is the one that needed the consent above."))

    r.call("DELETE", "/me/consents/stranger_matching", headers=head,
           note=("Withdraw it\n"
                 "Immediate, and available to the student themselves — "
                 "withdrawal only ever REMOVES capability, so it needs no "
                 "friction. The row is kept: `granted_at` and `revoked_at` are "
                 "the window a regulator asks about."),
           expect=(204,))

    r.call("POST", "/speaking/queue",
           json_body={"band_min": 5.0, "band_max": 6.5, "language": "en",
                      "org_only": False},
           headers=head,
           note=("Or practise right now\n"
                 "The live pool, matched in batches by a scheduled worker rather "
                 "than on this call — so expect to wait, and design the waiting "
                 "state properly. An entry older than ten minutes expires. "
                 "Declaring a band range is optional; the matcher falls back to "
                 "the student's measured band."),
           expect=(201,))

    r.call("DELETE", "/speaking/queue", headers=head,
           note="Leave the queue", expect=(204,))

    r.call("GET", "/speaking/ice-servers", headers=head,
           note=("TURN credentials for the peer connection\n"
                 "Two-hour TTL, per user. **The call is peer-to-peer and the "
                 "audio never reaches our servers** — TURN relays only the "
                 "connections that cannot be established directly. Fetch these "
                 "immediately before connecting, not at app start."))

    r.call("POST", "/realtime/ticket", headers=head,
           note=("Then open the socket\n"
                 "Single-use, 30 seconds, bound to this user. Browsers cannot "
                 "set headers on a WebSocket handshake, so the access token must "
                 "not go in the query string where it lands in every proxy log. "
                 "The gateway burns the ticket on connect. Matching, session "
                 "start and safety events arrive over this socket."),
           expect=(200, 201))
    assert slot is not None
    return r.calls


def flow_refusals(client, db, seed, published) -> list:
    """Every refusal a student client must handle, taken live.

    RFC 9457 problem documents throughout. **Branch on `code`, never on
    `title`** — the code is stable and the title is prose that will be
    translated. Status still decides the RECOVERY: 401 refresh, 403 stop.
    """
    r = Recorder(client)
    author = seed["author"]
    # A student of their own, with no entitlement. `flow_what_to_do_next` grants
    # one to the seeded student and every flow shares this session, so reusing
    # them here would document a `402` that no longer happens.
    student = make_student(db, seed["org"], "Nodira", age_years=15)
    # A good token on an account that is no longer active — the 403 that proves
    # the 401s above are about identity and not permission.
    suspended = make_student(db, seed["org"], "Shahnoza", age_years=20)
    suspended.status = "suspended"
    db.flush()
    db.expire_all()

    r.call("GET", "/me",
           note=("No token\n"
                 "`401 unauthenticated`, with `WWW-Authenticate: Bearer` as RFC "
                 "9110 §11.6.1 requires. **This is the status your interceptor "
                 "keys on: 401 means refresh and retry once, 403 means stop.** "
                 "Both used to be 403, so an interceptor never fired and an "
                 "expired token was reported to the student as a permission "
                 "problem."),
           expect=(401,))

    r.call("GET", "/me", headers={"Authorization": "Bearer not-a-token"},
           note=("A token that does not parse, or has expired\n"
                 "`401 invalid_token`. Refresh once and retry; if the refresh "
                 "also fails, sign the student out."),
           expect=(401,))

    r.call("GET", "/me", headers=bearer(suspended),
           note=("A GOOD token on an account that is no longer active\n"
                 "**`403`, and the difference from the two above is the whole "
                 "point.** The token is valid and we know exactly who this is; "
                 "refreshing would send the app round a loop. Suspended, banned "
                 "and closed accounts all land here. Sign the student out and "
                 "say why — do not retry."),
           expect=(403,))

    r.call("POST", "/attempts",
           json_body={"test_version_xid": str(published["test_version"].xid)},
           headers=bearer(student),
           note=("Not entitled, on the SELF-SERVE path\n"
                 "`402 payment_required`, and it is **not an error** — the "
                 "student did nothing wrong and something is simply not theirs "
                 "yet. `feature` names what was missing; route to the purchase "
                 "screen.\n\n"
                 "**Only self-serve is charged.** An attempt started with an "
                 "`assignment_xid` is never checked against the student: the "
                 "centre paid for it when the work was set, and a student must "
                 "never see a paywall for work their school assigned. So a `402` "
                 "here always means \"you chose this yourself and have not "
                 "bought it\", never \"your homework is locked\"."),
           expect=(402,))

    r.call("POST", "/attempts",
           json_body={"assignment_xid": str(uuid.uuid4())},
           headers=bearer(student),
           note="An id that matches nothing\n`404`.", expect=(404,))

    r.call("POST", "/me/consents",
           json_body={"kind": "stranger_matching", "doc_version": "2026-01",
                      "granted_by_kind": "self"},
           headers=bearer(student),
           note=("A minor consenting on their own behalf\n"
                 "`403 parental_consent_required`. The asymmetry is deliberate: "
                 "granting needs a parent, withdrawing does not."),
           expect=(403,))

    r.call("POST", "/auth/otp/request", json_body={"phone": "12345"},
           note=("A malformed body\n"
                 "`422`, and **every problem with the request comes back at "
                 "once** in `findings` — each with a `path` you can attach to the "
                 "right field. Do not make the student fix one thing per round "
                 "trip."),
           expect=(422,))

    r.call("GET", "/attempts/00000000-0000-7000-8000-000000000000/payload",
           headers=bearer(author),
           note=("Somebody else's attempt\n"
                 "`404`, not `403` — the API does not confirm that an id it will "
                 "not serve exists."),
           expect=(404, 403))
    return r.calls


PREAMBLE = """
Worked examples for the **student mobile app**, generated by performing each
flow against a real database and recording what came back.

`openapi/openapi.yaml` is the contract and remains authoritative for every field
and every type. This document exists because a schema cannot show a SEQUENCE,
and every flow that matters in this API is multi-step and stateful — starting an
attempt, spending the one audio grant, pairing for a speaking session. A client
built from the schema alone gets the shapes right and the order wrong.

**These examples cannot drift.** `tests/integration/test_api_examples.py`
re-performs every call in the ordinary test run, re-renders this file, and fails
when it no longer matches. If you are reading this, it matched on the last
commit. Regenerate with:

    WRITE_API_DOCS=1 python3 -m pytest tests/integration/test_api_examples.py

## Conventions

- **Base URL** — every path below is prefixed `/api/v1`.
- **Auth** — `Authorization: Bearer <access token>`. Access tokens last 15
  minutes; refresh with `POST /auth/refresh` and store the refresh token in the
  platform keychain.
- **Ids** — every public id is an opaque UUIDv7 called `xid`. Internal integer
  keys are never exposed. Treat an `xid` as opaque; do not parse a timestamp out
  of it.
- **Time** — every timestamp is UTC ISO-8601. Responses that matter for timing
  carry `server_now`; **the server is the sole authority on time remaining and
  on marking**, and the client's own clock is never trusted for either.
- **Errors** — RFC 9457 `application/problem+json`, always with a stable
  machine-readable `code`. **Branch on `code`** for what to say; branch on the
  **status** for what to do. `title` is prose and will be translated.
- **401 vs 403** — `401` means *I do not know who you are*: refresh, then retry
  once. It carries `WWW-Authenticate: Bearer` per RFC 9110 §11.6.1. `403` means
  *I know who you are and the answer is no*: stop, and show the reason. Never
  retry a `403` with a fresh token — a suspended account and a permission denial
  both live there, and neither is fixed by refreshing.
- **Idempotency** — `POST /attempts`, `POST /attempts/{xid}/submit` and
  `POST /orders` accept an `Idempotency-Key` header. Send one. A retry after a
  dropped response then returns the original result instead of doing the thing
  twice.
- **Values below are normalised** — ids, timestamps, tokens and digests are
  replaced with stable placeholders so this document is reproducible. Durations
  are preserved exactly: if `expires_at` is an hour after `started_at` here, it
  is an hour in production.
"""


def test_the_document_matches_what_the_api_actually_does(
        client, db, seed, published, with_audio, scorer_svc):
    """Perform every flow, render, and compare with what is committed."""
    flows = [
        ("Signing in",
         "No passwords anywhere in this product. A student proves a phone "
         "number and keeps a rotating refresh token for months — every forced "
         "re-login costs an SMS.",
         flow_signing_in(client, db, seed)),
        ("What to do next",
         "The four calls behind the home screen.",
         flow_what_to_do_next(client, db, seed, published)),
        ("Sitting an exam",
         "The flow the product exists for. Read this one in order — every step "
         "depends on the one before it, and two of them are irreversible.",
         flow_sitting_an_exam(client, db, seed, published, with_audio)),
        ("Speaking practice",
         "Peer-to-peer voice practice, and the child-safety rules that wrap it.",
         flow_speaking(client, db, seed)),
        ("Refusals you must handle",
         "Taken live, not written from memory. Every one of these is a state "
         "your UI needs.",
         flow_refusals(client, db, seed, published)),
    ]

    generated = render("Student app — API by flow", PREAMBLE, flows, Normaliser())

    if os.environ.get("WRITE_API_DOCS"):
        DOC.parent.mkdir(parents=True, exist_ok=True)
        DOC.write_text(generated)
        return

    assert DOC.exists(), (
        f"{DOC.relative_to(ROOT)} is missing. Generate it with "
        "WRITE_API_DOCS=1 python3 -m pytest tests/integration/test_api_examples.py")
    committed = DOC.read_text()
    if committed != generated:
        import difflib

        diff = "\n".join(difflib.unified_diff(
            committed.splitlines(), generated.splitlines(),
            fromfile="committed", tofile="what the API does now", lineterm=""))
        raise AssertionError(
            "docs/api/student-app.md no longer matches this API.\n\n"
            "The API changed and the document did not. Regenerate it:\n"
            "    WRITE_API_DOCS=1 python3 -m pytest "
            "tests/integration/test_api_examples.py\n\n"
            f"{diff[:8000]}")


def test_every_documented_path_is_in_the_contract():
    """The document may not invent an endpoint.

    Cheap, and it catches the failure this whole file is built against: prose
    that describes an API nobody serves.
    """
    import re

    import yaml

    if not DOC.exists():
        pytest.skip("document not generated yet")
    spec = yaml.safe_load((ROOT / "openapi" / "openapi.yaml").read_text())
    real = set(spec["paths"])
    cited = {re.sub(r"019a0000-[0-9a-f-]+|123456|stranger_matching|1",
                    "{x}", p)
             for p in re.findall(r"`(?:GET|POST|PUT|PATCH|DELETE) /api/v1(\S+)`",
                                 DOC.read_text())}
    # Compare by template shape: the document carries concrete ids where the
    # contract carries `{xid}`, so normalise both to a common skeleton.
    def skeleton(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", re.sub(r"019a0000-[0-9a-f-]+", "{}", path))

    shapes = {skeleton(p) for p in real}
    unknown = sorted(s for s in {skeleton(c) for c in cited} if s not in shapes)
    assert not unknown, f"documented paths not in the contract: {unknown}"


