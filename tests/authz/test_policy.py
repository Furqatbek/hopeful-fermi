"""The permission matrix, decided directly rather than through HTTP.

`tests/integration/test_authz_leaks.py` drives two rival centres through the API
and is the more convincing test of the two — it proves the wiring. But it only
ever constructs the resources those flows happen to create, and a coverage run
showed the consequence: **eleven of the decisions in `policy.check` had never
been executed once.** Among them were

  * `platform_global` content being readable,
  * an author reading their own `author_private` draft,
  * a teacher publishing because their centre enabled `teacher_can_publish`,
  * a teacher being refused an edit to a colleague's material,
  * published content refusing to be hard-deleted — the branch that keeps
    takedown evidence alive,
  * and the `content_grants` clause in `filter_content`, which is the entire
    mechanism for sharing a paper with another centre on purpose.

Every one of those is a promise made in `docs/design/0002-data-model.md` §8 or in
the brief. Reaching them through the API would need a fixture per branch; here
they are three lines each, because `check` is a pure function over three fields.

No database, no HTTP. These run in the unit suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import Column, Integer, Select, String, Table, select
from sqlalchemy.orm import declarative_base

from app.modules.authz.policy import (
    Action,
    Decision,
    Resource,
    Role,
    check,
    filter_content,
    require,
    role_of,
)
from app.platform.errors import Forbidden

HOME, RIVAL = 1, 2
AUTHOR, COLLEAGUE = 10, 11


@dataclass
class FakeActor:
    """Matches the `Actor` protocol. A dataclass rather than a Mock so a change
    to the protocol breaks this file loudly."""

    user_id: int = AUTHOR
    org_ids: tuple[int, ...] = (HOME,)
    roles: dict[int, str] = field(default_factory=lambda: {HOME: "teacher"})
    platform_roles: tuple[str, ...] = ()

    @property
    def is_platform_admin(self) -> bool:
        return "platform_admin" in self.platform_roles


def teacher(**kw) -> FakeActor:
    return FakeActor(**kw)


def centre_admin(org: int = HOME, user: int = AUTHOR) -> FakeActor:
    return FakeActor(user_id=user, org_ids=(org,), roles={org: "centre_admin"})


def student(org: int = HOME) -> FakeActor:
    return FakeActor(user_id=99, org_ids=(org,), roles={org: "student"})


def outsider() -> FakeActor:
    return FakeActor(user_id=50, org_ids=(RIVAL,), roles={RIVAL: "teacher"})


def platform_admin() -> FakeActor:
    return FakeActor(user_id=1, org_ids=(), roles={},
                     platform_roles=("platform_admin",))


OURS = Resource(org_id=HOME, owner_user_id=AUTHOR, visibility="org_private")


class TestRoleResolution:
    def test_a_platform_admin_outranks_every_organization(self):
        assert role_of(platform_admin(), RIVAL) is Role.PLATFORM_ADMIN

    def test_content_with_no_organization_has_no_role(self):
        """Platform-global content is nobody's to administer. Returning a role
        here would let any member of any centre edit shared material."""
        assert role_of(teacher(), None) is None

    def test_a_non_member_has_no_role_in_that_organization(self):
        assert role_of(teacher(), RIVAL) is None


class TestReadVisibility:
    """The contractual one. "A centre's material must never leak to competitor
    centres" is the promise; these are its four routes."""

    def test_platform_global_content_is_readable_by_anyone(self):
        shared = Resource(org_id=None, visibility="platform_global")
        assert check(outsider(), Action.READ, shared).allowed

    def test_an_author_reads_their_own_private_draft(self):
        draft = Resource(org_id=HOME, owner_user_id=AUTHOR, visibility="author_private")
        assert check(teacher(), Action.READ, draft) == Decision(True, "owner")

    def test_a_colleague_in_the_same_centre_does_not_read_a_private_draft(self):
        """`author_private` outranks org membership. A half-finished paper is not
        the centre's until its author says so."""
        draft = Resource(org_id=HOME, owner_user_id=AUTHOR, visibility="author_private")
        decision = check(teacher(user_id=COLLEAGUE), Action.READ, draft)
        assert decision == Decision(False, "author_private")

    def test_a_member_reads_their_centres_content(self):
        assert check(student(), Action.READ, OURS) == Decision(True, "org_member")

    def test_a_rival_centre_does_not(self):
        assert check(outsider(), Action.READ, OURS) == Decision(False, "not_a_member")

    def test_a_platform_admin_reads_everything(self):
        assert check(platform_admin(), Action.READ, OURS).allowed


