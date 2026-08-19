#!/usr/bin/env python3
"""The first platform admin — and, optionally, their own centre — from .env
values instead of by hand.

Companion to `dev_seed.py`, not a replacement for it: that one is a
development fixture (hardcoded name, hardcoded date of birth, gated to
`ENVIRONMENT=development`). This one is meant to run in production, driven
entirely by `BOOTSTRAP_*` variables, and left wired into the `migrate`
one-shot service so it is safe to leave configured indefinitely — see
"Safety" below for why a stale `.env` cannot re-fire it.

    python scripts/bootstrap.py

## Why this exists

There is no endpoint that creates a user or grants `platform_admin` —
deliberately, per `dev_seed.py`'s own docstring: an API that could mint a
platform admin is a far larger blast radius than a step performed once, out
of band. That leaves every real deployment needing the same manual `psql`
dance documented in `docs/deploy/development.md`, hand-edited with real
values each time. This is that dance, made repeatable and driven by config
instead of copy-pasted SQL.

## Safety

- No-op, exit 0, if `BOOTSTRAP_ADMIN_PHONE` is unset or empty. Safe to leave
  the whole block, and this script's invocation, in `.env` forever.
- No-op, exit 0, if an unrevoked platform admin already exists — checked
  before touching anything else, so a stale `.env` on a later redeploy
  cannot re-grant or duplicate.
- One transaction: either everything below commits, or none of it does.
- The organization is optional. Set the admin fields alone for a
  platform-admin-only account with no centre attached, same shape
  `dev_seed.py` produces today. Set `BOOTSTRAP_ORG_NAME` (and
  `BOOTSTRAP_ORG_SLUG`) too, and the same account also becomes that centre's
  own `centre_admin` — direct membership, not an invite. The invite→redeem
  round trip exists for real invitations between two different people; an
  operator bootstrapping their own first centre is not inviting themselves.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys

from sqlalchemy import create_engine, text

# Same pattern `app/modules/identity/models.py` validates every phone
# number against — kept in sync by hand since this script runs before the
# application package is necessarily importable (a bare venv, or a
# throwaway container stage with only this file and its dependencies).
PHONE_PATTERN = re.compile(r"^\+998[0-9]{9}$")
SLUG_PATTERN = re.compile(r"^[a-z0-9-]{3,40}$")
ORG_KINDS = {"prep_centre", "school", "university", "internal"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def bootstrap() -> int:
    phone = _env("BOOTSTRAP_ADMIN_PHONE")
    if not phone:
        print("    BOOTSTRAP_ADMIN_PHONE not set; nothing to bootstrap")
        return 0
    if not PHONE_PATTERN.match(phone):
        print(f"    BOOTSTRAP_ADMIN_PHONE {phone!r} does not match "
              f"{PHONE_PATTERN.pattern}")
        return 1

    given_name = _env("BOOTSTRAP_ADMIN_NAME")
    if not given_name:
        print("    BOOTSTRAP_ADMIN_NAME is required alongside BOOTSTRAP_ADMIN_PHONE")
        return 1
    family_name = _env("BOOTSTRAP_ADMIN_FAMILY_NAME") or None

    dob_raw = _env("BOOTSTRAP_ADMIN_DOB")
    if not dob_raw:
        print("    BOOTSTRAP_ADMIN_DOB is required alongside BOOTSTRAP_ADMIN_PHONE "
              "(YYYY-MM-DD)")
        return 1
    try:
        dob = dt.date.fromisoformat(dob_raw)
    except ValueError:
        print(f"    BOOTSTRAP_ADMIN_DOB {dob_raw!r} is not a YYYY-MM-DD date")
        return 1
    if dob >= dt.date.today():
        print(f"    BOOTSTRAP_ADMIN_DOB {dob_raw!r} is not in the past")
        return 1

    org_name = _env("BOOTSTRAP_ORG_NAME")
    org_slug = _env("BOOTSTRAP_ORG_SLUG")
    org_kind = _env("BOOTSTRAP_ORG_KIND", "prep_centre")
    org_contact_phone = _env("BOOTSTRAP_ORG_CONTACT_PHONE") or None
    if org_name and not org_slug:
        print("    BOOTSTRAP_ORG_SLUG is required alongside BOOTSTRAP_ORG_NAME")
        return 1
    if org_slug and not org_name:
        print("    BOOTSTRAP_ORG_NAME is required alongside BOOTSTRAP_ORG_SLUG")
        return 1
    if org_slug and not SLUG_PATTERN.match(org_slug):
        print(f"    BOOTSTRAP_ORG_SLUG {org_slug!r} does not match "
              f"{SLUG_PATTERN.pattern} (lowercase letters, digits, hyphens, 3-40 chars)")
        return 1
    if org_kind not in ORG_KINDS:
        print(f"    BOOTSTRAP_ORG_KIND {org_kind!r} must be one of {sorted(ORG_KINDS)}")
        return 1

    with create_engine(os.environ["DATABASE_URL"]).begin() as connection:
        # Unrevoked, not merely "any row ever": a platform admin later
        # revoked by hand should be re-bootstrappable rather than leaving
        # the operator locked out with no console left to fix it from.
        if connection.scalar(text(
            "SELECT count(*) FROM platform_role_grants WHERE revoked_at IS NULL"
        )):
            print("    a platform admin already exists; skipping bootstrap")
            return 0

        user_id = connection.scalar(text("""
            INSERT INTO users (phone, given_name, family_name, date_of_birth,
                               locale, status)
            VALUES (:phone, :given_name, :family_name, :dob, 'uz-Latn', 'active')
            RETURNING id
        """), {"phone": phone, "given_name": given_name,
               "family_name": family_name, "dob": dob})

        # `granted_by` is the new user itself — the same shape the manual
        # bootstrap in docs/deploy/development.md uses, because there is
        # nobody else to grant it yet.
        connection.execute(text("""
            INSERT INTO platform_role_grants (user_id, role, granted_by)
            VALUES (:u, 'platform_admin', :u)
        """), {"u": user_id})
        print(f"    created {phone} as a platform admin")

        if org_name:
            # `status='active'` explicitly: the column defaults to
            # 'pending', but `POST /orgs` never leaves it there (there is
            # no approval step in this product) and neither does this.
            org_id = connection.scalar(text("""
                INSERT INTO organizations (name, slug, kind, status,
                                           contact_phone, created_by)
                VALUES (:name, :slug, :kind, 'active', :contact_phone, :created_by)
                RETURNING id
            """), {"name": org_name, "slug": org_slug, "kind": org_kind,
                   "contact_phone": org_contact_phone, "created_by": user_id})
            connection.execute(text("""
                INSERT INTO org_memberships (org_id, user_id, role, status, invited_by)
                VALUES (:org, :user, 'centre_admin', 'active', :user)
            """), {"org": org_id, "user": user_id})
            print(f"    created {org_name!r} ({org_slug}) with {phone} as its centre_admin")

    return 0


if __name__ == "__main__":
    sys.exit(bootstrap())
