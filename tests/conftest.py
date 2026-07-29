"""Shared fixtures, and the rule that CI may not skip.

A skip is the right answer on a laptop with no PostgreSQL and no ffmpeg — the
pure domain suites still run and still mean something. It is the wrong answer in
CI, where a missing dependency is a broken runner and a green tick over an
unexecuted suite is the most expensive kind of false report.

That is not hypothetical here. `pytest -n 4` once printed **"355 passed, 400
skipped"** and exited 0, because four workers raced to build the template
database and a `try/except: pytest.skip(...)` swallowed the collision
(`docs/design/0010-test-suite-speed.md` §3.2). More than half the suite never
ran. The exit code said everything was fine.

So: when `CI` is set, a skip is a failure. The switch is `CI` rather than a flag
of our own because every CI provider sets it and nothing else does, which means
the gate cannot be forgotten when a workflow file is edited. `ALLOW_SKIPS=1`
overrides it for the rare container that sets `CI` and genuinely wants a partial
run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.modules.qtypes.registry import Registry, Scorer, load_lexicon

ROOT = Path(__file__).resolve().parents[1]


def strict_environment() -> bool:
    """True when a missing dependency must fail rather than skip."""
    return bool(os.environ.get("CI")) and not os.environ.get("ALLOW_SKIPS")


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):
    """Turn every skip into a failure under `CI`.

    Deliberately blunt: it does not enumerate the skips it knows about (the
    database and ffmpeg), it forbids the category. A skip added next year for a
    reason nobody remembers fails CI until somebody states why, which is the
    conversation worth having.

    `xfail` is untouched — an expected failure is a recorded fact, not an
    unexecuted test.
    """
    report = yield
    if not (report.skipped and strict_environment()):
        return report
    if hasattr(report, "wasxfail"):
        return report

    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
    report.outcome = "failed"
    report.longrepr = (
        f"SKIPPED IN CI: {reason}\n\n"
        "CI must exercise the whole suite; a skipped test here means the runner "
        "is missing a dependency it is supposed to provide (PostgreSQL, ffmpeg), "
        "not that the test is optional. Install it, or set ALLOW_SKIPS=1 if this "
        "run is deliberately partial."
    )
    return report


@pytest.fixture(scope="session")
def registry() -> Registry:
    """The real shipped definitions, not fixtures. A test suite that passes
    against hand-written stubs and fails against `registry/` is worthless."""
    return Registry.from_directory(ROOT / "registry" / "question_types")


@pytest.fixture(scope="session")
def lexicon():
    return load_lexicon(ROOT / "registry" / "lexicon")


@pytest.fixture(scope="session")
def scorer(registry, lexicon) -> Scorer:
    return Scorer(registry, lexicon)
