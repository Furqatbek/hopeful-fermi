"""billing: products, orders, two-phase payments, entitlements

Revision ID: 0015
Revises: 0014

Both Uzbek rails are TWO-PHASE and INBOUND-DRIVEN (ADR-0001 section 8.8):
  Payme -- JSON-RPC: CheckPerformTransaction -> CreateTransaction -> PerformTransaction
           (plus CancelTransaction / CheckTransaction / GetStatement)
  Click -- Prepare -> Complete callbacks
So `payments` models a state machine, not a charge result. Amounts are in MINOR
units (tiyin) because that is what both providers transact in.
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE products (
        id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid         uuid        NOT NULL DEFAULT gen_random_uuid(),
        code        text        NOT NULL,
        kind        text        NOT NULL CHECK (kind IN ('subscription','one_off','seat_licence')),
        name        text        NOT NULL,
        description text,
        -- Which entitlement features this product grants, as data:
        -- [{"feature": "mock.unlimited"}, {"feature": "competition.entry", "quantity": 4}]
        -- Adding a plan is a row, not a code change.
        features    jsonb       NOT NULL DEFAULT '[]',
        active      boolean     NOT NULL DEFAULT true,
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX products_code_uq ON products (code);

    CREATE TABLE prices (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        product_id   bigint      NOT NULL REFERENCES products(id),
        currency     char(3)     NOT NULL DEFAULT 'UZS',
        -- Tiyin. Never a float, never a decimal-string round trip through a provider.
        amount_minor bigint      NOT NULL,
        interval     text        CHECK (interval IN ('month','year')),
        min_quantity int         NOT NULL DEFAULT 1,
        active       boolean     NOT NULL DEFAULT true,
        valid_from   timestamptz NOT NULL DEFAULT now(),
        valid_to     timestamptz
    );
    -- The pricing page and checkout both read the live price for a product.
    CREATE INDEX prices_product_idx ON prices (product_id) WHERE active;

    CREATE TABLE orders (
        id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid          uuid        NOT NULL DEFAULT gen_random_uuid(),
        user_id      bigint      REFERENCES users(id),
        org_id       bigint      REFERENCES organizations(id),
        product_id   bigint      NOT NULL REFERENCES products(id),
        price_id     bigint      NOT NULL REFERENCES prices(id),
        quantity     int         NOT NULL DEFAULT 1,
        amount_minor bigint      NOT NULL,
        currency     char(3)     NOT NULL,
        status       text        NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending','awaiting_payment','paid','cancelled',
                                                   'refunded','failed','expired')),
        provider     text        CHECK (provider IN ('click','payme','manual_bank')),
        -- The merchant-side order identifier both providers echo back in every
        -- callback. This is what a reconciliation run joins on.
        reference    text        NOT NULL,
        metadata     jsonb       NOT NULL DEFAULT '{}',
        created_at   timestamptz NOT NULL DEFAULT now(),
        paid_at      timestamptz,
        cancelled_at timestamptz,
        expires_at   timestamptz,
        -- B2C orders belong to a user; B2B seat licences belong to an org.
        CHECK (user_id IS NOT NULL OR org_id IS NOT NULL)
    );
    CREATE UNIQUE INDEX orders_xid_uq ON orders (xid);
    CREATE UNIQUE INDEX orders_reference_uq ON orders (reference);
    CREATE INDEX orders_user_idx ON orders (user_id, created_at DESC) WHERE user_id IS NOT NULL;
    CREATE INDEX orders_org_idx ON orders (org_id, created_at DESC) WHERE org_id IS NOT NULL;
    -- Expiry sweeper for abandoned checkouts.
    CREATE INDEX orders_pending_idx ON orders (expires_at)
        WHERE status IN ('pending','awaiting_payment');

    CREATE TABLE payments (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid             uuid        NOT NULL DEFAULT gen_random_uuid(),
        order_id        bigint      NOT NULL REFERENCES orders(id),
        provider        text        NOT NULL CHECK (provider IN ('click','payme','manual_bank')),
        provider_txn_id text        NOT NULL,
        -- The two-phase state machine. 'authorized' is Payme's CreateTransaction /
        -- Click's Prepare; 'captured' is PerformTransaction / Complete.
        state           text        NOT NULL
                                    CHECK (state IN ('initiated','authorized','captured','cancelled',
                                                     'failed','refunded')),
        amount_minor    bigint      NOT NULL,
        currency        char(3)     NOT NULL,
        provider_state  jsonb       NOT NULL DEFAULT '{}',
        created_at      timestamptz NOT NULL DEFAULT now(),
        authorized_at   timestamptz,
        captured_at     timestamptz,
        cancelled_at    timestamptz,
        cancel_reason   text,
        updated_at      timestamptz NOT NULL DEFAULT now()
    );
    -- THE idempotency anchor. A duplicated PerformTransaction cannot create a second
    -- payment, and a replayed callback resolves to the same row.
    CREATE UNIQUE INDEX payments_provider_txn_uq ON payments (provider, provider_txn_id);
    CREATE INDEX payments_order_idx ON payments (order_id);
    -- Daily reconciliation pulls one provider's window.
    CREATE INDEX payments_recon_idx ON payments (provider, created_at DESC);
    CREATE TRIGGER payments_updated_at BEFORE UPDATE ON payments
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

    -- Every inbound provider call, logged before it is acted on. Assume webhooks
    -- arrive twice, late, or out of order: this table is how "twice" becomes harmless
    -- and how "late" stays diagnosable.
    CREATE TABLE payment_events (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        provider        text        NOT NULL,
        provider_txn_id text,
        -- 'CheckPerformTransaction' | 'CreateTransaction' | 'Prepare' | 'Complete' | ...
        method          text        NOT NULL,
        request_id      text,
        payload         jsonb       NOT NULL,
        response        jsonb,
        signature_ok    boolean     NOT NULL DEFAULT false,
        idempotency_key text        NOT NULL,
        received_at     timestamptz NOT NULL DEFAULT now(),
        processed_at    timestamptz,
        result          text,
        error           text
    );
    -- "Webhooks arrive twice": the second delivery hits this unique index and replays
    -- the stored response instead of re-executing the side effect.
    CREATE UNIQUE INDEX payment_events_idem_uq ON payment_events (provider, idempotency_key);
    -- Full call history for one transaction, in order — the only way to debug a
    -- provider dispute.
    CREATE INDEX payment_events_txn_idx ON payment_events (provider, provider_txn_id, received_at);
    -- Unprocessed / errored callbacks needing attention.
    CREATE INDEX payment_events_unprocessed_idx ON payment_events (received_at)
        WHERE processed_at IS NULL;

    -- "Webhooks arrive ... or never." This table is the answer to never: a daily job
    -- pulls the provider's own ledger (Payme GetStatement) and diffs it against ours.
    CREATE TABLE payment_reconciliations (
        id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        provider              text        NOT NULL,
        period_start          date        NOT NULL,
        period_end            date        NOT NULL,
        provider_count        int,
        provider_amount_minor bigint,
        local_count           int,
        local_amount_minor    bigint,
        -- [{"provider_txn_id":"...","issue":"missing_locally","amount_minor":...}]
        mismatches            jsonb       NOT NULL DEFAULT '[]',
        status                text        NOT NULL DEFAULT 'clean'
                                          CHECK (status IN ('clean','mismatched','failed')),
        run_at                timestamptz NOT NULL DEFAULT now(),
        UNIQUE (provider, period_start, period_end)
    );
    CREATE INDEX payment_reconciliations_bad_idx ON payment_reconciliations (run_at DESC)
        WHERE status <> 'clean';

    -- The ONE place the product asks "is this allowed". Every gated feature calls
    -- billing.Entitlements.check(); nothing reads orders or payments directly.
    CREATE TABLE entitlements (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        xid            uuid        NOT NULL DEFAULT gen_random_uuid(),
        subject_kind   text        NOT NULL CHECK (subject_kind IN ('user','org')),
        subject_id     bigint      NOT NULL,
        -- 'mock.unlimited' | 'competition.entry' | 'speaking.live_queue' | 'authoring.seat'
        feature        text        NOT NULL,
        source_kind    text        NOT NULL
                                   CHECK (source_kind IN ('order','manual_grant','trial','seat','promo')),
        source_id      bigint,
        -- NULL quantity = unlimited; otherwise a consumable balance.
        quantity       int,
        consumed       int         NOT NULL DEFAULT 0,
        starts_at      timestamptz NOT NULL DEFAULT now(),
        expires_at     timestamptz,
        revoked_at     timestamptz,
        revoked_reason text,
        metadata       jsonb       NOT NULL DEFAULT '{}',
        created_at     timestamptz NOT NULL DEFAULT now()
    );
    -- The hottest gate query in the product: every entitlement check hits this index.
    CREATE INDEX entitlements_lookup_idx
        ON entitlements (subject_kind, subject_id, feature, expires_at) WHERE revoked_at IS NULL;
    CREATE INDEX entitlements_source_idx ON entitlements (source_kind, source_id);
    -- Renewal / expiry notice job.
    CREATE INDEX entitlements_expiry_idx ON entitlements (expires_at)
        WHERE revoked_at IS NULL AND expires_at IS NOT NULL;

    -- B2B per-seat annual licence: the org holds the entitlement, seats attach users.
    CREATE TABLE seat_assignments (
        id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        entitlement_id bigint      NOT NULL REFERENCES entitlements(id) ON DELETE CASCADE,
        user_id        bigint      NOT NULL REFERENCES users(id),
        assigned_by    bigint      REFERENCES users(id),
        assigned_at    timestamptz NOT NULL DEFAULT now(),
        released_at    timestamptz,
        UNIQUE (entitlement_id, user_id)
    );
    -- "Does this student hold a seat" — resolved during the entitlement check.
    CREATE INDEX seat_assignments_user_idx ON seat_assignments (user_id) WHERE released_at IS NULL;
    -- Seat count enforcement for a centre admin assigning the next student.
    CREATE INDEX seat_assignments_ent_idx ON seat_assignments (entitlement_id)
        WHERE released_at IS NULL;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS seat_assignments, entitlements, payment_reconciliations,
        payment_events, payments, orders, prices, products CASCADE;
    """)
