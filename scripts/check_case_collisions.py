#!/usr/bin/env python3
"""Two files whose names differ only by case. Invisible on Linux, fatal elsewhere.

`web/src/features/governance/` held both:

    Exposure.tsx     the screen
    exposure.ts      burnPercent, rankByBurn, and `interface Exposure`

and `app/App.tsx` imported the screen the way it imports every other one:

    import { Exposure } from "../features/governance/Exposure";

On Linux that resolves to `Exposure.tsx`, because `Exposure.ts` does not exist.
On Windows and on a default macOS volume the filesystem is case-INSENSITIVE, so
`Exposure.ts` does exist — it is `exposure.ts` — and both TypeScript and esbuild
try `.ts` BEFORE `.tsx`. The import silently binds to the wrong module:

    ✘ [ERROR] No matching export in "src/features/governance/Exposure.ts"
      for import "Exposure"

Two screens gone, and the console does not start. It reproduces on every Windows
and most macOS machines and on none of ours: CI runs Linux, the production image
builds on Linux, every test here passes on Linux. A defect that is a property of
the *reader's filesystem* cannot be found by reading, and this repository's CI
had no way to see it at all.

## The two rules

**Full names, everywhere.** Two files in one directory whose complete names
differ only by case cannot both be checked out on a case-insensitive filesystem.
Git will write one and report the other as modified, forever, and no amount of
`git checkout` fixes it. This is worse than the resolution bug: the file is not
merely shadowed, it is absent.

**Stems, for anything importable.** Two files in one directory whose names
differ only by case before the extension, where both extensions are ones a
bundler will try. `Exposure.tsx` and `exposure.ts` are that; `BandMaps.tsx` and
`bandmaps.css` are not, because `.css` is never a candidate for an extensionless
import — an import of a stylesheet always spells the extension out.

## The convention this enforces, which was already the convention

Every other feature directory names its logic module for what it holds rather
than for the screen beside it: `bandMapTable.ts` next to `BandMaps.tsx`,
`slotRules.ts` next to `Slots.tsx`, `queue.ts` and `action.ts` next to
`Moderation.tsx`, `money.ts` next to `Billing.tsx`. `bandMapTable.ts` is the
evidence that this was deliberate — the obvious name was `bandmaps.ts` and
somebody passed it over. Governance was the one place it slipped, twice.
"""

from __future__ import annotations

import collections
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent

#: Extensions a bundler or `tsc` will append to an extensionless import, in the
#: order they are tried. Vite's `resolve.extensions` default, which is also
#: TypeScript's order for the ones they share — `.ts` before `.tsx` in both, and
#: that ordering is the whole bug.
#:
#: `.css` is deliberately absent. A stylesheet is imported with its extension
#: spelled out, so it is never a candidate and `Moderation.tsx` beside
#: `moderation.css` is not a defect. A rule that flagged it would be asking for a
#: rename with nothing behind it, and gates that cry wolf stop being read.
RESOLVABLE = (".mjs", ".js", ".mts", ".ts", ".jsx", ".tsx", ".json")


def tracked_files() -> list[PurePosixPath]:
    """What git has. Not a filesystem walk: `node_modules` is 95 directories of
    other people's naming decisions, and this gate has nothing to say about them.
    """
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [PurePosixPath(name) for name in listing.split("\0") if name]


def collisions(files: list[PurePosixPath], key) -> list[tuple[str, list[str]]]:
    """Directories where `key` maps two differently-cased names to one value."""
    grouped: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for path in files:
        identity = key(path)
        if identity is None:
            continue
        grouped[(str(path.parent), identity.lower())].add(path.name)
    return [
        (directory, sorted(names))
        for (directory, _), names in sorted(grouped.items())
        if len({PurePosixPath(name).stem for name in names}) > 1
        or len({name.lower() for name in names}) < len(names)
    ]


def main() -> int:
    files = tracked_files()
    if not files:
        # A selector that found nothing is a broken gate, not a clean one — the
        # same guard every other check in this directory carries.
        print("FAIL  git ls-files returned nothing — is this a repository?")
        return 1

    problems: list[str] = []

    for directory, names in collisions(files, lambda path: path.name):
        problems.append(
            f"{directory}/: {' and '.join(names)} differ only by case. Git cannot check "
            f"both out on Windows or macOS — one of them will be permanently missing."
        )

    importable = [path for path in files if path.suffix in RESOLVABLE]
    for directory, names in collisions(importable, lambda path: path.stem):
        winner = min(names, key=lambda name: RESOLVABLE.index(PurePosixPath(name).suffix))
        problems.append(
            f"{directory}/: {' and '.join(names)} differ only by case before the extension. "
            f"On a case-insensitive filesystem an extensionless import of either one "
            f"resolves to `{winner}`, silently. Rename one for what it holds."
        )

    for problem in problems:
        print(f"FAIL  {problem}")
    if problems:
        return 1

    print(f"PASS  no case-only name collisions ({len(files)} tracked files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
