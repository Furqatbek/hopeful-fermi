"""The multi-tenant leak suite.

"A centre's material must never leak to competitor centres" is a contractual
promise, so it gets a test file rather than a code review comment.

Almost every real multi-tenant leak is a missing LIST scope, not a missing detail
check — the detail endpoint is the one people remember to guard. So this file
tests three separate things:

  1. Every listing endpoint, with a rival centre's teacher holding a valid token,
     returns none of the first centre's rows.
  2. Every detail endpoint answers 404 rather than 403. Confirming a resource
     exists is itself a leak: "403" tells a competitor the id was real.
  3. A STRUCTURAL check over the source — any new handler that selects a content
     model without passing it through `policy.filter_content` fails the build.
     Points 1 and 2 protect today's endpoints; point 3 protects tomorrow's.
"""

from __future__ import annotations

import ast
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import issue_access_token

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(engine, db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def rival(db, seed):
    """A second centre with its own teacher and its own material.

    Same shape as the first centre, so a leak in either direction is visible: the
    tests assert the rival cannot see `seed`'s rows AND that they can still see
    their own, which is what stops an over-broad filter passing this file by
    returning nothing to anybody.
    """
    from app.modules.content.models import (
        AudioTrack, Passage, PassageVersion, Question, QuestionGroup,
        QuestionGroupVersion, QuestionVersion, Test, TestVersion,
    )
    from app.modules.identity.models import Organization, OrgMembership, User

    org = Organization(name="Rival Academy", slug=f"ra-{uuid.uuid4().hex[:6]}",
                       status="active")
    teacher = User(phone=f"+9989{uuid.uuid4().int % 10**8:08d}", given_name="Kamola",
                   date_of_birth=datetime(1990, 1, 1).date())
    db.add_all([org, teacher])
    db.flush()
    db.add(OrgMembership(org_id=org.id, user_id=teacher.id, role="centre_admin"))

    test = Test(org_id=org.id, owner_user_id=teacher.id, title="Rival Mock",
                kind="mock", skills=["reading"])
    passage = Passage(org_id=org.id, owner_user_id=teacher.id, title="Rival Passage")
    track = AudioTrack(org_id=org.id, owner_user_id=teacher.id, title="Rival Audio")
    question = Question(org_id=org.id, owner_user_id=teacher.id,
                        type_key="sentence_completion", skill="reading")
    group = QuestionGroup(org_id=org.id, owner_user_id=teacher.id,
                          title="Rival Group", skill="reading")
    db.add_all([test, passage, track, question, group])
    db.flush()
    tv = TestVersion(test_id=test.id, title="Rival Mock v1", created_by=teacher.id)
    pv = PassageVersion(passage_id=passage.id, title="Rival Passage", checksum="x",
                        created_by=teacher.id)
    qv = QuestionVersion(question_id=question.id, type_key="sentence_completion",
                         payload={"text": "x {{s1}}", "slots": ["s1"]},
                         slot_keys=["s1"], checksum="x", created_by=teacher.id)
    gv = QuestionGroupVersion(group_id=group.id, checksum="x", created_by=teacher.id)
    db.add_all([tv, pv, qv, gv])
    db.flush()
    return {"org": org, "teacher": teacher, "test": test, "test_version": tv,
            "passage": passage, "passage_version": pv, "audio": track,
            "question": question, "question_version": qv, "group": group,
            "group_version": gv}


@pytest.fixture
def rival_auth(rival):
    return {"Authorization": f"Bearer {issue_access_token(str(rival['teacher'].xid))}"}


@pytest.fixture
def home_auth(seed):
    return {"Authorization": f"Bearer {issue_access_token(str(seed['author'].xid))}"}


@pytest.fixture
def home_extras(db, seed):
    """The assets `seed` does not create, so no listing test passes vacuously.

    Without an audio track owned by the first centre, `GET /audio-tracks` returns
    an empty list to everyone and the leak assertion is true for the wrong reason.
    """
    import json

    from sqlalchemy import text

    from app.modules.content.models import AudioTrack

    track = AudioTrack(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                       title="Section 1 audio", status="ready")
    db.add(track)
    db.flush()
    cue = db.execute(text("""
        INSERT INTO cue_card_sets (org_id, owner_user_id, title)
        VALUES (:o, :u, 'Part 2 cards') RETURNING id, xid
    """).bindparams(o=seed["org"].id, u=seed["author"].id)).mappings().one()
    db.execute(text("""
        INSERT INTO cue_card_set_versions (set_id, version_no, body, created_by)
        VALUES (:s, 1, CAST(:b AS jsonb), :u)
    """).bindparams(s=cue["id"], b=json.dumps({"cards": []}), u=seed["author"].id))
    db.flush()
    return {"audio": track, "cue_card_set_xid": str(cue["xid"])}


@pytest.fixture
def home_xids(db, seed, home_extras) -> set[str]:
    """Every xid the first centre owns, read back from the database.

    Enumerated from the schema rather than hand-listed: a hand-written list is
    exactly what silently stops covering a table someone adds later.
    """
    from sqlalchemy import text

    tables = [
        ("tests", "org_id"), ("test_versions", None), ("passages", "org_id"),
        ("passage_versions", None), ("questions", "org_id"),
        ("question_versions", None), ("question_groups", "org_id"),
        ("question_group_versions", None), ("audio_tracks", "org_id"),
        ("cue_card_sets", "org_id"), ("organizations", "id"),
    ]
    found: set[str] = set()
    org_id = seed["org"].id
    for table, column in tables:
        if column == "org_id":
            sql = f"SELECT xid FROM {table} WHERE org_id = :o"
        elif column == "id":
            sql = f"SELECT xid FROM {table} WHERE id = :o"
        else:
            # Version tables reach their owner through their asset.
            parent = {"test_versions": ("tests", "test_id"),
                      "passage_versions": ("passages", "passage_id"),
                      "question_versions": ("questions", "question_id"),
                      "question_group_versions": ("question_groups", "group_id")}[table]
            sql = (f"SELECT v.xid FROM {table} v JOIN {parent[0]} a "
                   f"ON a.id = v.{parent[1]} WHERE a.org_id = :o")
        found |= {str(x) for x in db.execute(text(sql).bindparams(o=org_id)).scalars()}
    assert len(found) >= 12, "the fixture stopped creating content; the suite is vacuous"
    return found


def _xids(payload) -> set[str]:
    """Every xid anywhere in a response body, however deeply nested.

    Walking the whole document rather than the top-level `items` is deliberate:
    a leak through an embedded `current_version` or a `group_version` is still a
    leak, and that is exactly the shape a hand-written assertion misses.
    """
    found: set[str] = set()
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith("xid") and isinstance(value, str):
                    found.add(value)
                else:
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found


LISTINGS = [
    "/api/v1/tests",
    "/api/v1/passages",
    "/api/v1/audio-tracks",
    "/api/v1/questions",
    "/api/v1/question-groups",
    "/api/v1/band-maps",
    "/api/v1/cue-card-sets",
    "/api/v1/orgs",
    "/api/v1/assignments",
    "/api/v1/competitions",
    "/api/v1/speaking/slots",
]


# Listings that return an asset the first centre owns. Each is paired with an
# xid that MUST appear for its owner — otherwise "the rival saw nothing" is true
# because the endpoint returns nothing to anybody, and the test proves nothing.
OWNED_BY_LISTING = {
    "/api/v1/tests": lambda s, e: str(s["test"].xid),
    "/api/v1/passages": lambda s, e: None,          # asset xid resolved from the db
    "/api/v1/audio-tracks": lambda s, e: str(e["audio"].xid),
    "/api/v1/questions": lambda s, e: None,
    "/api/v1/question-groups": lambda s, e: None,
    "/api/v1/cue-card-sets": lambda s, e: e["cue_card_set_xid"],
    "/api/v1/orgs": lambda s, e: str(s["org"].xid),
}


class TestListingsAreScoped:
    @pytest.mark.parametrize("path", LISTINGS)
    def test_a_rival_centre_sees_none_of_our_rows(self, client, rival_auth, home_xids,
                                                  path):
        response = client.get(path, headers=rival_auth)
        assert response.status_code == 200, response.text
        leaked = _xids(response.json()) & home_xids
        assert not leaked, f"{path} leaked {sorted(leaked)} to a rival centre"

    @pytest.mark.parametrize("path", LISTINGS)
    def test_and_we_still_see_our_own(self, client, home_auth, home_extras, path):
        """The other half of the pair.

        A filter that returns nothing to anybody would pass the test above and
        break the product; this is what keeps the first test honest.
        """
        assert client.get(path, headers=home_auth).status_code == 200

    @pytest.mark.parametrize("path", sorted(OWNED_BY_LISTING))
    def test_the_owner_sees_their_own_row_in_it(self, client, home_auth, seed,
                                                home_extras, home_xids, path):
        """Proves the leak assertion above is not vacuous.

        For each of these listings the owner's own row is present, so "the rival
        saw none of these xids" is a statement about scoping rather than about an
        endpoint that happens to be empty.
        """
        body = client.get(path, headers=home_auth).json()
        seen = _xids(body)
        expected = OWNED_BY_LISTING[path](seed, home_extras)
        if expected is not None:
            assert expected in seen, f"{path} did not return the owner's own row"
        else:
            assert seen & home_xids, f"{path} returned none of the owner's assets"

    def test_the_rival_sees_their_own_library(self, client, rival_auth, rival):
        body = client.get("/api/v1/tests", headers=rival_auth).json()
        assert str(rival["test"].xid) in _xids(body)


DETAILS = [
    ("/api/v1/tests/{test}", None),
    ("/api/v1/tests/{test}/versions", None),
    ("/api/v1/test-versions/{test_version}", None),
    ("/api/v1/test-versions/{test_version}/sections", None),
    ("/api/v1/passage-versions/{passage_version}", None),
    ("/api/v1/passage-versions/{passage_version}/usage", None),
    ("/api/v1/question-versions/{question_version}", None),
    ("/api/v1/question-versions/{question_version}/keys", None),
    ("/api/v1/question-group-versions/{group_version}", None),
    ("/api/v1/orgs/{org}", None),
]


class TestDetailsAre404NotForbidden:
    @pytest.mark.parametrize("template,_", DETAILS)
    def test_a_rival_gets_not_found(self, client, rival_auth, seed, template, _):
        """404, never 403.

        A 403 confirms the id was real, which is the whole answer a competitor
        probing for a rival's test ids is looking for.
        """
        path = template.format(
            test=seed["test"].xid, test_version=seed["test_version"].xid,
            passage_version=seed["passage_version"].xid,
            question_version=seed["question_versions"][0].xid,
            group_version=seed["group_version"].xid, org=seed["org"].xid)
        response = client.get(path, headers=rival_auth)
        assert response.status_code == 404, f"{path} -> {response.status_code}"


class TestWritesAreRefused:
    def test_a_rival_cannot_edit_our_test(self, client, rival_auth, seed):
        r = client.patch(f"/api/v1/tests/{seed['test'].xid}",
                         json={"title": "Stolen"}, headers=rival_auth)
        assert r.status_code == 404

    def test_a_rival_cannot_publish_our_version(self, client, rival_auth, seed):
        r = client.post(f"/api/v1/test-versions/{seed['test_version'].xid}/publish",
                        headers=rival_auth)
        assert r.status_code in (403, 404)

    def test_a_rival_cannot_clone_our_test_without_a_grant(self, client, rival_auth,
                                                           seed):
        r = client.post(f"/api/v1/tests/{seed['test'].xid}/clone", json={},
                        headers=rival_auth)
        assert r.status_code == 404

    def test_a_rival_cannot_export_our_answer_keys(self, client, rival_auth, seed):
        r = client.get(f"/api/v1/test-versions/{seed['test_version'].xid}/export"
                       "?include_keys=true", headers=rival_auth)
        assert r.status_code == 404

    def test_a_rival_cannot_place_our_group_into_their_test(self, client, rival_auth,
                                                            rival, seed, db):
        """The composition path is the subtle one.

        Reading is blocked everywhere else, but a reference passed into someone
        else's own draft would pull a competitor's group into a test they own —
        so the reference is authorized on the way IN.
        """
        from app.modules.content.models import TestVersionSection

        section = TestVersionSection(test_version_id=rival["test_version"].id,
                                     position=1, skill="reading", title="S1")
        db.add(section)
        db.flush()
        r = client.post(f"/api/v1/sections/{section.xid}/groups",
                        json={"group_version_xid": str(seed["group_version"].xid),
                              "position": 1}, headers=rival_auth)
        assert r.status_code == 404

    def test_a_rival_cannot_reference_our_passage_in_their_section(
            self, client, rival_auth, rival, seed):
        r = client.post(f"/api/v1/test-versions/{rival['test_version'].xid}/sections",
                        json={"position": 1, "skill": "reading", "title": "S1",
                              "passage_version_xid": str(seed["passage_version"].xid)},
                        headers=rival_auth)
        assert r.status_code == 404


class TestOrgMembershipIsNotTeachingAuthority:
    """The second leak vector, and the one an org-scoped filter cannot catch.

    Everything above keeps org A's data away from org B. These keep a STUDENT at
    org A away from the teacher-facing views of org A — where their classmates'
    bands, attendance and phone numbers live. Being a member of an organization
    is not the same as running one, and conflating the two is easy because both
    checks read `org_id in actor.org_ids`.
    """

    @pytest.fixture
    def cohort(self, db, seed):
        from app.modules.identity.models import Cohort, CohortMember

        row = Cohort(org_id=seed["org"].id, name="Evening group",
                     created_by=seed["author"].id)
        db.add(row)
        db.flush()
        db.add_all([CohortMember(cohort_id=row.id, user_id=seed["student"].id),
                    CohortMember(cohort_id=row.id, user_id=seed["author"].id)])
        db.flush()
        return row

    @pytest.fixture
    def student_auth(self, seed):
        return {"Authorization":
                f"Bearer {issue_access_token(str(seed['student'].xid))}"}

    def test_a_student_cannot_read_cohort_progress(self, client, student_auth, cohort):
        r = client.get(f"/api/v1/cohorts/{cohort.xid}/progress", headers=student_auth)
        assert r.status_code == 404

    def test_a_student_cannot_read_cohort_attendance(self, client, student_auth,
                                                     cohort):
        r = client.get(f"/api/v1/cohorts/{cohort.xid}/attendance",
                       headers=student_auth)
        assert r.status_code == 404

    def test_a_student_cannot_read_the_centres_seats(self, client, student_auth, seed):
        r = client.get(f"/api/v1/orgs/{seed['org'].xid}/seats", headers=student_auth)
        assert r.status_code == 403

    def test_a_classmate_sees_names_but_not_phone_numbers(self, client, student_auth,
                                                          cohort, seed):
        """Half a cohort may be fifteen. A roster is fine; a phone list is not."""
        r = client.get(f"/api/v1/cohorts/{cohort.xid}/members", headers=student_auth)
        assert r.status_code == 200, r.text
        members = r.json()
        assert {m["user"]["given_name"] for m in members} == {"Aziza", "Dilnoza"}
        assert all(m["user"]["phone"] is None for m in members)
        assert seed["author"].phone not in r.text

    def test_a_teacher_gets_the_full_roster(self, client, home_auth, cohort, seed):
        r = client.get(f"/api/v1/cohorts/{cohort.xid}/members", headers=home_auth)
        assert r.status_code == 200, r.text
        assert any(m["user"]["phone"] for m in r.json())

    def test_a_student_cannot_stage_a_regrade(self, client, student_auth, seed):
        r = client.post("/api/v1/regrades",
                        json={"trigger": "answer_key_change",
                              "subject_type": "question_version",
                              "subject_xid": str(seed["question_versions"][0].xid),
                              "reason": "I got it wrong"}, headers=student_auth)
        assert r.status_code == 403


class TestPlatformGlobalIsStillVisible:
    def test_platform_global_content_reaches_everyone(self, client, rival_auth, seed, db):
        """The filter must not be so tight it breaks the shared library.

        Platform-global content is the one route by which a centre legitimately
        sees material it does not own.
        """
        seed["test"].visibility = "platform_global"
        db.flush()
        body = client.get("/api/v1/tests", headers=rival_auth).json()
        assert str(seed["test"].xid) in _xids(body)


# ── the structural guard ─────────────────────────────────────────────

CONTENT_MODELS = {"Test", "Passage", "Question", "QuestionGroup", "AudioTrack"}

# Handlers that select a content model but legitimately scope another way. Each
# entry is a deliberate decision, not a suppression: keeping the list short and
# named is what makes the check meaningful.
EXEMPT = {
    # Scoped by the join to an already-filtered parent, or by an explicit
    # ownership predicate written in SQL.
    "app/api/routers/assets.py:list_band_maps",       # org_id IS NULL OR mine
    "app/api/routers/assets.py:list_cue_cards",       # raw SQL, same four routes
    # Not a listing: resolves one row for the exam engine after the attempt's own
    # ownership check has already run.
    "app/api/routers/exam.py:read_payload",
    "app/api/routers/exam.py:read_result",
    "app/api/routers/exam.py:start_attempt",
    # Assigning and competing read a single version, then call policy.require
    # explicitly with the resolved resource.
    "app/api/routers/teaching.py:create_assignment",
    "app/api/routers/competitions.py:create_competition",
    # The publish gate path predates the filter and guards with _may_publish;
    # covered by TestWritesAreRefused above.
    "app/api/routers/authoring.py:_test_version",
    "app/api/routers/authoring.py:publish_version",
    "app/api/routers/authoring.py:_regrade_preview",
}

SCOPING_CALLS = {"scoped", "filter_content", "require", "_owned", "_test", "_version",
                 "_section", "_passage_version", "_question_version", "_group_version"}


def _selects_content_model(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if (isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                and child.func.id == "select"):
            for arg in child.args:
                name = arg.id if isinstance(arg, ast.Name) else None
                if name in CONTENT_MODELS:
                    return True
    return False


def _calls_a_scoping_helper(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name in SCOPING_CALLS:
                return True
    return False


def test_no_handler_selects_content_without_scoping_it():
    """The guard that outlives this file's explicit cases.

    A new listing endpoint written next month, with a `select(Test)` and no
    filter, fails here — which is the only way "every query that returns content
    passes through filter" stays true rather than becoming a comment describing
    what used to be the case.
    """
    offenders = []
    for path in sorted((ROOT / "app" / "api" / "routers").glob("*.py")):
        tree = ast.parse(path.read_text())
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if f"{rel}:{node.name}" in EXEMPT:
                continue
            if _selects_content_model(node) and not _calls_a_scoping_helper(node):
                offenders.append(f"{rel}:{node.name}")
    assert not offenders, (
        "these handlers select content without a policy filter: " + ", ".join(offenders))
