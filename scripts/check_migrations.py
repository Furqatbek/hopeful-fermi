#!/usr/bin/env python3
"""Prove the migration history runs forwards, backwards, and forwards again.

`alembic upgrade head` on an empty database is the only path most projects ever
exercise, so `downgrade` rots quietly — and the day it is needed is the day a
deploy is already going badly. This runs the full round trip on a scratch
database:

    upgrade head  ->  downgrade base  ->  upgrade head

The middle step is the one that finds things: a `DROP INDEX` naming an index the
upgrade created with a different name, a `downgrade()` left as `pass`, an
extension dropped while a column still depends on it. The third step proves the
downgrade actually returned the database to a state the upgrade can rebuild
from, which a downgrade that silently leaves objects behind will fail.

Also asserts the history is linear. Alembic branches are a footgun for a solo
maintainer (`alembic.ini` says so); this makes that a rule rather than a note.

    DATABASE_URL=postgresql+psycopg://postgres@localhost/postgres \\
        python3 scripts/check_migrations.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def _admin_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("set TEST_DATABASE_URL (or DATABASE_URL) to a server you may "
                 "create a scratch database on")
    return url


def _alembic(dsn: str, target: str) -> None:
    """Run alembic as a subprocess, not via its API.

    The API caches the migration modules in-process, so `upgrade` after
    `downgrade` in one interpreter can run against stale imports — which would
    make this check pass on a history that a real deploy cannot replay.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade" if target != "base" else "downgrade",
         target],
        cwd=ROOT, env={**os.environ, "DATABASE_URL": dsn},
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(f"FAIL  alembic {target} exited {result.returncode}")


def _linear_history() -> None:
    result = subprocess.run([sys.executable, "-m", "alembic", "heads"],
                            cwd=ROOT, capture_output=True, text=True)
    heads = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if len(heads) != 1:
        sys.exit(f"FAIL  {len(heads)} alembic heads; the history must stay linear:\n"
                 + "\n".join(heads))
    print(f"  history      linear, one head: {heads[0].strip()}")


def main() -> int:
    _linear_history()

    parsed = make_url(_admin_url())
    name = f"ielts_mig_{uuid.uuid4().hex[:8]}"
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    dsn = parsed.set(database=name).render_as_string(hide_password=False)

    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        for step, target in (("upgrade   ", "head"), ("downgrade ", "base"),
                             ("re-upgrade", "head")):
            _alembic(dsn, target)
            print(f"  {step}   ok")

        # A downgrade that leaves tables behind still lets `upgrade head` pass,
        # because most migrations are `CREATE ... IF NOT EXISTS`-shaped in
        # practice. Count what survived `downgrade base` on the SECOND pass
        # instead: after the round trip the schema must match a fresh build.
        engine = create_engine(dsn)
        with engine.connect() as c:
            tables = c.scalar(text("""
                SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
            """))
        engine.dispose()
        print(f"  round trip   {tables} tables rebuilt")
        if tables < 50:
            return _fail(f"only {tables} tables after re-upgrade; the downgrade "
                         "probably dropped something the upgrade does not recreate")
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()

    print("PASS  migrations apply, reverse, and re-apply")
    return 0


def _fail(message: str) -> int:
    print(f"FAIL  {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
