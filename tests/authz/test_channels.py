"""The channel grammar and the rules, without a database.

The half of `authz/channels.py` that needs no lookup: parsing, the event
declarations, and the four rules applied to a `Subject` built by hand. The other
half — what the database actually says about a slot, an assignment or a contest
— is in `tests/integration/test_realtime.py`, against real rows, because a rule
tested only against a `Subject` a test wrote is a rule tested against the test's
opinion of the schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.modules.authz import channels

ME = "018f1111-1111-7111-8111-111111111111"
SOMEONE_ELSE = "018f2222-2222-7222-8222-222222222222"


@dataclass
class Actor:
    user_id: int = 1
    user_xid: str = ME
    org_ids: tuple[int, ...] = ()
    roles: dict[int, str] = field(default_factory=dict)
    platform_roles: tuple[str, ...] = ()

    @property
    def is_platform_admin(self) -> bool:
        return "platform_admin" in self.platform_roles


class TestParsing:
    def test_a_well_formed_channel_parses(self):
        parsed = channels.parse(f"attempt:{ME}")
        assert parsed == channels.Channel("attempt", ME)
        assert str(parsed) == f"attempt:{ME}"

    def test_case_and_braces_normalise_to_one_channel(self):
        """`{ABC…}` and `abc…` are the same UUID and must not become two
        channels — one subscribed and one silently empty."""
        braced = channels.parse("attempt:{" + ME.upper() + "}")
        assert str(braced) == f"attempt:{ME}"

    @pytest.mark.parametrize("name", [
        "",
        "attempt",                       # a family that needs a subject, without one
        "attempt:not-a-uuid",
        "nonsense:" + ME,
        "safety:" + ME,                  # a family with no subject, given one
        "a" * 200,
    ])
    def test_rubbish_does_not_parse(self, name):
        assert channels.parse(name) is None

    def test_a_non_string_does_not_parse(self):
        """The frame is JSON written by the client, so `channels: [17]` is a
        thing that arrives."""
        assert channels.parse(17) is None

    def test_a_subject_less_family_parses_bare(self):
        assert channels.parse("safety") == channels.Channel("safety")

    def test_an_unparseable_channel_is_unknown_not_forbidden(self):
        """Two different codes for two different client behaviours: `unknown` is
        "stop asking", `forbidden` is "you cannot read this one"."""
        channel, verdict = channels.decide(Actor(), "nonsense:1", None)
        assert channel is None
        assert not verdict.allowed
        assert verdict.code == channels.UNKNOWN_CHANNEL


class TestDeclaredEvents:
    def test_every_family_declares_at_least_one_event(self):
        assert all(family.events for family in channels.FAMILIES.values())

    def test_an_event_declared_nowhere_is_refused(self):
        with pytest.raises(channels.UndeclaredEvent):
            channels.assert_carries(f"user:{ME}", "leaderboard.delta")

    def test_the_invigilation_view_may_not_be_addressed_to_a_person(self):
        """The publish-side half of the org-private promise. `assignment.progress`
        is one teacher's view of forty students; addressed to `user:{someone}` it
        would reach a reader the subscribe path correctly admitted."""
        with pytest.raises(channels.UndeclaredEvent):
            channels.assert_carries(f"user:{ME}", "assignment.progress")
        channels.assert_carries(f"assignment:{ME}", "assignment.progress")

    def test_signalling_is_declared_only_on_the_pair_channel(self):
        assert channels.carries("pair", "signal.offer")
        assert not channels.carries("competition", "signal.offer")

    def test_an_unknown_family_carries_nothing(self):
        assert not channels.carries("nonsense", "notification")


class TestThePersonalChannel:
    def test_a_user_may_read_their_own(self):
        _, verdict = channels.decide(Actor(), f"user:{ME}", None)
        assert verdict.allowed

    def test_a_user_may_not_read_someone_elses(self):
        _, verdict = channels.decide(Actor(), f"user:{SOMEONE_ELSE}", None)
        assert not verdict.allowed
        assert verdict.code == channels.FORBIDDEN_CHANNEL

    def test_not_even_a_platform_admin(self):
        """Deliberate divergence from `policy.check`, which grants a platform
        admin everything. `user:` carries a ban landing and a band changing;
        there is no support task that needs another person's live copy, and the
        bypass would be a supervisor tailing a student's stream."""
        admin = Actor(platform_roles=("platform_admin",))
        _, verdict = channels.decide(admin, f"user:{SOMEONE_ELSE}", None)
        assert not verdict.allowed


