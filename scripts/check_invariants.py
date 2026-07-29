#!/usr/bin/env python3
"""Gate the database-enforced invariants and the zero-DDL acceptance test.

`verify_invariants.sql` proves that the promises in
`docs/design/0002-data-model.md` §10 are enforced by PostgreSQL and not by
application code that a future handler can forget: the audit log is append-only,
a question version has exactly one current answer key, a published test version
is immutable, submitted answers are frozen, content defaults to org-private.

It was written to be read by a human — it runs with `ON_ERROR_STOP off` and a
correct run is *full of* errors, because each error IS an invariant firing. That
makes its exit code meaningless, which is why it never ran in CI. This wrapper
gives it a verdict: every expected invariant must fire, every reported value must
match, and nothing else may error.

Then it runs `acceptance_new_question_type.py` against the same database, which
is where the fixtures it needs come from.

    TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost/postgres \\
        python3 scripts/check_invariants.py
"""
from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]

# (label, needle, how many writes must be refused with that message).
#
# Matched as substrings, and deliberately not on the full message: the audit log
# is partitioned by month, so its error names `audit_log_2026_07` and a literal
# would start failing on the first of some future month for no reason at all.
#
# The count exists because UPDATE and DELETE on `audit_log` raise the SAME
# message. Listed as two entries they were indistinguishable, so disabling the
# trigger for UPDATE reported the DELETE row as broken — a true failure with a
# false cause, which is how a real regression gets chased in the wrong file.
MUST_REFUSE = (
    ("audit log is append-only (UPDATE and DELETE)", "is append-only", 2),
    ("one current answer key per question version", "answer_key_versions_current_uq", 1),
    ("unknown question type is rejected by the registry FK",
     "question_versions_type_key_type_version_fkey", 1),
    ("published test version cannot be edited", "test_version 1 is immutable", 1),
    ("published test version cannot return to draft", "cannot return to draft", 1),
    ("answers freeze at submit", "answers are frozen", 1),
    ("one current score run per attempt", "score_runs_current_uq", 1),
)
REFUSALS = sum(n for _, _, n in MUST_REFUSE)


def _expected_values() -> dict[str, str]:
    """The seven reported values.

    Check 1 renders `minor=<adult_at > current_date>`, so the expected text
    depends on today. Computed rather than pinned: a literal `minor=true` would
    pass for eighteen months and then fail on 2028-03-01 with no code change,
    which is the same shape of bug as a test that only fails after 22:00.
    """
    today = dt.date.today()
    aziza = "true" if dt.date(2028, 3, 1) > today else "false"
    bekzod = "true" if dt.date(2017, 6, 15) > today else "false"
    return {
        "1. generated adult_at":
            f"Aziza=2028-03-01/minor={aziza} Bekzod=2017-06-15/minor={bekzod}",
        "2. audit rows": "1",
        "3. key versions": "total=2 current=1",
        "4. published tv status": "archived",
        "5. frozen answer": '"A"',
        "6. score runs": "total=2 current_raw=31.00",
        "7. default visibility": "passages=org_private tests=org_private",
    }


def _psql_dsn(url) -> str:
    """psql speaks libpq URIs; SQLAlchemy DSNs carry a `+psycopg` driver tag and
    put a unix socket in the query string. Translate rather than assume TCP."""
    dsn = url.render_as_string(hide_password=False)
    return re.sub(r"^postgresql\+\w+://", "postgresql://", dsn)


def main() -> int:
    base = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not base:
        sys.exit("set TEST_DATABASE_URL (or DATABASE_URL)")

    parsed = make_url(base)
    name = f"ielts_inv_{uuid.uuid4().hex[:8]}"
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    scratch = parsed.set(database=name)
    env = {**os.environ, "DATABASE_URL": scratch.render_as_string(hide_password=False)}

    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        migrate = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                                 cwd=ROOT, env=env, capture_output=True, text=True)
        if migrate.returncode != 0:
            sys.stderr.write(migrate.stderr)
            return _fail("could not migrate the scratch database")

        run = subprocess.run(
            ["psql", _psql_dsn(scratch), "-f", "scripts/verify_invariants.sql"],
            cwd=ROOT, capture_output=True, text=True,
        )
        output = run.stdout + run.stderr
        problems = _judge(output)

        acceptance = subprocess.run(
            [sys.executable, "scripts/acceptance_new_question_type.py"],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        if acceptance.returncode != 0:
            sys.stdout.write(acceptance.stdout)
            sys.stderr.write(acceptance.stderr)
            problems.append("acceptance_new_question_type.py failed — adding a "
                            "question type now requires a migration")
        else:
            print("  acceptance   new question type added with zero DDL")
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()

    if problems:
        print()
        for p in problems:
            print(f"  FAIL  {p}")
        print(f"\nFAIL  {len(problems)} problem(s)\n\n--- psql output ---\n{output}")
        return 1
    print(f"PASS  {REFUSALS} writes refused by the database, "
          f"{len(_expected_values())} values as specified")
    return 0


def _judge(output: str) -> list[str]:
    problems: list[str] = []
    errors = [ln for ln in output.splitlines() if "ERROR:" in ln]

    unmatched = list(errors)
    for label, needle, wanted in MUST_REFUSE:
        hits = [ln for ln in unmatched if needle in ln]
        for line in hits:
            unmatched.remove(line)
        if len(hits) != wanted:
            problems.append(
                f"invariant NOT enforced: {label} — expected {wanted} refusal(s) "
                f"matching {needle!r}, saw {len(hits)}")
        else:
            print(f"  refused      {label}")

    # An error the script did not intend means the fixtures no longer apply
    # cleanly, and then every check above is measuring something else.
    for line in unmatched:
        problems.append(f"unexpected error: {line.strip()}")

    for label, expected in _expected_values().items():
        line = next((ln for ln in output.splitlines() if ln.startswith(label)), None)
        if line is None:
            problems.append(f"check did not report: {label}")
        elif line.split("-> ", 1)[-1].strip() != expected:
            problems.append(f"{label}: expected {expected!r}, "
                            f"got {line.split('-> ', 1)[-1].strip()!r}")
    return problems


def _fail(message: str) -> int:
    print(f"FAIL  {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
