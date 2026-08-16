"""An idempotency key belongs to the caller who used it, not to the platform.

`idempotency_keys.user_id` has been written since 0002 and read by nothing.
`Idempotency.replay` matched on `(scope, key)` alone, so a caller presenting
somebody else's key, with a body that hashed the same, was handed that person's
stored response body. For `attempts.start` the body is a single test-version
xid — public to everyone who can see the paper — and the response is another
student's attempt.

Scoping the LOOKUP is the fix, and this index is the other half of it. Neither
is correct alone, which is the reason they ship together:

  * lookup scoped, index platform-wide → the second caller misses the replay,
    executes, and collides on `idempotency_keys_uq` at insert. The leak becomes
    a denial: guess a key, use it first, and the person it belongs to gets a
    500 on every retry.
  * lookup platform-wide, index per user → the leak stays, and now two users
    can both store a row for one key, so which response leaks depends on
    insertion order.

`NULLS NOT DISTINCT` because the column is nullable and dedupe has to keep
working for a NULL owner. Nothing writes NULL today — `idempotency()` depends on
`principal`, which is a hard 401 — but the default (NULLs are distinct) would
mean rows that cannot dedupe at all rather than rows that dedupe together, and
that failure is silent. PostgreSQL 15+; this repository is on 16.

No backfill and no data migration. Every row here carries
`expires_at = created_at + 1 day` and the GC sweep reads it: the whole table is
a one-day cache, so the old shape drains rather than needing rewriting.
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    DROP INDEX IF EXISTS idempotency_keys_uq;

    -- The dedupe anchor, per caller: a retried request from the SAME user with
    -- the same key returns the stored response, and one user's key says nothing
    -- about another's.
    CREATE UNIQUE INDEX idempotency_keys_uq
        ON idempotency_keys (scope, key, user_id) NULLS NOT DISTINCT;
    """)


def downgrade() -> None:
    # Going back re-narrows the index, which can fail on rows this migration
    # made legal — two users holding one key. Delete those rather than leave the
    # downgrade unrunnable: the table is a one-day cache, and the cost of losing
    # a replay is one request executed twice by a client that asked for it.
    op.execute("""
    DELETE FROM idempotency_keys a
     USING idempotency_keys b
     WHERE a.scope = b.scope AND a.key = b.key AND a.id > b.id;

    DROP INDEX IF EXISTS idempotency_keys_uq;
    CREATE UNIQUE INDEX idempotency_keys_uq ON idempotency_keys (scope, key);
    """)