class TestWritesRequireMembership:
    def test_a_non_member_cannot_write_at_all(self):
        """Distinct from the READ path above: a write by someone with no role in
        the owning organization is refused before the matrix is consulted."""
        assert check(outsider(), Action.EDIT, OURS) == Decision(False, "not_a_member")

    @pytest.mark.parametrize("action", [a for a in Action if a is not Action.READ])
    def test_a_student_can_do_none_of_them(self, action):
        """The matrix as data means this is one parametrize rather than fifteen
        scattered role checks — which is the point of the module.

        Over EVERY action but READ, derived from the enum rather than listed:
        `0002` §8 says "Students are omitted" from every write row, and a list
        of eight actions written by hand was silently missing CREATE, SHARE,
        VIEW_EXPOSURE, VIEW_ANSWER_KEY and MANAGE_BAND_MAP — so adding
        `Role.STUDENT` to the answer-key row, whose comment says the absence "is
        the whole point of the action", changed no test. The reason is asserted
        too, because "refused" by `not_a_member` would pass a broken matrix.
        """
        assert check(student(), action, OURS) == \
            Decision(False, f"role_student_cannot_{action.value}")

    @pytest.mark.parametrize("action", [
        Action.PUBLISH, Action.ARCHIVE, Action.SHARE, Action.TAKEDOWN,
        Action.MANAGE_REGISTRY, Action.MANAGE_ORG, Action.MANAGE_BAND_MAP,
    ])
    def test_a_teacher_is_refused_the_rows_that_exclude_them(self, action):
        """The rows where a teacher is absent by design — publishing and sharing
        because a centre's reputation rides on them, the rest because they
        administer the centre or the platform rather than teach in it."""
        assert check(teacher(), action, OURS) == \
            Decision(False, f"role_teacher_cannot_{action.value}")


class TestTheMatrixIsExactlyThis:
    """The permission table, pinned as a literal.

    Every other test here exercises a BRANCH of `check`, and the coverage floor
    on `app/modules/authz/` holds every branch to 100%. Set membership is not a
    branch: adding a role to a row of `_MATRIX` executes no new line and, until
    this test, changed the outcome of no test either. So the data that is the
    entire authorization policy could drift with the suite green.

    A change to the matrix must now be a change to this test, made on purpose,
    with the `0002` §8 row it corresponds to in the reviewer's eye.
    """

    def test_the_matrix_is_exactly_this(self):
        from app.modules.authz import policy

        S, T, C, P = Role.STUDENT, Role.TEACHER, Role.CENTRE_ADMIN, Role.PLATFORM_ADMIN
        assert policy._MATRIX == {
            Action.READ: {S, T, C, P},
            Action.CREATE: {T, C, P},
            Action.EDIT: {T, C, P},
            Action.PUBLISH: {C, P},
            Action.ARCHIVE: {C, P},
            Action.DELETE: {T, C, P},
            Action.SHARE: {C, P},
            Action.IMPORT: {T, C, P},
            Action.EXPORT: {T, C, P},
            Action.REGRADE: {T, C, P},
            Action.VIEW_EXPOSURE: {T, C, P},
            Action.VIEW_ANSWER_KEY: {T, C, P},
            Action.TAKEDOWN: {P},
            Action.MANAGE_REGISTRY: {P},
            Action.MANAGE_ORG: {C, P},
            Action.MANAGE_BAND_MAP: {C, P},
        }

    def test_every_action_has_a_row(self):
        """An action with no row is refused for everybody by `_MATRIX.get(...,
        set())` — silently, which is the wrong way for a new action to be
        discovered missing."""
        from app.modules.authz import policy

        assert set(policy._MATRIX) == set(Action)


class TestPublishIsOptInPerCentre:
    """Teachers are absent from the PUBLISH row on purpose: a centre's reputation
    rides on its published material."""

    def test_a_teacher_cannot_publish_by_default(self):
        decision = check(teacher(), Action.PUBLISH, OURS)
        assert decision == Decision(False, "role_teacher_cannot_publish")

    def test_a_teacher_can_publish_when_the_centre_enables_it(self):
        decision = check(teacher(), Action.PUBLISH, OURS,
                         org_settings={"teacher_can_publish": True})
        assert decision == Decision(True, "org_setting")

    def test_the_setting_does_not_leak_into_other_actions(self):
        """It grants PUBLISH, not everything. A single over-broad flag is how an
        organization setting becomes a privilege escalation."""
        for action in (Action.TAKEDOWN, Action.MANAGE_REGISTRY, Action.MANAGE_ORG):
            assert not check(teacher(), action, OURS,
                             org_settings={"teacher_can_publish": True}).allowed

    def test_a_centre_admin_publishes_without_it(self):
        assert check(centre_admin(), Action.PUBLISH, OURS).allowed


