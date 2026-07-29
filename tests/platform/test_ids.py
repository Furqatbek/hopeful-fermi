"""Public identifiers are UUIDv7, and that is a property worth asserting.

`app/platform/ids.py` used to import `uuid7` behind a try/except that fell back
to `uuid.uuid4()`. The fallback was described as dev convenience; in practice the
development environment did not have `uuid6` installed at all, so **every id
minted in every test run for the whole life of this project was a uuid4** — and
nothing said so, because a uuid4 is a perfectly valid opaque identifier and the
suite only ever checked opacity.

What was lost is the reason the module exists: UUIDv7 embeds a millisecond
timestamp in its leading bits, so consecutive inserts land next to each other in
the index instead of scattering across it. On a table with a few thousand rows
the difference is invisible. At 20-50k users and millions of attempt answers it
is the difference between an index that stays in cache and one that does not —
and by then the ids are already in the database and the damage is not reversible.

Surfaced by installing the package into a clean environment on the declared
Python, which is what CI does and what nothing did before it existed.
"""

from __future__ import annotations

import time

from app.platform.ids import new_xid, parse_xid


class TestPublicIds:
    def test_they_are_version_7_not_version_4(self):
        """The assertion that would have failed for the entire project history."""
        assert {new_xid().version for _ in range(50)} == {7}

    def test_they_sort_in_creation_order(self):
        """The property version 7 is chosen FOR. A v4 passes every other test in
        this class and fails this one."""
        first = [new_xid() for _ in range(20)]
        time.sleep(0.005)
        second = [new_xid() for _ in range(20)]
        assert max(str(x) for x in first) < min(str(x) for x in second)

    def test_they_are_unique(self):
        assert len({new_xid() for _ in range(2000)}) == 2000

    def test_they_do_not_leak_a_sequence(self):
        """Time-ordered is not the same as guessable. `/tests/1234` would be a
        scraping API; the random tail is what stops the next id being derivable
        from this one."""
        a, b = new_xid(), new_xid()
        assert a.bytes[8:] != b.bytes[8:]

    def test_a_malformed_id_raises_rather_than_returning_none(self):
        """The API maps ValueError to 404, so an unparseable id and a
        nonexistent one are indistinguishable to someone probing for content."""
        for bad in ("", "not-a-uuid", "1234", "../../etc/passwd"):
            try:
                parse_xid(bad)
            except ValueError:
                continue
            raise AssertionError(f"{bad!r} parsed as an id")
