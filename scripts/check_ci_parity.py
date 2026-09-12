#!/usr/bin/env python3
"""Does CI actually run every gate `make ci` runs?

The Makefile says `ci: ci-checks ci-tests  ## The whole pipeline, exactly as CI
runs it`. That comment was false. Three gates were in the aggregates and in no
workflow step:

    console       every admin endpoint has a screen
    write-paths   every filtered column has a writer
    web-lint      eslint over the console

`write-paths` is the sharpest example, because it was added to `ci-tests` in the
same commit that introduced it and CI never ran it once. A gate nobody runs is a
file, not a gate — and the failure mode is silent in the worst direction: the
build is green, the check exists, and the person who wrote it believes it is
guarding something.

This is the same defect the rest of this directory keeps finding, one level up.
`check_write_paths.py` asks whether a column some query reads is ever written;
`check_console_coverage.py` asks whether an endpoint some contract declares has a
screen. This asks whether a gate the build claims to run is ever invoked. In each
case two places have to agree and nothing was looking at the relationship.

## Why the workflow does not simply call `make ci`

Because it deliberately does not, and the reason is good: ci.yml says "Each on
its own step so the failing gate is the step name in the UI, not a line to find
in a combined log." That is worth keeping. The cost is drift, and the answer to
drift is a check rather than a convention.

So: every target named in `ci-checks` and `ci-tests` must appear as `make
<target>` in some step of the workflow. The reverse is not required — a workflow
may run extra things (`npm run build`), and it may split a target across jobs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

#: Aggregates whose members CI must run. Both, because the split between them is
#: about which services a gate needs, not about which ones matter.
AGGREGATES = ("ci-checks", "ci-tests")

#: Targets that are deliberately not their own CI step, with the reason.
#:
#: Empty, and it should stay that way: a gate worth having in `make ci` is a
#: gate worth running in CI. A stale entry fails the build the same way the
#: other two gate lists do — the first thing put here was `coverage`, which CI
#: has always run, and this check caught that on its first execution.
EXEMPT: dict[str, str] = {}


def prerequisites(target: str, text: str) -> list[str]:
    """The targets an aggregate depends on, minus the `## help` comment."""
    match = re.search(rf"^{re.escape(target)}:([^\n#]*)", text, re.M)
    if not match:
        raise SystemExit(f"FAIL  Makefile has no `{target}` target")
    return match.group(1).split()


#: A YAML comment: a whole line of one, or a trailing one after whitespace.
#: `#` inside a quoted string with no space before it survives, which is the
#: only kind a workflow step would legitimately carry.
_COMMENT = re.compile(r"(?m)^\s*#.*$|\s#.*$")


def invoked_targets(workflow: str) -> set[str]:
    """Every `make <target>` the workflow's STEPS run. Comments do not count.

    They did. The match ran over the raw file, and ci.yml explains its steps in
    comments that name the same targets: "`make coverage` is `test-fast` plus
    the per-path floors" three lines above `run: make coverage`. Delete the
    `run:` line and `coverage` stayed in the invoked set through its own
    explanation, so the gate whose docstring says a gate nobody runs is
    "green, present, and guarding nothing" would itself have been exactly that
    while the suite and every floor stopped running. The docstring above
    states the requirement as "in some step"; this makes the code say the same.
    """
    return set(re.findall(r"make\s+([a-z][a-z0-9-]*)", _COMMENT.sub("", workflow)))


def main() -> int:
    makefile = MAKEFILE.read_text()
    workflow = WORKFLOW.read_text()

    invoked = invoked_targets(workflow)

    wanted: list[str] = []
    for aggregate in AGGREGATES:
        wanted.extend(prerequisites(aggregate, makefile))

    missing = [t for t in wanted if t not in invoked and t not in EXEMPT]
    stale = sorted(set(EXEMPT) & (invoked | {t for t in wanted if t in invoked}))

    print(f"gates in {' + '.join(AGGREGATES)}   {len(wanted)}")
    print(f"invoked by ci.yml            {len(set(wanted) & invoked)}")

    if missing:
        print(f"\nFAIL  {len(missing)} gates run in `make ci` and in NO CI step.")
        print("      A gate the build claims to run and does not is worse than")
        print("      no gate: green, present, and guarding nothing. Add a step")
        print("      to .github/workflows/ci.yml, or an EXEMPT entry saying why.")
        for target in missing:
            print(f"        make {target}")
    if stale:
        print(f"\nFAIL  {len(stale)} exemptions are stale — CI runs these now.")
        for target in stale:
            print(f"        {target}  ({EXEMPT[target]})")

    if missing or stale:
        return 1
    print("\nPASS  every gate in `make ci` has a CI step")
    return 0


if __name__ == "__main__":
    sys.exit(main())
