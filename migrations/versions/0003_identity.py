"""identity: users, organizations, cohorts, auth, consent

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE users (
        id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid               uuid        NOT NULL DEFAULT gen_random_uuid(),
        -- E.164. The primary identity in this market; email is optional and secondary.
        phone             text        NOT NULL,
        phone_verified_at timestamptz,
        -- Primary onboarding channel (ADR-0001 section 8.6). Also the strongest
        -- duplicate-account signal we get: SIM cards are cheap, Telegram accounts are sticky.
        telegram_user_id  bigint,
        telegram_username text,
        email             citext,
        email_verified_at timestamptz,
        given_name        text        NOT NULL,
        family_name       text,
        -- Required, not optional: the 18 boundary drives the matching safety rule,
        -- and a nullable DOB would mean an unenforceable invariant.
        date_of_birth     date        NOT NULL,
        -- Generated so it can never drift from date_of_birth, and so age banding is
        -- an indexed comparison rather than per-row arithmetic in the matcher.
        adult_at          date        GENERATED ALWAYS AS
                                      (((date_of_birth + INTERVAL '18 years'))::date) STORED,
        locale            text        NOT NULL DEFAULT 'uz-Latn'
                                      CHECK (locale IN ('uz-Latn','uz-Cyrl','ru','en')),
        timezone          text        NOT NULL DEFAULT 'Asia/Tashkent',
        status            text        NOT NULL DEFAULT 'active'
                                      CHECK (status IN ('active','suspended','deleted')),
        target_band       numeric(2,1),
        last_seen_at      timestamptz,
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now(),
        deleted_at        timestamptz
    );
    CREATE UNIQUE INDEX users_xid_uq ON users (xid);
    -- Partial uniqueness: a deleted user must not block phone reuse forever.
    CREATE UNIQUE INDEX users_phone_uq ON users (phone) WHERE deleted_at IS NULL;
    CREATE UNIQUE INDEX users_telegram_uq ON users (telegram_user_id)
        WHERE telegram_user_id IS NOT NULL AND deleted_at IS NULL;
    CREATE UNIQUE INDEX users_email_uq ON users (email)
        WHERE email IS NOT NULL AND deleted_at IS NULL;
    -- The age-band filter in speaking matchmaking runs on every match cycle.
    CREATE INDEX users_adult_at_idx ON users (adult_at);
    CREATE TRIGGER users_updated_at BEFORE UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE TABLE organizations (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid           uuid        NOT NULL DEFAULT gen_random_uuid(),
        name          text        NOT NULL,
        slug          text        NOT NULL,
        legal_name    text,
        kind          text        NOT NULL DEFAULT 'prep_centre'
                                  CHECK (kind IN ('prep_centre','school','university','internal')),
        status        text        NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending','active','suspended','closed')),
        contact_phone text,
        contact_email citext,
        country       char(2)     NOT NULL DEFAULT 'UZ',
        timezone      text        NOT NULL DEFAULT 'Asia/Tashkent',
        settings      jsonb       NOT NULL DEFAULT '{}',
        created_by    bigint      REFERENCES users(id),
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX organizations_xid_uq ON organizations (xid);
    CREATE UNIQUE INDEX organizations_slug_uq ON organizations (lower(slug));
    CREATE TRIGGER organizations_updated_at BEFORE UPDATE ON organizations
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    -- Role is a property of MEMBERSHIP, not of the user. This is the whole answer to
    -- "unaffiliated individuals and org cohorts must coexist": a user with zero
    -- memberships is an individual student; the same user may hold a teacher
    -- membership in one org and a student membership in another.
    CREATE TABLE org_memberships (
        id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        org_id     bigint      NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        user_id    bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role       text        NOT NULL CHECK (role IN ('student','teacher','centre_admin')),
        status     text        NOT NULL DEFAULT 'active'
                               CHECK (status IN ('invited','active','suspended','left')),
        invited_by bigint      REFERENCES users(id),
        joined_at  timestamptz,
        left_at    timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX org_memberships_uq ON org_memberships (org_id, user_id);
    -- "Which orgs am I in" — resolved on every request to build the auth principal.
    CREATE INDEX org_memberships_user_idx ON org_memberships (user_id) WHERE status = 'active';
    -- "List this centre's teachers / students" — the centre-admin dashboard.
    CREATE INDEX org_memberships_org_role_idx ON org_memberships (org_id, role) WHERE status = 'active';
    CREATE TRIGGER org_memberships_updated_at BEFORE UPDATE ON org_memberships
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE TABLE cohorts (
        id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid           uuid        NOT NULL DEFAULT gen_random_uuid(),
        org_id        bigint      NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        name          text        NOT NULL,
        academic_year text,
        status        text        NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active','archived')),
        created_by    bigint      REFERENCES users(id),
        created_at    timestamptz NOT NULL DEFAULT now(),
        archived_at   timestamptz
    );
    CREATE UNIQUE INDEX cohorts_xid_uq ON cohorts (xid);
    CREATE INDEX cohorts_org_idx ON cohorts (org_id) WHERE status = 'active';

    CREATE TABLE cohort_members (
        id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        cohort_id bigint      NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
        user_id   bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        status    text        NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','left')),
        joined_at timestamptz NOT NULL DEFAULT now(),
        left_at   timestamptz
    );
    CREATE UNIQUE INDEX cohort_members_uq ON cohort_members (cohort_id, user_id);
    -- Analytics and assignment fan-out both walk this direction.
    CREATE INDEX cohort_members_user_idx ON cohort_members (user_id) WHERE status = 'active';

    -- Phone-based invite: a centre admin adds a student who has no account yet.
    CREATE TABLE org_invites (
        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid         uuid        NOT NULL DEFAULT gen_random_uuid(),
        org_id      bigint      NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        cohort_id   bigint      REFERENCES cohorts(id) ON DELETE SET NULL,
        role        text        NOT NULL CHECK (role IN ('student','teacher','centre_admin')),
        phone       text,
        token_hash  text        NOT NULL,
        created_by  bigint      NOT NULL REFERENCES users(id),
        created_at  timestamptz NOT NULL DEFAULT now(),
        expires_at  timestamptz NOT NULL,
        accepted_at timestamptz,
        accepted_by bigint      REFERENCES users(id),
        revoked_at  timestamptz
    );
    -- Redemption looks the invite up by its hashed token, never by id.
    CREATE UNIQUE INDEX org_invites_token_uq ON org_invites (token_hash);
    CREATE INDEX org_invites_org_idx ON org_invites (org_id, created_at DESC);
    CREATE INDEX org_invites_phone_idx ON org_invites (phone) WHERE accepted_at IS NULL;

    CREATE TABLE otp_challenges (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        phone        text        NOT NULL,
        purpose      text        NOT NULL
                                 CHECK (purpose IN ('login','verify_phone','change_phone','recover')),
        -- Hash only. The plaintext code is never persisted and never logged.
        code_hash    text        NOT NULL,
        channel      text        NOT NULL CHECK (channel IN ('sms','telegram','voice')),
        attempts     int         NOT NULL DEFAULT 0,
        max_attempts int         NOT NULL DEFAULT 5,
        request_ip   inet,
        expires_at   timestamptz NOT NULL,
        consumed_at  timestamptz,
        created_at   timestamptz NOT NULL DEFAULT now()
    );
    -- Per-phone rate limit window: "how many codes has this number asked for in 1h".
    CREATE INDEX otp_challenges_phone_idx ON otp_challenges (phone, created_at DESC);
    -- Per-IP abuse detection, which is what stops someone burning your SMS budget.
    CREATE INDEX otp_challenges_ip_idx ON otp_challenges (request_ip, created_at DESC);
    CREATE INDEX otp_challenges_gc_idx ON otp_challenges (expires_at) WHERE consumed_at IS NULL;

    -- Refresh tokens are opaque and stored, not JWTs: a safety ban must be able to
    -- kill a live session immediately (ADR-0001 section 5.2).
    CREATE TABLE auth_sessions (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid             uuid        NOT NULL DEFAULT gen_random_uuid(),
        user_id         bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash      text        NOT NULL,
        device_label    text,
        user_agent_hash text,
        ip_first        inet,
        ip_last         inet,
        issued_at       timestamptz NOT NULL DEFAULT now(),
        last_used_at    timestamptz,
        expires_at      timestamptz NOT NULL,
        revoked_at      timestamptz,
        revoked_reason  text,
        rotated_from_id bigint      REFERENCES auth_sessions(id)
    );
    CREATE UNIQUE INDEX auth_sessions_token_uq ON auth_sessions (token_hash);
    -- "Log out everywhere" and ban enforcement both scan a user's live sessions.
    CREATE INDEX auth_sessions_user_idx ON auth_sessions (user_id) WHERE revoked_at IS NULL;
    CREATE INDEX auth_sessions_gc_idx ON auth_sessions (expires_at);

    -- Consent is evidence, not a boolean. Who consented, when, through what channel,
    -- and against which version of the text (ADR-0001 section 9.2).
    CREATE TABLE consents (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id         bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind            text        NOT NULL
                                    CHECK (kind IN ('terms','privacy','parental','stranger_matching','marketing')),
        doc_version     text        NOT NULL,
        doc_hash        text        NOT NULL,
        granted_by_kind text        NOT NULL CHECK (granted_by_kind IN ('self','parent','centre_admin')),
        parent_name     text,
        parent_phone    text,
        channel         text        NOT NULL CHECK (channel IN ('web','telegram','sms','paper')),
        evidence        jsonb       NOT NULL DEFAULT '{}',
        granted_at      timestamptz NOT NULL DEFAULT now(),
        revoked_at      timestamptz
    );
    -- "Does this minor currently hold stranger-matching consent" — checked before every match.
    CREATE INDEX consents_user_kind_idx ON consents (user_id, kind, granted_at DESC);

    -- Device identity. Feeds duplicate-account detection in competitions and
    -- "new device" security notices.
    CREATE TABLE user_devices (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id          bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        fingerprint_hash text        NOT NULL,
        platform         text,
        label            text,
        ip_last          inet,
        trusted          boolean     NOT NULL DEFAULT false,
        first_seen_at    timestamptz NOT NULL DEFAULT now(),
        last_seen_at     timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX user_devices_uq ON user_devices (user_id, fingerprint_hash);
    -- The anti-cheat query: one fingerprint, many user_ids = probable duplicate accounts.
    CREATE INDEX user_devices_fp_idx ON user_devices (fingerprint_hash);
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS user_devices, consents, auth_sessions, otp_challenges,
        org_invites, cohort_members, cohorts, org_memberships, organizations, users CASCADE;
    """)
