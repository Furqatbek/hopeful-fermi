"""The no-services tier, enforced rather than assumed.

`make test-unit` collects everything outside `tests/integration`, and CI runs it
in the **lint** job — a runner with no ffmpeg, no Postgres, no MinIO and no
Redis. That is the tier's whole value: 460 tests in 1.5 s that a contributor can
run on a laptop with nothing installed.

It was a convention, and a convention held until it did not. A test here asserted
`available() is True` — a fact about the machine — and passed on every developer
box and in the tests job, then failed in the lint job where ffmpeg is genuinely
absent. `make ci` on a laptop could not reproduce it, because the laptop has
ffmpeg, so the README's promise that a failing pipeline is one command away was
not true for this one job.

Emptying `PATH` for these tests makes the runner's environment the local
environment. A test that reaches for a binary now fails the same way everywhere,
at the moment it is written. Tests that stub `shutil.which` themselves are
unaffected, which is how both answers stay reachable.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_binaries(monkeypatch):
    monkeypatch.setenv("PATH", "")
