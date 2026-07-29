"""speaking: scheduled slots (primary), live queue (secondary), pairs

Revision ID: 0013
Revises: 0012

Slot-first, per ADR-0001 section 8.1: at 150 DAU a live queue has roughly one
interested user per hour, so batch matching at slot open is the mechanism that
actually works. The live queue is the same matching code over a stream instead of
a batch, and ships as the secondary path.
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE speaking_slots (
        id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                      uuid        NOT NULL DEFAULT gen_random_uuid(),
        org_id                   bigint      REFERENCES organizations(id),
        starts_at                timestamptz NOT NULL,
        duration_minutes         int         NOT NULL DEFAULT 15,
        capacity                 int         NOT NULL DEFAULT 20,
        status                   text        NOT NULL DEFAULT 'scheduled'
                                             CHECK (status IN ('scheduled','booking','matching','live','closed','cancelled')),
        audience                 text        NOT NULL DEFAULT 'public'
                                             CHECK (audience IN ('public','org','cohort')),
        cohort_id                bigint      REFERENCES cohorts(id),
        band_min                 numeric(2,1),
        band_max                 numeric(2,1),
        language                 text        NOT NULL DEFAULT 'en',
        -- Age banding is a property of the SLOT, so a minor can never end up in an
        -- adult pool by accident. 'mixed_supervised' exists only for teacher-run
        -- cohort slots where an adult teacher is present by design.
        age_band                 text        NOT NULL DEFAULT 'adult'
                                             CHECK (age_band IN ('minor','adult','mixed_supervised')),
        cue_card_set_version_id  bigint      REFERENCES cue_card_set_versions(id),
        created_by               bigint      NOT NULL REFERENCES users(id),
        created_at               timestamptz NOT NULL DEFAULT now(),
        updated_at               timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX speaking_slots_xid_uq ON speaking_slots (xid);
    -- The student's "book a session" list and the scheduler's state tick.
    CREATE INDEX speaking_slots_upcoming_idx ON speaking_slots (starts_at)
        WHERE status IN ('scheduled','booking');
    CREATE INDEX speaking_slots_org_idx ON speaking_slots (org_id, starts_at DESC);
    CREATE TRIGGER speaking_slots_updated_at BEFORE UPDATE ON speaking_slots
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    CREATE TABLE speaking_slot_bookings (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        slot_id      bigint      NOT NULL REFERENCES speaking_slots(id) ON DELETE CASCADE,
        user_id      bigint      NOT NULL REFERENCES users(id),
        booked_at    timestamptz NOT NULL DEFAULT now(),
        cancelled_at timestamptz,
        -- Check-in is what feeds the batch matcher: only present users get paired.
        checked_in_at timestamptz,
        no_show      boolean     NOT NULL DEFAULT false,
        self_band    numeric(2,1),
        pair_id      bigint,
        UNIQUE (slot_id, user_id)
    );
    -- The matcher's input set at slot open.
    CREATE INDEX ssb_slot_idx ON speaking_slot_bookings (slot_id) WHERE cancelled_at IS NULL;
    -- The student's upcoming bookings, and the reminder job's source.
    CREATE INDEX ssb_user_idx ON speaking_slot_bookings (user_id, booked_at DESC);

    CREATE TABLE speaking_pairs (
        id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid                     uuid        NOT NULL DEFAULT gen_random_uuid(),
        slot_id                 bigint      REFERENCES speaking_slots(id),
        origin                  text        NOT NULL
                                            CHECK (origin IN ('slot_batch','live_queue','scheduled_direct')),
        user_a_id               bigint      NOT NULL REFERENCES users(id),
        user_b_id               bigint      NOT NULL REFERENCES users(id),
        -- Denormalized onto the pair itself: the safety invariant must be auditable
        -- from the pair record alone, without re-deriving both users' ages later.
        age_band                text        NOT NULL CHECK (age_band IN ('minor','adult','mixed_supervised')),
        cue_card_set_version_id bigint      REFERENCES cue_card_set_versions(id),
        matched_at              timestamptz NOT NULL DEFAULT now(),
        started_at              timestamptz,
        ended_at                timestamptz,
        end_reason              text,
        -- Whether TURN relay was used. Aggregated, this is your actual relay-rate
        -- number, which is what the TURN bandwidth estimate depends on.
        turn_relayed            boolean,
        connection_quality      jsonb,
        -- Populated ONLY when a report is filed: the reporter's client uploads its
        -- rolling 60-second local buffer. Audio is never routinely stored
        -- (ADR-0001 section 7).
        evidence_media_id       bigint      REFERENCES media_assets(id),
        CHECK (user_a_id <> user_b_id)
    );
    CREATE UNIQUE INDEX speaking_pairs_xid_uq ON speaking_pairs (xid);
    -- "Who has this user spoken with" — needed by both the anti-repeat rule and any
    -- safety investigation. Two indexes because the pair is unordered.
    CREATE INDEX speaking_pairs_user_a_idx ON speaking_pairs (user_a_id, matched_at DESC);
    CREATE INDEX speaking_pairs_user_b_idx ON speaking_pairs (user_b_id, matched_at DESC);
    CREATE INDEX speaking_pairs_slot_idx ON speaking_pairs (slot_id);

    ALTER TABLE speaking_slot_bookings ADD CONSTRAINT ssb_pair_fk
        FOREIGN KEY (pair_id) REFERENCES speaking_pairs(id);

    -- The live "try now" queue. Presence itself lives in Redis (ephemeral); this is
    -- the durable record of who waited, how long, and whether they were matched --
    -- which is the data that tells you when the live queue becomes viable.
    CREATE TABLE speaking_queue_entries (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id         bigint      NOT NULL REFERENCES users(id),
        joined_at       timestamptz NOT NULL DEFAULT now(),
        left_at         timestamptz,
        matched_pair_id bigint      REFERENCES speaking_pairs(id),
        band_min        numeric(2,1),
        band_max        numeric(2,1),
        language        text        NOT NULL DEFAULT 'en',
        age_band        text        NOT NULL CHECK (age_band IN ('minor','adult')),
        org_only        boolean     NOT NULL DEFAULT false,
        org_id          bigint      REFERENCES organizations(id),
        status          text        NOT NULL DEFAULT 'waiting'
                                    CHECK (status IN ('waiting','matched','expired','cancelled'))
    );
    -- Literally the matching query: same age band, same language, oldest first.
    -- age_band leads the index so a cross-band match is not merely forbidden in code,
    -- it is not even reachable by the query that finds candidates.
    CREATE INDEX sqe_waiting_idx ON speaking_queue_entries (age_band, language, joined_at)
        WHERE status = 'waiting';
    CREATE INDEX sqe_user_idx ON speaking_queue_entries (user_id, joined_at DESC);
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS speaking_queue_entries CASCADE;
    ALTER TABLE IF EXISTS speaking_slot_bookings DROP CONSTRAINT IF EXISTS ssb_pair_fk;
    DROP TABLE IF EXISTS speaking_pairs, speaking_slot_bookings, speaking_slots CASCADE;
    """)
