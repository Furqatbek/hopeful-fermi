"""`scripts/check_write_paths.py` reads the statement form of a write.

The gate failed the build over `attempt_sections.completed_at`, a column with
exactly one writer — `update(AttemptSection).where(...).values(completed_at=
now)` — because its resolver read attribute assignments, constructor keywords
and `setattr` and nothing else. CI pins the keyword form now, by running the
gate over `app/`; the other shapes `Resolver._values` documents occur nowhere in
`app/` today, so a regression in any of them would ship green. These pin each
one on a synthetic snippet, and the two answers that matter most: a joined
update writes only the model it is FOR, and a payload the AST cannot read makes
the gate say "I cannot tell" rather than "not written".

No subprocess and no services: the module is loaded from its path, because
`scripts/` is not a package and this tier runs with an empty PATH.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_write_paths.py"

CLASSES = {"AttemptSection", "AuthSession", "Cohort", "CohortMember", "ScoreRun"}


def _load():
    spec = importlib.util.spec_from_file_location("check_write_paths", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve(source: str):
    return _load().Resolver(ast.parse(source), CLASSES, {}, "x.py")


def test_update_values_keywords_write_the_updated_model():
    # The shape that produced the false alarm.
    r = _resolve("update(AttemptSection).where(AttemptSection.attempt_id == a)"
                 ".values(completed_at=now)")
    assert dict(r.assigned) == {"AttemptSection": {"completed_at"}}
    assert "completed_at" in r.opaque_attrs["AttemptSection"]
    assert dict(r.unresolved) == {}


def test_the_dict_form_records_names_and_string_literals():
    r = _resolve("update(AttemptSection).values({'status': 'x', 'completed_at': now})")
    assert dict(r.assigned) == {"AttemptSection": {"status", "completed_at"}}
    assert r.values["AttemptSection"]["status"] == {"x"}
    assert r.opaque_attrs["AttemptSection"] == {"completed_at"}


def test_a_splat_or_a_computed_key_marks_the_model_dynamic():
    for source in ("update(AttemptSection).values(**payload)",
                   "update(AttemptSection).values({key: value})"):
        r = _resolve(source)
        assert r.dynamic == {"AttemptSection"}, source
        assert dict(r.assigned) == {}, source


def test_a_positional_payload_is_unreadable_not_unwritten():
    # `insert(X).values([{...}, ...])` is SQLAlchemy's executemany; a variable is
    # the same thing one step removed. Neither can be read, and neither may be
    # skipped — skipping is what turns "I cannot tell" into "nothing writes it".
    for source in ("insert(ScoreRun).values([{'reason': 'a'}, {'reason': 'b'}])",
                   "update(ScoreRun).values(payload)"):
        r = _resolve(source)
        assert r.dynamic == {"ScoreRun"}, source
        assert dict(r.assigned) == {}, source


def test_table_update_writes_the_class_in_front_of_it():
    r = _resolve("AuthSession.__table__.update().where(AuthSession.user_id == u)"
                 ".values(revoked_at=now)")
    assert dict(r.assigned) == {"AuthSession": {"revoked_at"}}


def test_a_joined_update_writes_only_the_model_it_is_for():
    r = _resolve("update(CohortMember)"
                 ".where(Cohort.id == CohortMember.cohort_id, Cohort.org_id == org)"
                 ".values(left_at=now)")
    assert dict(r.assigned) == {"CohortMember": {"left_at"}}


def test_a_statement_with_no_model_is_recorded_unresolved():
    r = _resolve("stmt.values(completed_at=now)\nstmt.values(payload)")
    assert dict(r.assigned) == {}
    assert r.dynamic == set()
    assert dict(r.unresolved) == {"completed_at": {"x.py:1"}, "**": {"x.py:2"}}


def test_a_bare_values_call_is_a_dicts_and_records_nothing():
    r = _resolve("for value in payload.values():\n    total += value")
    assert dict(r.assigned) == {}
    assert r.dynamic == set()
    assert dict(r.unresolved) == {}
