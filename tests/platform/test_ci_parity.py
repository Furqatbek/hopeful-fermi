"""`scripts/check_ci_parity.py` counts steps, not comments.

The parity gate exists because gates drifted out of the workflow while staying
in `make ci`. It matched `make <target>` over the raw YAML, and ci.yml explains
its steps in comments that name the same targets — so a deleted `run:` line
would have stayed green through its own explanation. These pin the matcher to
what its docstring always claimed: a target counts when a step runs it.

No subprocess and no services: the module is loaded from its path, because
`scripts/` is not a package and this tier runs with an empty PATH.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_ci_parity.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_ci_parity", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_comment_naming_a_target_is_not_a_step():
    # The exact shape from ci.yml: the explanation survives, the step is gone.
    workflow = "      - name: Test suite and coverage floors\n        # `make coverage` is `test-fast` plus the floors.\n"
    assert _load().invoked_targets(workflow) == set()


def test_a_run_line_counts():
    assert _load().invoked_targets("      - run: make coverage\n") == {"coverage"}


def test_a_bare_make_inside_a_block_scalar_counts():
    workflow = "      - run: |\n          set -euo pipefail\n          make coverage\n          make migrations\n"
    assert _load().invoked_targets(workflow) == {"coverage", "migrations"}


def test_a_trailing_comment_on_a_step_does_not_add_a_target():
    workflow = "      - run: make lint  # not `make coverage`, that is the tests job\n"
    assert _load().invoked_targets(workflow) == {"lint"}


def test_the_real_workflow_still_runs_every_gate():
    """The gate against the repository's own files, without a subprocess.

    Not a substitute for `make ci-parity` in CI — this is the same assertion,
    here so a change to the matcher fails next to its unit tests rather than
    two jobs away.
    """
    module = _load()
    makefile = (ROOT / "Makefile").read_text()
    invoked = module.invoked_targets((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    wanted = [t for a in module.AGGREGATES for t in module.prerequisites(a, makefile)]
    assert [t for t in wanted if t not in invoked and t not in module.EXEMPT] == []
