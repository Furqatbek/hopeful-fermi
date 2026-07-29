from __future__ import annotations

from pathlib import Path

import pytest

from app.modules.qtypes.registry import Registry, Scorer, load_lexicon

ROOT = Path(__file__).resolve().parents[1]


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
