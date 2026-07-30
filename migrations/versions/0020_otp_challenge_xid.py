"""otp_challenges.xid: give the challenge an identity the server can look up

Revision ID: 0020
Revises: 0019

`POST /auth/otp/request` returns a `challenge_xid` and the client sends it back
to `/auth/otp/verify`. The column did not exist. The value was generated in
Python and then folded into `code_hash = sha256(f"{challenge_xid}:{code}")` and
nowhere else, so the only way to find a challenge row was to already know the
code.

That is what made `max_attempts` unenforceable. `otp_verify` checked
`attempts >= max_attempts` and nothing ever incremented `attempts`, because a
WRONG code hashes to a row that does not exist — counting only matched rows
counts only correct guesses. With an addressable challenge the attempt is
charged to the challenge before the code is compared, which is the only ordering
that costs an attacker something per guess.

Backfilled with random uuids: existing rows are short-lived (5 minutes) and their
xids are unrecoverable by construction, so any value is as good as any other. The
NOT NULL is added after the backfill so the migration works on a live table.
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE otp_challenges ADD COLUMN xid uuid;
    UPDATE otp_challenges SET xid = gen_random_uuid() WHERE xid IS NULL;
    ALTER TABLE otp_challenges ALTER COLUMN xid SET NOT NULL;
    ALTER TABLE otp_challenges ALTER COLUMN xid SET DEFAULT gen_random_uuid();

    -- Unique, because the verify path now addresses a single row by it. A
    -- duplicate would make "which challenge is this attempt against" ambiguous
    -- and the attempt counter meaningless again.
    CREATE UNIQUE INDEX otp_challenges_xid_uq ON otp_challenges (xid);
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX IF EXISTS otp_challenges_xid_uq;
    ALTER TABLE otp_challenges DROP COLUMN IF EXISTS xid;
    """)
