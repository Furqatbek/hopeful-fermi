"""One rule, two languages, and the test that stops them drifting.

`has_response` decides what "the student put something here" means in Python;
`answered_sql` is the same decision as a predicate PostgreSQL evaluates. The
invigilation screen needs the SQL one because it counts across a cohort in a
lateral join; the scorer needs the Python one because it marks in process.

Nothing forces them to agree, and `docs/known-issues.md` has the same shape
recorded eleven times over: two places that have to match with nobody looking at
the relationship. The original defect here was exactly that — the screen counted
`response IS NOT NULL`, the marking counted the scorer's rule, and a student who
cleared a box appeared on a teacher's screen as having answered it.

This runs the SQL against a real PostgreSQL, because the whole point is what the
database does with `'""'::jsonb` and `jsonb_typeof`, and a mock would be a second
copy of the assumption under test.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.modules.qtypes.primitives import answered_sql, has_response

# Every shape a stored response actually takes, plus the ones that broke it.
CORPUS = [
    None,                       # never touched
    "",                         # typed and cleared — the original defect
    "   ",                      # selected and deleted, which leaves whitespace
    "\n",                       # what a textarea leaves behind
    "\t",
    "\r\n  ",
    "bicycle",
    " bicycle ",
    "0",                        # a real answer to "how many years?"
    0,
    False,                      # jsonb `false`; an answer, not an absence
    True,
    [],                         # a multi-select with nothing ticked
    ["A"],
    ["A", "C"],
    {},
    {"s1": "x"},
]


@pytest.mark.parametrize("value", CORPUS, ids=lambda v: repr(v)[:20])
def test_the_sql_and_the_python_agree(db, value):
    sql = answered_sql("v")
    row = db.execute(
        text(f"SELECT ({sql}) AS answered FROM (SELECT CAST(:v AS jsonb) AS v) t"),
        {"v": json.dumps(value)},
    ).scalar_one()
    assert row is has_response(value), (
        f"{value!r}: SQL says {row}, Python says {has_response(value)}")


def test_json_null_is_not_an_answer(db):
    """Distinct from SQL NULL and easy to lose: a client that sends `null` for a
    cleared slot stores `'null'::jsonb`, which is NOT NULL to the database."""
    sql = answered_sql("v")
    row = db.execute(
        text(f"SELECT ({sql}) AS answered FROM (SELECT 'null'::jsonb AS v) t")
    ).scalar_one()
    assert row is False
