#!/usr/bin/env python3
"""Can `docker compose build` actually succeed? Asked without a daemon.

Nothing in this repository builds an image. CI does not (docs/design/0011-ci.md
§12: eleven minutes on a free runner, for a check a deploy performs anyway),
`docker compose config` validates compose schema and never opens the Dockerfile,
and no test can start a daemon. So the build definition was the one artefact here
with no gate on it at all — and it had three defects, none of which a human
reader can see, because all three are things that are *absent*.

    1.  `docker-compose.yml` built four services with no `target:`.
        A multi-stage build with no target builds the LAST stage, which here is
        `caddy`. The api, worker, scheduler and migrate one-shot would every one
        have come up as a web-server image and died on `uvicorn: executable file
        not found`.

    2.  `ARG NODE_IMAGE` was declared beside the stage that used it.
        An ARG after a FROM is scoped to that stage; only ARGs before the FIRST
        FROM can be interpolated into a later FROM. So `FROM ${NODE_IMAGE} AS
        web` had nothing to expand, and the file did not parse — meaning NOTHING
        built, including the two stages with no interest in Node:

            failed to solve: base name (${NODE_IMAGE}) should not be blank

    3.  `.dockerignore` excluded `Caddyfile`, which the last COPY reads.
        Context exclusions are invisible from the Dockerfile. The error is
        `"/Caddyfile": not found`, which blames a missing file. That file's own
        comments document this exact trap, for `openapi/`, twenty lines above the
        line that repeated it.

    4.  `docker-compose.yml` ran `python scripts/bootstrap.py` in an image that
        had no `scripts/`. The `migrate` one-shot chains it after `alembic
        upgrade head`; the runtime stage copied `app`, `registry`, `migrations`
        and two files, and `.dockerignore` excluded `scripts/` as CI-only. The
        image built. On the box `python` exited 2 with "can't open file",
        `sh -c` propagated it, and api, worker and scheduler waited on
        `service_completed_successfully` forever. The dev stack bind-mounts the
        tree over /app, so it never reproduced.

Each is the shape this directory keeps being written for: **two places have to
agree and nothing is looking at the relationship.** A compose file and a
Dockerfile's stage list; an ARG's position and a FROM's interpolation; a COPY
source and an ignore pattern; a service's `command` and the stage it runs in.

## What this cannot tell you

Whether a stage does the right thing, whether a package installs, whether the
image runs. `target: web` on the api service passes every check here and fails
the moment it starts. Only a build proves a build — this closes the failures a
reader cannot see and leaves the ones they can.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERIGNORE = ROOT / ".dockerignore"

#: `FROM x AS name`. Case-insensitive: Docker accepts `from`/`as` in any case,
#: and a parser that only understands the shouty convention would pass a file it
#: had failed to read — silently, and in the permissive direction.
_FROM = re.compile(r"^\s*FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE | re.MULTILINE)
_ARG = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE | re.MULTILINE)
#: `${NAME}` or `$NAME` inside a FROM's image reference.
_INTERPOLATION = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


# ── the Dockerfile ───────────────────────────────────────────────────────────

def instructions(dockerfile: Path) -> str:
    r"""The Dockerfile with comments dropped and line continuations joined.

    Both matter. A `# COPY something` in prose would otherwise be read as a COPY,
    and this repository's Dockerfile is more comment than instruction by volume.
    And

        COPY app        /app/app \
             registry   /app/registry

    is one instruction whose second source line would be invisible to a
    line-oriented regex — the direction that misses a defect rather than
    inventing one.
    """
    text = re.sub(r"\\\r?\n", " ", dockerfile.read_text())
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def stages(text: str) -> list[str]:
    """Every named build stage, in file order. The last one is the default."""
    return [name for _, name in _FROM.findall(text) if name]


def global_args(text: str) -> set[str]:
    """The ARGs declared before the first FROM — the only ones a FROM can use."""
    head = _FROM.split(text, maxsplit=1)[0]
    return set(_ARG.findall(head))


def check_from_args(dockerfile: Path, text: str) -> list[str]:
    declared = global_args(text)
    problems = []
    for image, _ in _FROM.findall(text):
        for name in _INTERPOLATION.findall(image):
            if name not in declared:
                problems.append(
                    f"{dockerfile.name}: `FROM {image}` interpolates {name}, which is not "
                    f"declared before the first FROM. An ARG after a FROM belongs to that "
                    f"stage; move it to the top or the whole file fails to parse."
                )
    return problems


# ── the build context ────────────────────────────────────────────────────────

def _to_regex(pattern: str) -> re.Pattern[str]:
    """One .dockerignore pattern, as a regex.

    `fnmatch` is wrong here and quietly so: it compiles `*` to `.*`, which
    crosses `/`. Docker uses Go's `filepath.Match`, where `*` stops at a
    separator and only `**` spans them. Under `fnmatch`, `var/` would match
    `web/src/var` — and a check that over-matches reports defects that are not
    there until somebody stops believing it.
    """
    out, index = [], 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


def ignore_rules(dockerignore: Path) -> list[tuple[re.Pattern[str], bool]]:
    """Every pattern, in file order, paired with whether it re-includes."""
    rules = []
    for raw in dockerignore.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        rules.append((_to_regex(line.lstrip("!").rstrip("/")), negated))
    return rules


def is_excluded(path: str, rules: list[tuple[re.Pattern[str], bool]]) -> bool:
    """Docker's rule: **the last matching pattern wins**, and a pattern matching a
    parent directory excludes everything beneath it.

    Both halves are load-bearing for this repository's own file, which excludes
    `openapi/` and then re-includes `!openapi/openapi.yaml`. Drop either and the
    contract is missing from the context, or the exclusion does nothing.
    """
    parts = path.split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    excluded = False
    for regex, negated in rules:
        if any(regex.match(candidate) for candidate in candidates):
            excluded = not negated
    return excluded


def context_sources(text: str) -> list[str]:
    """Every path a COPY or ADD reads FROM THE BUILD CONTEXT.

    `COPY --from=deps` and `COPY --from=web` read another stage, not the context,
    and `.dockerignore` has nothing to say about them.
    """
    sources = []
    for line in text.splitlines():
        match = re.match(r"\s*(COPY|ADD)\s+(.*)", line, re.IGNORECASE)
        if not match:
            continue
        words = match.group(2).split()
        if any(word.startswith("--from=") for word in words):
            continue
        words = [word for word in words if not word.startswith("--")]
        sources.extend(words[:-1])          # the last word is the destination
    return sources


def stage_text(text: str, stage: str) -> str:
    """The instructions of one named stage — from its FROM to the next.

    A COPY in `dev` says nothing about what `runtime` ships; the two stages
    start from the same base and copy different things, which is the whole
    reason the fourth defect above was invisible from the dev stack.
    """
    for index, match in enumerate(_FROM.finditer(text)):
        if match.group(2) != stage:
            continue
        rest = list(_FROM.finditer(text))[index + 1 :]
        end = rest[0].start() if rest else len(text)
        return text[match.end() : end]
    return ""


def covers(sources: list[str], path: str) -> bool:
    """Does some COPY source bring `path` in — the file itself, or a directory
    above it? `COPY app /app/app` covers `app/api/main.py`; nothing covers
    `scripts/bootstrap.py` unless a COPY names it or `scripts`."""
    parts = path.split("/")
    ancestors = {"/".join(parts[: n + 1]) for n in range(len(parts))}
    return any(source.rstrip("/") in ancestors for source in sources)


def check_context(dockerfile: Path, text: str) -> list[str]:
    if not DOCKERIGNORE.is_file():
        return []
    rules = ignore_rules(DOCKERIGNORE)
    problems = []
    for source in context_sources(text):
        path = source.rstrip("/")
        if "*" in path or "?" in path:
            continue                        # a glob: what it matches is not knowable here
        if not (ROOT / path).exists():
            problems.append(f"{dockerfile.name}: `COPY {source}` — no such path in the repository")
        elif is_excluded(path, rules):
            problems.append(
                f'{dockerfile.name}: `COPY {source}` reads a path .dockerignore excludes. '
                f'The build fails with `"/{path}": not found`, which blames the file.'
            )
    return problems


# ── the compose files ────────────────────────────────────────────────────────

def services(compose: Path) -> dict[str, dict]:
    """The services in a compose file, with `<<:` merges already applied.

    `yaml.safe_load` resolves merge keys itself, which is what makes this
    readable: `x-app: &app` carries the build block and four services inherit it
    through `<<: *app`, so the anchor is where a missing target hides — one
    place, four failures, and the bug is in none of the services that show it.
    """
    document = yaml.safe_load(compose.read_text()) or {}
    return document.get("services", {}) or {}


def check_targets(compose: Path) -> list[str]:
    problems: list[str] = []

    for name, service in sorted(services(compose).items()):
        build = service.get("build")
        if build is None:
            continue                        # a registry image; nothing to target
        if isinstance(build, str):
            build = {"context": build}      # `build: .` is legal shorthand

        dockerfile = ROOT / build.get("dockerfile", "Dockerfile")
        if not dockerfile.is_file():
            problems.append(f"{compose.name}: {name} builds {dockerfile}, which does not exist")
            continue

        declared = stages(instructions(dockerfile))
        if len(declared) < 2:
            continue                        # single-stage: the default is the only one

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


#: `scripts/<name>.py` wherever it appears in a service's command. The scripts
#: directory is the one this repository excludes from the build context, so it
#: is the one a command can name and the image can lack.
_SCRIPT_TOKEN = re.compile(r"\bscripts/[A-Za-z0-9_.-]+\.py\b")


def command_words(command: object) -> str:
    """A compose `command:` as one string, whichever of its two forms it takes.

    `["sh", "-c", "alembic upgrade head && python scripts/bootstrap.py"]` is the
    form that hides the script two levels down — a list, whose third element is
    a shell line. Joining is enough; the check wants tokens, not structure.
    """
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        return " ".join(str(word) for word in command)
    return ""


def mounts_working_tree(service: dict) -> bool:
    """Does the service bind-mount the repository over the container?

    `docker-compose.dev.yml` mounts `.:/app`, so the dev stack has every script
    without copying one — which is exactly why it never showed the defect this
    check exists for. A mounted tree needs no COPY, and asking for one would
    fail the dev stack for shipping nothing, which is its design.
    """
    for volume in service.get("volumes") or []:
        source = volume.get("source") if isinstance(volume, dict) else str(volume).split(":")[0]
        if source in (".", "./"):
            return True
    return False


def check_commands(compose: Path) -> list[str]:
    """Every script a service's `command` names must be in the stage it runs.

    The three checks above prove the build definition can build. This one proves
    the image can run the command compose gives it — the narrow version: a
    `scripts/*.py` token in `command:` must be a COPY source of the service's
    target stage, and that source must survive `.dockerignore`. Narrow on
    purpose. Whether `alembic` is on PATH or `python` is the venv's is the
    image's business; which files are in it is the build definition's, and the
    build definition is what this script reads.
    """
    problems: list[str] = []
    rules = ignore_rules(DOCKERIGNORE) if DOCKERIGNORE.is_file() else []

    for name, service in sorted(services(compose).items()):
        build = service.get("build")
        if build is None or mounts_working_tree(service):
            continue
        if isinstance(build, str):
            build = {"context": build}
        dockerfile = ROOT / build.get("dockerfile", "Dockerfile")
        target = build.get("target")
        if not dockerfile.is_file() or target is None:
            continue                        # check_targets has already reported it

        scripts = sorted(set(_SCRIPT_TOKEN.findall(command_words(service.get("command")))))
        if not scripts:
            continue
        sources = context_sources(stage_text(instructions(dockerfile), target))
        for script in scripts:
            if not covers(sources, script):
                problems.append(
                    f"{compose.name}: service `{name}` runs `{script}`, and stage "
                    f"`{target}` of {dockerfile.name} never COPYs it. The image builds; "
                    f"the container exits 2 with \"can't open file\" and everything "
                    f"that depends on it waits forever."
                )
            elif is_excluded(script, rules):
                problems.append(
                    f"{compose.name}: service `{name}` runs `{script}`, which "
                    f".dockerignore excludes — the COPY in stage `{target}` reads a path "
                    f"the context does not carry."
                )
    return problems


def main() -> int:
    dockerfiles = sorted(ROOT.glob("Dockerfile*"))
    composes = sorted(ROOT.glob("docker-compose*.yml"))
    if not dockerfiles or not composes:
        # The selector found nothing, which is a broken gate rather than a clean
        # one. Every check in this directory carries the same guard and for the
        # same reason: a rename upstream must fail loudly here.
        print("FAIL  no Dockerfile or no docker-compose*.yml — has the layout changed?")
        return 1

    problems: list[str] = []
    for dockerfile in dockerfiles:
        text = instructions(dockerfile)
        problems += check_from_args(dockerfile, text)
        problems += check_context(dockerfile, text)
    for compose in composes:
        problems += check_targets(compose)
        problems += check_commands(compose)

    for problem in problems:
        print(f"FAIL  {problem}")
    if problems:
        return 1

    print(
        f"PASS  {len(dockerfiles)} Dockerfile, {len(composes)} compose files: every FROM "
        f"resolves, every COPY is in the context, every built service names a stage, "
        f"every script a command runs is in its stage"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
