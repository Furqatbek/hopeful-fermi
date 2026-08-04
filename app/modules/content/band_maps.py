"""Validating a band map before it can score anything.

A band map turns a raw mark into the band a student is told they got, and
`scoring.BandMap.band_for` answers `None` for a mark the table does not cover —
which becomes a null band, silently, for however many students sat that paper.
`publish_gate` checks coverage, but only at publish time and only for the map
attached to the test being published. `POST /band-maps` validated nothing at
all, so a broken curve could be created, listed, selected, and only discovered
by a cohort getting no band.

Four rules, and each one is a way a real map goes wrong rather than a schema
nicety:

**Every mark is covered.** The gap is the whole failure mode above.

**No mark is covered twice.** `band_for` returns the FIRST matching row, so
overlapping ranges make the answer depend on row order — and a map that gives a
different band after an edit that "only reordered things" is the worst kind of
bug to be asked about by a parent.

**Bands never go down as marks go up.** Not a formatting rule: it is what makes
the curve a curve. A transposed pair of rows produces a table where 30 marks
scores higher than 32, which no amount of eyeballing a JSON blob catches.

**Bands are real IELTS bands.** Whole or half, 0 to 9. A band of 7.3 is not a
result anybody can be given.

Findings are collected rather than raised one at a time, because a centre
retuning a curve wants the list, not a fix-and-resubmit loop nine times over.
"""

from __future__ import annotations

from typing import Any

from app.platform.findings import Report

#: 0.0 to 9.0 in half steps. IELTS reports nothing else.
VALID_BANDS = frozenset(x / 2 for x in range(0, 19))


def findings(mapping: list[dict[str, Any]], max_raw: int, report: Report) -> None:
    """Add every problem with this mapping to `report`."""
    if max_raw < 1:
        report.add("BAND_MAP_MAX_RAW_INVALID",
                   f"A band map must cover at least one mark; max_raw is {max_raw}.",
                   path="max_raw", fix_hint="Set it to the paper's total marks.")
        return
    if not mapping:
        report.add("BAND_MAP_EMPTY", "A band map needs at least one row.",
                   path="mapping",
                   fix_hint="Add a row per band, covering 0 to max_raw.")
        return

    rows: list[tuple[int, int, float]] = []
    for index, row in enumerate(mapping):
        where = f"mapping[{index}]"
        try:
            lo, hi, band = int(row["raw_min"]), int(row["raw_max"]), float(row["band"])
        except (KeyError, TypeError, ValueError):
            report.add("BAND_MAP_ROW_MALFORMED",
                       "Each row needs raw_min, raw_max and band as numbers.",
                       path=where, fix_hint='{"raw_min": 30, "raw_max": 32, "band": 7.0}')
            continue
        if lo > hi:
            report.add("BAND_MAP_RANGE_REVERSED",
                       f"raw_min {lo} is above raw_max {hi}.", path=where,
                       fix_hint="Put the lower mark first.")
            continue
        if lo < 0 or hi > max_raw:
            report.add("BAND_MAP_RANGE_OUTSIDE",
                       f"Row covers {lo}-{hi}, outside 0-{max_raw}.", path=where,
                       fix_hint="Every row must sit inside the paper's mark range.")
            continue
        if band not in VALID_BANDS:
            report.add("BAND_MAP_BAND_INVALID",
                       f"{band} is not an IELTS band.", path=where,
                       fix_hint="Whole or half bands from 0 to 9.")
            continue
        rows.append((lo, hi, band))

    if not rows:
        return

    rows.sort()
    covered: set[int] = set()
    for lo, hi, _band in rows:
        overlap = covered & set(range(lo, hi + 1))
        if overlap:
            report.add("BAND_MAP_OVERLAP",
                       f"Mark(s) {sorted(overlap)[:8]} appear in more than one row.",
                       path="mapping",
                       fix_hint="`band_for` takes the first match, so an overlap "
                                "makes the band depend on row order.")
        covered.update(range(lo, hi + 1))

    missing = sorted(set(range(0, max_raw + 1)) - covered)
    if missing:
        report.add("BAND_MAP_GAP",
                   f"No band for mark(s): {missing[:8]}"
                   + (" …" if len(missing) > 8 else ""),
                   path="mapping",
                   fix_hint="A mark with no row scores no band at all — the "
                            "student is simply told nothing.")

    previous_band = None
    for lo, _hi, band in rows:
        if previous_band is not None and band < previous_band:
            report.add("BAND_MAP_NOT_MONOTONIC",
                       f"Band drops to {band} at mark {lo}, below {previous_band} "
                       "for a lower mark.", path="mapping",
                       fix_hint="A higher mark must never score a lower band.")
            break
        previous_band = band