class TestTeachersAndOtherPeoplesWork:
    def test_a_teacher_cannot_edit_a_colleagues_material_by_default(self):
        decision = check(teacher(user_id=COLLEAGUE), Action.EDIT, OURS)
        assert decision == Decision(False, "not_the_author")

    def test_a_teacher_cannot_delete_it_either(self):
        decision = check(teacher(user_id=COLLEAGUE), Action.DELETE, OURS)
        assert decision == Decision(False, "not_the_author")

    def test_the_centre_can_allow_it(self):
        assert check(teacher(user_id=COLLEAGUE), Action.EDIT, OURS,
                     org_settings={"content_edit_others": True}).allowed

    def test_a_centre_admin_is_not_subject_to_it(self):
        assert check(centre_admin(user=COLLEAGUE), Action.EDIT, OURS).allowed

    def test_a_teacher_may_still_edit_their_own(self):
        assert check(teacher(), Action.EDIT, OURS).allowed


class TestPublishedContentSurvivesDeletion:
    """Not a role rule — a liability one. Attempts reference published content by
    id, and a copyright takedown needs the material preserved as evidence
    (`0009` §5). So the answer is archive, and it is the same for everybody."""

    PUBLISHED = Resource(org_id=HOME, owner_user_id=AUTHOR, status="published")

    def test_the_author_cannot_hard_delete_it(self):
        decision = check(teacher(), Action.DELETE, self.PUBLISHED)
        assert decision == Decision(False, "published_content_is_archived_not_deleted")

    def test_neither_can_the_centre_admin(self):
        assert not check(centre_admin(), Action.DELETE, self.PUBLISHED).allowed

    def test_a_draft_can_be_deleted(self):
        draft = Resource(org_id=HOME, owner_user_id=AUTHOR, status="draft")
        assert check(teacher(), Action.DELETE, draft).allowed

    def test_archiving_it_is_permitted(self):
        assert check(centre_admin(), Action.ARCHIVE, self.PUBLISHED).allowed


class TestRequireRaises:
    def test_it_raises_forbidden_carrying_the_reason(self):
        with pytest.raises(Forbidden) as refused:
            require(outsider(), Action.EDIT, OURS)
        assert refused.value.code == "edit_not_permitted"
        # `reason` rides in `extra`, which `as_problem` merges into the RFC 9457
        # body — so this is also the assertion that the denial reason is part of
        # the wire contract, not just a log line.
        assert refused.value.extra["reason"] == "not_a_member"

    def test_it_returns_quietly_when_allowed(self):
        assert require(teacher(), Action.EDIT, OURS) is None


# ── filter_content ───────────────────────────────────────────────────

Base = declarative_base()
Thing = Table(
    "things", Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("org_id", Integer),
    Column("owner_user_id", Integer),
    Column("visibility", String),
)


class _Model:
    id = Thing.c.id
    org_id = Thing.c.org_id
    owner_user_id = Thing.c.owner_user_id
    visibility = Thing.c.visibility


def _sql(query: Select) -> str:
    return str(query.compile(compile_kwargs={"literal_binds": True}))


class TestListScoping:
    """The half the module docstring calls load-bearing: almost every real
    multi-tenant leak is a missing list-scope, not a missing detail-check."""

    def test_a_platform_admin_gets_no_where_clause(self):
        query = filter_content(platform_admin(), select(Thing), _Model)
        assert "WHERE" not in _sql(query)

    def test_everyone_else_gets_one(self):
        """There is no code path that returns content unscoped. If this ever
        stops holding, every listing endpoint leaks at once."""
        for actor in (teacher(), student(), outsider(),
                      FakeActor(org_ids=(), roles={})):
            assert "WHERE" in _sql(filter_content(actor, select(Thing), _Model))

    def test_it_admits_platform_global_content(self):
        sql = _sql(filter_content(teacher(), select(Thing), _Model))
        assert "visibility = 'platform_global'" in sql

    def test_it_admits_the_actors_own_organizations(self):
        actor = FakeActor(org_ids=(HOME, 7), roles={HOME: "teacher"})
        sql = _sql(filter_content(actor, select(Thing), _Model))
        assert "org_id IN (1, 7)" in sql

    def test_an_actor_with_no_organization_gets_no_org_clause(self):
        """An empty `IN ()` is a SQL error on some drivers and a match-nothing on
        others. Neither is a thing to find out during a listing request."""
        sql = _sql(filter_content(FakeActor(org_ids=(), roles={}),
                                  select(Thing), _Model))
        assert "org_id IN" not in sql

    def test_it_admits_the_actors_own_private_drafts(self):
        sql = _sql(filter_content(teacher(), select(Thing), _Model))
        assert "owner_user_id = 10" in sql and "author_private" in sql

    def test_it_admits_explicitly_granted_rows(self):
        """The sharing mechanism. Never executed before this test: a paper shared
        with another centre on purpose is the only way content crosses an
        organization boundary, and it was the one route with no coverage."""
        sql = _sql(filter_content(teacher(), select(Thing), _Model,
                                  grant_ids={4, 5}))
        assert "id IN (4, 5)" in sql

    def test_no_grants_adds_no_clause(self):
        for grants in (None, set()):
            sql = _sql(filter_content(teacher(), select(Thing), _Model,
                                      grant_ids=grants))
            assert "things.id IN" not in sql
