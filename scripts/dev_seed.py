#!/usr/bin/env python3
"""The first account, and the check for whether the database is up.

Called by both `dev.sh` and `dev.ps1`, because it was briefly written twice —
once as a heredoc in each — and two copies of the program that grants platform
admin is precisely the shape this repository keeps finding and removing. The
bash and PowerShell wrappers differ in how they find a Python and start a
process; what they do to the database must not differ at all.

    python scripts/dev_seed.py ping     -> exit 0 when DATABASE_URL answers
    python scripts/dev_seed.py seed +998901234567

## Why a first account has to be created out of band

A fresh database has no users and you cannot register through the API:
registration goes through `POST /auth/telegram/verify` and needs a payload
signed with a real bot token. That is deliberate — the version that accepted a
bare phone number was a complete authentication bypass — but it leaves a laptop
with no way in at all.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text


def ping() -> int:
    try:
        create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True) \
            .connect().execute(text("select 1"))
    except Exception:
        return 1
    return 0


def seed(phone: str) -> int:
    """Create one account, as a platform admin. Idempotent, and guarded twice.

    ENVIRONMENT must be `development` AND the users table must be empty. Neither
    guard is redundant: the first keeps this off a staging box that happens to
    have the script checked out, the second keeps a re-run from quietly granting
    platform admin on a database that already has real people in it.
    """
    if os.environ.get("ENVIRONMENT", "development") != "development":
        print("    refusing to seed: ENVIRONMENT is not development")
        return 1

    with create_engine(os.environ["DATABASE_URL"]).begin() as connection:
        if connection.scalar(text("SELECT count(*) FROM users")):
            print(f"    an account already exists; signing in as {phone} still works")
            return 0
        user_id = connection.scalar(text("""
            INSERT INTO users (phone, given_name, date_of_birth, locale, status)
            VALUES (:p, 'Aziza', '2000-01-01', 'uz-Latn', 'active')
            RETURNING id
        """), {"p": phone})
        # `granted_by` is the new user itself. That is what a bootstrap looks
        # like, and it is the same shape as the production one: there is no
        # endpoint that mints a platform admin, deliberately, because an API
        # that can is a far larger blast radius than a step performed once.
        connection.execute(text("""
            INSERT INTO platform_role_grants (user_id, role, granted_by)
            VALUES (:u, 'platform_admin', :u)
        """), {"u": user_id})
        print(f"    created {phone} as a platform admin")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "ping":
        sys.exit(ping())
    if command == "seed":
        sys.exit(seed(sys.argv[2] if len(sys.argv) > 2 else "+998901234567"))
    sys.exit("usage: dev_seed.py ping | seed <phone>")