class TestTheSafetyChannel:
    def test_staff_only(self):
        _, verdict = channels.decide(Actor(platform_roles=("platform_admin",)),
                                     "safety", None)
        assert verdict.allowed

    def test_an_ordinary_user_is_refused(self):
        _, verdict = channels.decide(Actor(), "safety", None)
        assert not verdict.allowed
        assert verdict.code == channels.FORBIDDEN_CHANNEL

    def test_a_centre_admin_is_still_refused(self):
        """Centre staff are not platform staff. The moderation queue names
        reporters, subjects and minors, and its highest-priority entries are the
        ones a centre would most want to see about its own students."""
        _, verdict = channels.decide(Actor(org_ids=(7,), roles={7: "centre_admin"}),
                                     "safety", None)
        assert not verdict.allowed


class TestMembershipRules:
    """`_party_to_it` and `_centre_staff` against `Subject`s built by hand.

    Integration covers the same rules against rows the schema actually produces.
    """

    def test_a_party_is_admitted(self):
        assert channels._party_to_it(
            Actor(user_id=5), channels.Subject(member_user_ids=frozenset({5, 6}))).allowed

    def test_a_stranger_is_not(self):
        assert not channels._party_to_it(
            Actor(user_id=9), channels.Subject(member_user_ids=frozenset({5, 6}))).allowed

    def test_a_platform_admin_may_investigate(self):
        assert channels._party_to_it(
            Actor(user_id=9, platform_roles=("platform_admin",)),
            channels.Subject(member_user_ids=frozenset({5, 6}))).allowed

    def test_the_assigning_teacher_is_admitted_whatever_their_current_roles(self):
        """They may have been moved to another centre since. It is still their
        assignment."""
        verdict = channels._centre_staff(
            Actor(user_id=5), channels.Subject(org_id=7, owner_user_id=5))
        assert verdict.allowed

    def test_a_competitor_centre_is_refused(self):
        verdict = channels._centre_staff(
            Actor(user_id=9, org_ids=(8,), roles={8: "centre_admin"}),
            channels.Subject(org_id=7, owner_user_id=5))
        assert not verdict.allowed
        assert verdict.reason == "not_this_centre"

    def test_a_student_at_the_right_centre_is_refused(self):
        """Org membership is necessary and not sufficient: the roster includes
        the students whose progress this channel reports."""
        verdict = channels._centre_staff(
            Actor(user_id=9, org_ids=(7,), roles={7: "student"}),
            channels.Subject(org_id=7, owner_user_id=5))
        assert not verdict.allowed
        assert verdict.reason == "not_centre_staff"

    def test_a_colleague_teacher_is_admitted(self):
        verdict = channels._centre_staff(
            Actor(user_id=9, org_ids=(7,), roles={7: "teacher"}),
            channels.Subject(org_id=7, owner_user_id=5))
        assert verdict.allowed

    def test_a_platform_admin_is_admitted(self):
        verdict = channels._centre_staff(
            Actor(user_id=9, platform_roles=("platform_admin",)),
            channels.Subject(org_id=7, owner_user_id=5))
        assert verdict.allowed


class TestImplicitChannels:
    def test_everyone_gets_their_own(self):
        assert channels.describe(Actor())["implicit_channels"] == [f"user:{ME}"]

    def test_staff_also_get_the_moderation_stream(self):
        described = channels.describe(Actor(platform_roles=("platform_admin",)))
        assert described["implicit_channels"] == [f"user:{ME}", "safety"]
