"""The three guards in `grants.py` that decline to answer.

`tests/integration/test_content_grants_reach.py` drives real grants through the
API and proves the mechanism. It cannot reach these, because each is the branch
taken when the caller asks about something that does not exist — an unknown
permission, an unknown subject type, a model outside the sharing vocabulary. A
fixture cannot produce a subject type the schema has no table for.

They matter more than "defensive code usually does", because this module is
authorization and each one decides what happens when the vocabulary drifts. The
sharing map is duplicated by design — `SUBJECT_TABLES` here and
`platform_ops._SUBJECT_TABLES` there, because this module must not import a
router — so "a kind one side knows and the other does not" is the failure mode
these three exist for. Each returns the CLOSED answer: no permissions widened,
no rows granted, no subject type invented.

`app/modules/authz/` has a 100% coverage floor and these were the only three
lines under it. That is what a floor is for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.modules.authz.grants import (
    HIERARCHY,
    SUBJECT_TABLES,
    granted_ids,
    satisfying,
    subject_type_of,
)


@dataclass
class FakeActor:
    user_id: int = 10
    org_ids: tuple[int, ...] = field(default_factory=tuple)
    platform_admin: bool = False

    @property
    def is_platform_admin(self) -> bool:
        return self.platform_admin


class TestSatisfying:
    def test_the_hierarchy_widens_downward(self):
        """`copy` implies `assign` implies `view`, so a listing asking for
        `view` accepts all three."""
        assert satisfying("view") == HIERARCHY
        assert satisfying("copy") == ("copy",)

    def test_an_unknown_permission_widens_to_nothing(self):
        """The guard. A permission outside the hierarchy satisfies ITSELF and
        nothing else — the alternative reading, that an unrecognised string
        matches everything, would turn a typo in a `content_grants` row into a
        grant of `copy` on another centre's bank.
        """
        assert satisfying("bogus") == ("bogus",)
        assert satisfying("") == ("",)


class TestGrantedIds:
    def test_an_unknown_subject_type_grants_nothing(self):
        """The guard, and it returns BEFORE touching the session — which is why
        `None` is a safe thing to pass here and why the check has to come first:
        `subject_type` is interpolated into the FROM clause, so a kind with no
        table entry must never reach the query at all.
        """
        assert granted_ids(None, FakeActor(), "not_a_kind") == set()

    def test_a_platform_admin_gets_an_empty_set(self):
        """Not everything. `filter_content` returns unfiltered for them well
        before this is reached, so a set computed here would be discarded."""
        assert granted_ids(None, FakeActor(platform_admin=True), "test") == set()


class TestSubjectTypeOf:
    def test_a_known_model_resolves(self):
        class Passage:
            __tablename__ = "passages"

        assert subject_type_of(Passage) == "passage"

    def test_a_model_outside_the_vocabulary_is_none(self):
        """The guard. `None` means "this cannot be shared", and every caller
        treats it that way — inventing a subject type from an unmapped table
        would write a `content_grants` row nothing can ever read back."""
        class Attempt:
            __tablename__ = "attempts"

        assert subject_type_of(Attempt) is None

    def test_something_with_no_tablename_is_none(self):
        assert subject_type_of(object()) is None

    def test_every_mapped_table_round_trips(self):
        """The map is duplicated on purpose; this at least proves it is
        self-consistent."""
        for kind, table in SUBJECT_TABLES.items():
            class Model:
                pass

            Model.__tablename__ = table
            assert subject_type_of(Model) == kind
