"""`record_payload_exposure`, on the branch a real snapshot never takes.

The function walks a snapshot and writes one exposure row per item shown. Its
guard — an empty snapshot writes nothing — is unreachable through the HTTP
endpoint, because the publish gate refuses a version with no questions, so it can
only be exercised here.

Worth exercising rather than deleting. `preview_version` materializes a snapshot
for an UNPUBLISHED version on purpose — "the publish gate has not run, so this is
deliberately allowed to render a broken test" — and an author who has created a
version and no questions yet is the first person to hit it. Without the guard
that is `ANY(CAST(ARRAY[] AS uuid[]))` inside an INSERT on the exam's hot path,
which is a strange way to find out.

No database: the point is that the walk returns nothing before any SQL is built.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.modules.analytics.projections import record_payload_exposure

NOW = dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.UTC)


class Exploding:
    """A session that fails the test if it is touched.

    An assertion about the return value alone would pass just as well if the
    function ran the INSERT and PostgreSQL happened to write nothing — which is
    the same answer arrived at by doing the work.
    """

    def execute(self, *args, **kwargs):
        raise AssertionError("no SQL should be built for an empty snapshot")


def _record(snapshot) -> int:
    return record_payload_exposure(
        Exploding(), snapshot=snapshot, attempt_id=1, test_version_id=1,
        user_id=1, org_id=None, context="exam", now=NOW)


@pytest.mark.parametrize("snapshot", [
    pytest.param({}, id="no sections key at all"),
    pytest.param({"sections": []}, id="a version with no sections"),
    pytest.param({"sections": [{"groups": []}]}, id="a section with no groups"),
    pytest.param({"sections": [{"groups": [{"questions": []}]}]},
                 id="a group with no questions"),
])
def test_an_empty_snapshot_records_nothing(snapshot):
    assert _record(snapshot) == 0


def test_a_malformed_section_does_not_take_the_exam_down():
    """`.get` with a default at every level, deliberately.

    This runs inside `GET /attempts/{xid}/payload`. A snapshot shape nobody
    predicted must cost an exposure row, not a student's exam — the reading is
    the product, the analytics are the by-product.
    """
    assert _record({"sections": [{}, {"groups": [{}]}]}) == 0
