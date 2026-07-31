"""The matcher. Pure — no database, no clock of its own, no I/O.

This file carries the one safety invariant in the product that is not merely a
correctness bug:

    A MINOR IS NEVER PAIRED 1:1 WITH AN ADULT.

It is enforced here, at the matching layer, because that is the only place it can
be enforced completely. The slot list filters and the booking endpoint re-checks,
but both are reachable only through HTTP; the matcher is the single point every
pair passes through, whatever created it.

The enforcement is structural rather than a filter: candidates are PARTITIONED on
`is_minor` before any pairing happens, so a cross-band pair is not rejected — it
is unreachable, because the two candidates are never in the same list. A final
assertion re-checks each pair anyway and raises rather than returning it, on the
principle that this failure must be loud.

**`mixed_supervised` does not weaken it.** That band exists so a teacher-run
cohort session may contain both minors and adults in one ROOM; it does not
authorize a minor–adult PAIR. A supervising adult with no adult peer simply goes
unmatched, which is correct — they are supervising, not practising.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal


class UnsafePair(Exception):
    """Raised rather than returned. A cross-band pair is not a bad match to be
    filtered out downstream; it is a bug that must stop the batch."""


@dataclass(frozen=True, slots=True)
class Candidate:
    user_xid: str
    # Derived by the caller from `users.adult_at`, never self-reported and never
    # taken from the request body.
    is_minor: bool
    language: str = "en"
    band: Decimal | None = None
    waiting_since: dt.datetime | None = None
    org_xid: str | None = None
    org_only: bool = False
    # Who this user has spoken with recently, and who they have blocked or been
    # blocked by. Both are passed in so this stays a pure function.
    recent_partners: frozenset[str] = field(default_factory=frozenset)
    blocked: frozenset[str] = field(default_factory=frozenset)

    @property
    def cohort_key(self) -> tuple[bool, str]:
        """The partition key. `is_minor` leads, so the safety rule is a property
        of the data structure rather than of the code that walks it."""
        return (self.is_minor, self.language)


@dataclass(frozen=True, slots=True)
class Pair:
    a: Candidate
    b: Candidate
    age_band: str
    band_gap: Decimal | None
    repeat: bool

    @property
    def user_xids(self) -> tuple[str, str]:
        return (self.a.user_xid, self.b.user_xid)


@dataclass(frozen=True, slots=True)
class Outcome:
    pairs: tuple[Pair, ...]
    unmatched: tuple[Candidate, ...]

    @property
    def matched_count(self) -> int:
        return len(self.pairs) * 2


# A band is a self-reported estimate, so a gap of one is noise and a gap of three
# is two people who cannot hold a conversation. Two is the widest useful pairing.
MAX_BAND_GAP = Decimal("2.0")


def match(candidates: list[Candidate], *, allow_repeats: bool = True) -> Outcome:
    """Pair up a pool. Deterministic: the same input always gives the same output.

    Determinism matters more than it looks. A matcher that shuffles cannot be
    reproduced when a student reports "I was paired with someone unsuitable", and
    a safety investigation that cannot reproduce the decision is not much of an
    investigation.
    """
    partitions: dict[tuple[bool, str], list[Candidate]] = {}
    for candidate in candidates:
        partitions.setdefault(candidate.cohort_key, []).append(candidate)

    pairs: list[Pair] = []
    unmatched: list[Candidate] = []
    for key in sorted(partitions, key=lambda k: (k[0], k[1])):
        made, left = _pair_within(partitions[key], allow_repeats=allow_repeats)
        pairs.extend(made)
        unmatched.extend(left)

    for pair in pairs:
        if pair.a.is_minor != pair.b.is_minor:                       # never reached
            raise UnsafePair(
                f"{pair.a.user_xid} and {pair.b.user_xid} are in different age "
                "bands. This is a bug in the partitioning, not a match to reject.")
    return Outcome(tuple(pairs), tuple(sorted(unmatched, key=_order)))


def _pair_within(pool: list[Candidate], *,
                 allow_repeats: bool) -> tuple[list[Pair], list[Candidate]]:
    """Greedy nearest-band pairing.

    Band proximity rather than arrival order: two people three bands apart have a
    bad fifteen minutes, and the student who was carrying the conversation does
    not come back. Within that, longest-waiting first, so nobody starves.
    """
    ordered = sorted(pool, key=_order)
    taken: set[str] = set()
    pairs: list[Pair] = []

    for candidate in ordered:
        if candidate.user_xid in taken:
            continue
        partner = _best_partner(candidate, ordered, taken, allow_repeat=False)
        repeat = False
        if partner is None and allow_repeats:
            # Better a repeat partner than a fifteen-minute wait that ends in
            # nothing. Recorded on the pair so the anti-repeat rule can be tuned
            # against real numbers rather than a guess.
            partner = _best_partner(candidate, ordered, taken, allow_repeat=True)
            repeat = partner is not None
        if partner is None:
            continue
        taken.update({candidate.user_xid, partner.user_xid})
        pairs.append(Pair(a=candidate, b=partner,
                          age_band="minor" if candidate.is_minor else "adult",
                          band_gap=_gap(candidate, partner), repeat=repeat))

    return pairs, [c for c in ordered if c.user_xid not in taken]


def _best_partner(candidate: Candidate, pool: list[Candidate], taken: set[str], *,
                  allow_repeat: bool) -> Candidate | None:
    best: Candidate | None = None
    best_gap: Decimal | None = None
    for other in pool:
        if other.user_xid == candidate.user_xid or other.user_xid in taken:
            continue
        if not _compatible(candidate, other, allow_repeat=allow_repeat):
            continue
        gap = _gap(candidate, other)
        if gap is not None and gap > MAX_BAND_GAP:
            continue
        if best is None or _closer(gap, best_gap):
            best, best_gap = other, gap
    return best


def _compatible(a: Candidate, b: Candidate, *, allow_repeat: bool) -> bool:
    """Every pairwise rule, in one predicate.

    Two of these are also enforced structurally by `cohort_key`, which is why
    `match()` never reaches them: age and language partition the pool before
    anything is compared. They stay here because this predicate is what a second
    way of building a pair would call, and the module's whole claim is that the
    matcher is "the single point every pair passes through, whatever created it".
    A rule that lives only in the partition key is a rule that a future caller
    can walk around.
    """
    # A block is absolute and mutual regardless of who filed it. Someone who
    # blocked a user must never be handed back to them by the matcher, and a user
    # must not learn they were blocked by noticing they stopped being matched.
    if b.user_xid in a.blocked or a.user_xid in b.blocked:
        return False
    if a.language != b.language:
        return False
    if a.org_only or b.org_only:
        # `a.org_xid != b.org_xid` alone let two users with NO organization match
        # while one of them had asked for their centre only — SQL's NULL-equality
        # trap in Python form, where "neither of us has an org" read as "we are in
        # the same org". An org-only request needs an org to be about.
        if a.org_xid is None or b.org_xid is None or a.org_xid != b.org_xid:
            return False
    if not allow_repeat and (b.user_xid in a.recent_partners
                             or a.user_xid in b.recent_partners):
        return False
    return True


def _gap(a: Candidate, b: Candidate) -> Decimal | None:
    if a.band is None or b.band is None:
        return None
    return abs(a.band - b.band)


def _closer(gap: Decimal | None, best: Decimal | None) -> bool:
    """An unknown band sorts after every known one.

    Pairing two people whose levels are both unknown is a coin flip; pairing two
    who are both band 6 is a good fifteen minutes. Prefer the information.
    """
    if gap is None:
        return False
    if best is None:
        return True
    return gap < best


def _order(candidate: Candidate) -> tuple:
    """Longest-waiting first, then by band, then by xid for determinism."""
    return (
        candidate.waiting_since or dt.datetime.max.replace(tzinfo=dt.UTC),
        candidate.band if candidate.band is not None else Decimal(99),
        candidate.user_xid,
    )
