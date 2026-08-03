"""band_maps: give the platform defaults a version, so publishing is possible

Revision ID: 0026
Revises: 0025

Migration 0017 seeds two platform band maps — "IELTS Academic Reading (default)"
and "IELTS Listening (default)" — and seeds no VERSIONS for them. A band map with
no version is a name and nothing else: the curve lives on
`band_map_versions.mapping`, `test_versions.band_map_version_id` references a
version rather than a map, and `GET /band-maps` reports `current_version: null`.

The consequence is not cosmetic. The publish gate refuses a test version with no
band map (`BAND_MAP_MISSING`), so on a fresh installation there was no version to
reference and **nothing could be published at all** — by anyone, ever, until a
centre authored its own curve through an endpoint the admin console does not
expose. Measured on a migrated database: two band maps, zero versions between
them.

The values below are the widely published raw-to-band conversions and are
**indicative, not official** — Cambridge sets boundaries per test paper and does
not publish a universal table. That is precisely why band maps are DATA scoped by
`org_id`: a centre that needs different boundaries creates its own version and
every score records which version produced it, so changing the curve later is a
regrade with an audit trail rather than a silent reinterpretation of past results.

Idempotent, and it does not touch a centre's own maps.
"""

from __future__ import annotations

import json

from alembic import op
from sqlalchemy import text

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def _mapping(bands: list[tuple[int, int, float]]) -> str:
    return json.dumps([{"raw_min": lo, "raw_max": hi, "band": band}
                       for lo, hi, band in bands])


# 40-question papers, contiguous and covering 0..40 with no gap: a raw score that
# falls in no band is a scored attempt with no result to show.
READING = [
    (0, 3, 2.0), (4, 5, 2.5), (6, 7, 3.0), (8, 9, 3.5), (10, 12, 4.0),
    (13, 14, 4.5), (15, 18, 5.0), (19, 22, 5.5), (23, 26, 6.0), (27, 29, 6.5),
    (30, 32, 7.0), (33, 34, 7.5), (35, 36, 8.0), (37, 38, 8.5), (39, 40, 9.0),
]

LISTENING = [
    (0, 3, 2.0), (4, 5, 2.5), (6, 7, 3.0), (8, 10, 3.5), (11, 12, 4.0),
    (13, 15, 4.5), (16, 17, 5.0), (18, 22, 5.5), (23, 25, 6.0), (26, 29, 6.5),
    (30, 31, 7.0), (32, 34, 7.5), (35, 36, 8.0), (37, 38, 8.5), (39, 40, 9.0),
]


def upgrade() -> None:
    conn = op.get_bind()
    for name, bands in (("IELTS Academic Reading (default)", READING),
                        ("IELTS Listening (default)", LISTENING)):
        conn.execute(text("""
            INSERT INTO band_map_versions (band_map_id, version_no, mapping,
                                           max_raw, status, published_at)
            SELECT m.id, 1, CAST(:mapping AS jsonb), 40, 'published', now()
              FROM band_maps m
             WHERE m.name = :name AND m.org_id IS NULL
               AND NOT EXISTS (SELECT 1 FROM band_map_versions v
                                WHERE v.band_map_id = m.id)
        """).bindparams(mapping=_mapping(bands), name=name))


def downgrade() -> None:
    # Only the platform defaults' own versions, and only when nothing references
    # them. A published test scored against one of these curves must keep it:
    # `score_runs` records the band it produced, and removing the mapping behind
    # a past result makes that result unexplainable.
    op.execute("""
        DELETE FROM band_map_versions v
         USING band_maps m
         WHERE m.id = v.band_map_id
           AND m.org_id IS NULL
           AND m.name LIKE '%(default)'
           AND NOT EXISTS (SELECT 1 FROM test_versions t
                            WHERE t.band_map_version_id = v.id)
    """)
