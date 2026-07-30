"""band_map_versions.xid: the one public identifier that was an integer

Revision ID: 0021
Revises: 0020

Every public identifier in this system is an opaque UUIDv7 `xid` over a bigint
primary key. `band_map_versions` was the one table that never got the column, and
the two endpoints that expose a band-map version papered over it by stringifying
the primary key:

    "current_version": {"xid": str(current.id), ...}

So `GET /band-maps` handed a client `"7"` and called it an xid. That is the
internal id on the public surface — the thing the whole scheme exists to prevent,
and a row count a competitor can read off a response.

It also made attaching a band map impossible. `PATCH /test-versions/{xid}` takes
`band_map_version_xid: uuid.UUID` and resolved it with
`BandMapVersion.xid == ...`, an attribute that did not exist on the model — so a
real uuid raised `AttributeError` (500) and the `"7"` the API had just handed out
was rejected by request validation as not-a-uuid. Both doors shut.

That is not a cosmetic endpoint bug. `publish_gate` refuses a version with no band
map (`BAND_MAP_MISSING` — "without a band map a score is a raw count"), so with
the only attach path broken **no test could be published through the API at all**.
The suite missed it because its fixtures set `band_map_version_id` in Python.

Backfilled with `gen_random_uuid()` rather than uuid7. These rows predate the
column, so there is no creation order in the id to preserve, and inventing a
plausible-looking v7 timestamp for a row of unknown age would be worse than
honest randomness. New rows get uuid7 from the model default.
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE band_map_versions ADD COLUMN xid uuid;
    UPDATE band_map_versions SET xid = gen_random_uuid() WHERE xid IS NULL;
    ALTER TABLE band_map_versions ALTER COLUMN xid SET NOT NULL;
    ALTER TABLE band_map_versions ALTER COLUMN xid SET DEFAULT gen_random_uuid();

    -- Unique, because attaching a band map addresses a single version by it.
    CREATE UNIQUE INDEX band_map_versions_xid_uq ON band_map_versions (xid);
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX IF EXISTS band_map_versions_xid_uq;
    ALTER TABLE band_map_versions DROP COLUMN IF EXISTS xid;
    """)
