#!/usr/bin/env python3
"""Does every compose service that builds the Dockerfile say WHICH stage?

`docker-compose.yml` had this, for four services:

    x-app: &app
      build:
        context: .
        dockerfile: Dockerfile

No `target:`. A multi-stage build with no target builds the LAST stage, and the
last stage of this Dockerfile is `caddy` — the console baked into a web server,
which is right for the `caddy` service and catastrophic for the other four. The
api, the worker, the scheduler and the migrate one-shot would every one of them
have come up as a Caddy image and died on `uvicorn: executable file not found`.

The whole production deployment, unable to start. Nothing caught it, because
nothing here can run a Docker daemon: `docker compose config` validates the
schema and says nothing about stages, and the build has never been performed in
CI. The file was read many times and looked right — a missing key looks like
nothing at all, which is the same reason `check_ci_parity.py` exists.

## The rule

Any service whose `build` points at a Dockerfile with more than one named stage
must name a `target`, and that target must be a stage that exists.

Both halves matter and the second is not theoretical: `target: runtime` is only
correct while a stage called `runtime` is still called that. Renaming a stage is
a one-word edit in the Dockerfile that silently breaks a file three directories
away, which is the shape of defect this directory keeps being written for.

Implicitly-last is banned even when it happens to be right today, because
"correct until someone appends a stage" is not a property worth having. Adding a
stage to the end of a Dockerfile must never be able to change what an existing
service builds — and here it could, twice: `web` was appended after `runtime`,
then `caddy` after `web`.

## What this does not check

Whether the stage does the right thing. `target: web` on the api service passes
this and fails at once when run; a gate that could tell those apart would have to
build the image, and the argument for not building images in CI (docs/design/
0011-ci.md §12 — an eleven-minute pipeline on a free runner) still holds. This
closes the failure that a human reader cannot see, and leaves the one they can.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

#: `FROM x AS name`. Case-insensitive because Docker accepts `from`/`as` in any
#: case and a gate that only understands the shouty convention would pass a file
#: it had failed to parse — silently, and in the permissive direction.
_STAGE = re.compile(r"^\s*FROM\s+\S+\s+AS\s+(\S+)", re.IGNORECASE | re.MULTILINE)


def stages(dockerfile: Path) -> list[str]:
    """Every named build stage, in file order. The last one is the default."""
    return _STAGE.findall(dockerfile.read_text())


def services(compose: Path) -> dict[str, dict]:
    """The services in a compose file, with `<<:` merges already applied.

    `yaml.safe_load` resolves merge keys itself, which is what makes this
    readable: `x-app: &app` carries the build block and four services inherit it
    through `<<: *app`, so the anchor is where a missing target hides — one
    place, four failures, and none of them named in the service that has the
    bug.
    """
    document = yaml.safe_load(compose.read_text()) or {}
    return document.get("services", {}) or {}


def check(compose: Path) -> list[str]:
    problems: list[str] = []

    for name, service in sorted(services(compose).items()):
        build = service.get("build")
        if build is None:
            continue                       # a registry image; nothing to target
        # `build: .` is legal shorthand for `build: {context: .}`.
        if isinstance(build, str):
            build = {"context": build}

        dockerfile = ROOT / build.get("dockerfile", "Dockerfile")
        if not dockerfile.is_file():
            problems.append(f"{compose.name}: {name} builds {dockerfile}, which does not exist")
            continue

        declared = stages(dockerfile)
        if len(declared) < 2:
            continue                       # single-stage: the default is the only one

        target = build.get("target")
        if target is None:
            problems.append(
                f"{compose.name}: service `{name}` builds {dockerfile.name} with no `target:`, "
                f"so it gets the last stage — `{declared[-1]}`. Say which stage it wants."
            )
        elif target not in declared:
            problems.append(
                f"{compose.name}: service `{name}` wants stage `{target}`, which "
                f"{dockerfile.name} does not declare. It has: {', '.join(declared)}"
            )

    return problems


def main() -> int:
    files = sorted(ROOT.glob("docker-compose*.yml"))
    if not files:
        # The selector found nothing, which is a broken gate rather than a clean
        # one. Every other check in this directory carries the same guard, for
        # the same reason: a rename upstream must fail loudly here.
        print("FAIL  no docker-compose*.yml found — has the layout changed?")
        return 1

    problems = [problem for compose in files for problem in check(compose)]
    for problem in problems:
        print(f"FAIL  {problem}")
    if problems:
        return 1

    print(f"PASS  every built service names a stage that exists ({len(files)} compose files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
