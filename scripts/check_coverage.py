#!/usr/bin/env python3
"""Coverage floors, per path, and a deliberate refusal to set a global target.

A single repository-wide percentage is the wrong gate. It rewards testing
whatever is cheapest to cover, it goes up when someone deletes a hard-to-test
module, and once it is a number on a dashboard the honest answer to "should this
`except` branch have a test?" becomes "will it move the number?".

What is actually worth gating is narrower: **the code where an unexecuted line is
a security or correctness risk.** Those get a floor of 100 and a ratchet. Every
one of them is at 100 today, so the floor costs nothing to hold and fails loudly
the moment a new branch lands without a test.

The global figure is reported, not enforced, with one exception: a floor low
enough that only a collapse trips it (a conftest that stops importing, a suite
that silently stops collecting half of itself). That is a smoke alarm, not a
target — and this repository has already had exactly that failure once, when
`pytest -n 4` reported "355 passed, 400 skipped" and exited 0.

    make coverage
    python3 scripts/check_coverage.py --report   # print the table, gate nothing
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "coverage.json"

# path prefix -> minimum percent. Ordered most-specific first; a file is judged
# by the first prefix it matches.
#
# The justification is the point. A floor without one is a number somebody will
# lower during a bad afternoon.
FLOORS: tuple[tuple[str, int, str], ...] = (
    ("app/modules/authz/", 100,
     "The central policy. 'Enforce centrally, not with scattered role checks' is "
     "only true while every branch of it is exercised — and eleven of them were "
     "not, until a coverage run said so."),
    ("app/platform/grants.py", 100,
     "Media grants and object signatures. An unexecuted refusal branch is a "
     "verifier nobody has checked."),
    ("app/api/routers/auth.py", 100,
     "Sign-in, OTP and session rotation. It was at 59% and the unexecuted part "
     "contained a complete authentication bypass; an untested branch here is a "
     "login bypass by default."),
    ("app/api/routers/platform_ops.py", 95,
     "Takedowns, moderation, seat licences and BOTH payment callbacks. It was at "
     "65% and the gap contained an entirely unauthenticated Payme endpoint that "
     "marked any order paid. The remainder is the redirect-mode media path, "
     "which needs a config this suite does not run under."),
    ("app/api/routers/assets.py", 95,
     "The authoring core. It was at 70%, and the gap held a transcript endpoint "
     "any student at the centre could read — the answer sheet for the paper they "
     "were about to sit."),
    ("app/api/routers/identity.py", 100,
     "Profiles, consents, org rosters and invites. It was at 70%, and the gap "
     "returned every member's phone number and minor flag to any student at the "
     "centre."),
    ("app/api/routers/speaking.py", 100,
     "Age banding at the HTTP boundary and the ONE path by which conversation "
     "audio may reach these servers. It was at 81%, and the gap was the safety "
     "evidence upload: the bytes were read, hashed, recorded — and never stored. "
     "A minor–adult refusal or an evidence write that CI does not execute is a "
     "child-safety control nobody has run."),
    ("app/api/routers/competitions.py", 100,
     "The fairness rule: simultaneous start, the encrypted two-phase payload, and "
     "the ranking students screenshot. It was at 74%, and the gap held a capacity "
     "check that returned 500 every time (`count(*) ... FOR UPDATE`, which "
     "PostgreSQL rejects) and a leaderboard that ignored the contest's own "
     "tiebreak. A contest is worthless if its ranking is not the ranking it "
     "computed."),
    ("app/api/routers/tests_authoring.py", 100,
     "Composition — the assembly half of the authoring system, which the brief "
     "calls the core of the product. It was at 84%, and the gap held a section "
     "move that 500'd in one direction and silently left a hole in the section "
     "sequence in the other. A hole is a student who cannot enter the next "
     "section of a timed exam."),
    ("app/api/routers/teaching.py", 100,
     "Assignments and the regrade flow. It was at 83%, and the gap refused a "
     "teacher permission to assign to their own centre's students — it asked "
     "`cohort_members` where a roster is `org_memberships` — and reported a "
     "band-map regrade as touching zero attempts, in a flow whose entire purpose "
     "is showing a human the numbers before they commit."),
    ("app/api/routers/exam.py", 100,
     "The student's side of the exam: start, autosave, the clock, submit. It was "
     "at 89%, and the gap was the ENTIRE assigned-attempt path — `assignment_xid` "
     "was accepted and never read, so work a school set could only be sat as "
     "private practice, on the student's own entitlement, invisible to the "
     "teacher. A branch here that CI does not execute is a student stuck in a "
     "timed exam."),
    ("app/api/routers/authoring.py", 100,
     "Publish, the key fix, and import. It was at 95%, and the six lines held the "
     "import path — which had no authorization at all and captured no copyright "
     "attestation, on the one route by which a whole published paper arrives at "
     "once. 'Assume some centres WILL try to upload published Cambridge papers.'"),
    ("app/modules/exam/scoring.py", 100,
     "'The server is the sole authority on scoring.' A scoring line that never "
     "runs in CI is a band nobody has verified."),
    ("app/modules/qtypes/", 95,
     "The three scoring primitives every question type reduces to."),
    ("app/modules/exam/", 90,
     "Timing authority, play-once, and submit. The server is the sole authority "
     "on time remaining."),
    ("app/modules/content/", 90,
     "Versioning, the publish gate and import — the authoring core."),
    ("app/platform/", 85,
     "The kernel every other layer depends on."),
)

# Not a target. A tripwire for the suite collapsing, set well below the current
# figure so ordinary work never touches it.
GLOBAL_FLOOR = 80


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true",
                        help="print the table and exit 0 regardless")
    parser.add_argument("--json", type=Path, default=REPORT)
    args = parser.parse_args()

    if not args.json.exists():
        sys.exit(f"{args.json} not found — run `make coverage` first")
    data = json.loads(args.json.read_text())

    buckets: dict[str, list[int]] = {prefix: [0, 0] for prefix, _, _ in FLOORS}
    for name, entry in data["files"].items():
        prefix = _bucket(name)
        if prefix is None:
            continue
        buckets[prefix][0] += entry["summary"]["num_statements"]
        buckets[prefix][1] += entry["summary"]["missing_lines"]

    failures: list[str] = []
    print(f"{'path':34} {'stmts':>6} {'miss':>5} {'cov':>5}  floor")
    for prefix, floor, _why in FLOORS:
        statements, missing = buckets[prefix]
        if not statements:
            failures.append(f"{prefix} matched no files — has it moved or been "
                            "renamed? A floor over nothing always passes.")
            continue
        percent = 100.0 * (statements - missing) / statements
        flag = " " if percent >= floor else "✗"
        print(f"{flag} {prefix:32} {statements:6} {missing:5} {percent:4.0f}% {floor:>5}")
        if percent < floor:
            failures.append(
                f"{prefix} is {percent:.1f}%, floor {floor}%\n"
                f"      {_why}\n"
                f"      uncovered: {_uncovered(data, prefix)}")

    total = data["totals"]
    overall = total["percent_covered"]
    print(f"\n  {'TOTAL (reported, not a target)':32} "
          f"{total['num_statements']:6} {total['missing_lines']:5} {overall:4.0f}% "
          f"{GLOBAL_FLOOR:>5}")
    if overall < GLOBAL_FLOOR:
        failures.append(
            f"overall coverage {overall:.1f}% is below the {GLOBAL_FLOOR}% "
            "tripwire. This is not a quality target — it is set low enough that "
            "reaching it means something structural broke, most likely a chunk "
            "of the suite no longer running.")

    if args.report:
        return 0
    if failures:
        print()
        for failure in failures:
            print(f"  FAIL  {failure}")
        return 1
    print("\nPASS  every gated path is at or above its floor")
    return 0


def _bucket(name: str) -> str | None:
    for prefix, _, _ in FLOORS:
        if name == prefix or name.startswith(prefix):
            return prefix
    return None


def _uncovered(data: dict, prefix: str) -> str:
    """Name the files, so the failure is actionable without a second command."""
    worst = sorted(
        ((n, f) for n, f in data["files"].items()
         if _bucket(n) == prefix and f["missing_lines"]),
        key=lambda kv: -len(kv[1]["missing_lines"]))[:4]
    return "; ".join(f"{n}:{','.join(str(x) for x in f['missing_lines'][:8])}"
                     for n, f in worst) or "(none)"


if __name__ == "__main__":
    raise SystemExit(main())
