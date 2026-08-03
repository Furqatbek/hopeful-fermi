/**
 * The three states these screens are read in are an empty class, a class where
 * nobody has started, and a class of one — and all three are the states a real
 * centre hits first, in that order, in its first fortnight on the product.
 *
 * The string cases are not defensive padding. `GET /cohorts/{xid}/progress`
 * answers `"5.5"` where the generated type says `5.5`, so every band on that
 * screen arrives as a string, and a helper that only handled numbers would be
 * green here and blank on the page.
 */

import { describe, expect, it } from "vitest";

import {
  attendanceRows,
  band,
  bandHeight,
  completion,
  counts,
  numeric,
  orderAttendance,
  outstanding,
  personName,
  segments,
  studentSpreads,
  trend,
  unlisted,
  weekLabel,
  weekSeries,
  whole,
} from "./cohort";

describe("numeric", () => {
  it("parses the strings the progress endpoint actually sends", () => {
    // Observed against the running API: `sum()` and `round(avg(...))` are both
    // PostgreSQL `numeric`, which serializes as a quoted string.
    expect(numeric("5.5")).toBe(5.5);
    expect(numeric("2")).toBe(2);
    expect(numeric("7.0")).toBe(7);
  });

  it("passes real numbers through", () => {
    expect(numeric(6.5)).toBe(6.5);
    expect(numeric(0)).toBe(0);
  });

  it("answers null for absent, never zero", () => {
    expect(numeric(null)).toBeNull();
    expect(numeric(undefined)).toBeNull();
  });

  it("rejects blank rather than reading it as zero", () => {
    // `Number("")` and `Number(" ")` are both 0. A band that came back blank
    // would render as 0.0 — on a 4-to-9 scale that is a whole class failing.
    expect(numeric("")).toBeNull();
    expect(numeric("   ")).toBeNull();
  });

  it("rejects what is not a number at all", () => {
    expect(numeric("n/a")).toBeNull();
    expect(numeric(true)).toBeNull();
    expect(numeric({})).toBeNull();
    expect(numeric(Number.NaN)).toBeNull();
    expect(numeric(Number.POSITIVE_INFINITY)).toBeNull();
  });
});

describe("whole", () => {
  it("coerces the counts", () => {
    expect(whole("3")).toBe(3);
    expect(whole(3)).toBe(3);
  });

  it("floors absent and negative at zero, because a count is drawn as a width", () => {
    expect(whole(null)).toBe(0);
    expect(whole(undefined)).toBe(0);
    expect(whole(-2)).toBe(0);
  });
});

describe("band", () => {
  it("shows one decimal, the way a report form does", () => {
    expect(band(7)).toBe("7.0");
    expect(band(5.5)).toBe("5.5");
  });

  it("shows a dash rather than 0.0 for no band", () => {
    expect(band(null)).toBe("—");
  });
});

describe("weekLabel", () => {
  it("labels the week it was given", () => {
    // Not via `new Date("2026-07-06")`, which is midnight UTC and renders as the
    // 5th anywhere west of it — a Monday labelled Sunday.
    expect(weekLabel("2026-07-06")).toBe("6 Jul");
    expect(weekLabel("2026-01-01")).toBe("1 Jan");
    expect(weekLabel("2026-12-28")).toBe("28 Dec");
  });

  it("returns what it was given when that is not a date", () => {
    expect(weekLabel("last week")).toBe("last week");
    expect(weekLabel("2026-13-40")).toBe("2026-13-40");
  });
});

describe("segments", () => {
  const parts = (row: Record<string, number>) => segments(counts(row));

  it("splits one student's record into parts that add up", () => {
    // Three assigned: one finished on time, one finished late, one never opened.
    const split = parts({ assigned: 3, started: 2, completed: 2, late: 1 });
    expect(split).toEqual({ onTime: 1, late: 1, unfinished: 0, notStarted: 1 });
    expect(split.onTime + split.late + split.unfinished + split.notStarted).toBe(3);
  });

  it("does not double-count a late submission as completed and late", () => {
    // `late` is a subset of `completed` in what the refresh job writes: it needs
    // a `submitted_at`, and submitting is what sets the status `completed` reads.
    // Adding them would report four pieces of work where three were set.
    const split = parts({ assigned: 2, started: 2, completed: 2, late: 2 });
    expect(split).toEqual({ onTime: 0, late: 2, unfinished: 0, notStarted: 0 });
  });

  it("separates started-and-unfinished from never-started", () => {
    const split = parts({ assigned: 4, started: 3, completed: 1, late: 0 });
    expect(split.unfinished).toBe(2);
    expect(split.notStarted).toBe(1);
  });

  it("never produces a negative segment", () => {
    // `late` and `completed` come from two independent columns, so a row where
    // late exceeds completed is possible. A negative width breaks the row's
    // layout; a capped one is merely shorter, and the count beside it is printed
    // from `counts`, unaltered.
    const split = parts({ assigned: 1, started: 1, completed: 0, late: 1 });
    expect(Object.values(split).every((value) => value >= 0)).toBe(true);
    expect(split.onTime + split.late + split.unfinished + split.notStarted).toBe(1);
    expect(counts({ assigned: 1, started: 1, completed: 0, late: 1 }).late).toBe(1);
  });

  it("is all zeroes for a student with nothing assigned", () => {
    expect(parts({ assigned: 0, started: 0, completed: 0, late: 0 }))
      .toEqual({ onTime: 0, late: 0, unfinished: 0, notStarted: 0 });
  });
});

describe("completion", () => {
  it("is a percentage of what was set", () => {
    expect(completion(counts({ assigned: 4, started: 3, completed: 3, late: 0 })))
      .toBe(75);
  });

  it("counts a late submission as completed", () => {
    // It was handed in. Whether it was on time is a separate column and a
    // separate conversation; a centre that reported late work as not done would
    // be telling a parent their child did nothing.
    expect(completion(counts({ assigned: 2, started: 2, completed: 2, late: 2 })))
      .toBe(100);
  });

  it("is null, not 0%, when nothing was ever set", () => {
    // Nobody assigned this student any work. 0% blames the student for it.
    expect(completion(counts({ assigned: 0, started: 0, completed: 0, late: 0 })))
      .toBeNull();
  });
});

describe("personName", () => {
  it("joins the parts that are there", () => {
    expect(personName({ given_name: "Aziza", family_name: "Karimova" }))
      .toBe("Aziza Karimova");
  });

  it("copes with the family name the API leaves null", () => {
    // Observed: the seeded student comes back with `family_name: null`.
    expect(personName({ given_name: "Aziza", family_name: null })).toBe("Aziza");
  });

  it("falls back to a handle rather than rendering blank", () => {
    expect(personName({ xid: "019fc8e4-c70a-7642-9be6-a5944c2338a4" }))
      .toBe("Student 019fc8e4");
    expect(personName(undefined)).toBe("Unnamed student");
  });
});

describe("attendanceRows and orderAttendance", () => {
  const cohort = [
    { user: { xid: "a", given_name: "Zulfiya" },
      assigned: 3, started: 3, completed: 3, late: 0 },
    { user: { xid: "b", given_name: "Aziza" },
      assigned: 3, started: 1, completed: 0, late: 0 },
    { user: { xid: "c", given_name: "Malika" },
      assigned: 3, started: 3, completed: 2, late: 2 },
  ];

  it("is empty for an empty cohort", () => {
    expect(attendanceRows([])).toEqual([]);
    expect(orderAttendance([], "name")).toEqual([]);
    expect(orderAttendance([], "attention")).toEqual([]);
  });

  it("carries one student through", () => {
    const only = attendanceRows([cohort[2]!]);
    expect(only).toHaveLength(1);
    expect(only[0]!.name).toBe("Malika");
    expect(only[0]!.counts.late).toBe(2);
    expect(only[0]!.parts.onTime).toBe(0);
    expect(only[0]!.completion).toBe(67);
  });

  it("orders by name so a parent's child can be found", () => {
    expect(orderAttendance(attendanceRows(cohort), "name").map((r) => r.name))
      .toEqual(["Aziza", "Malika", "Zulfiya"]);
  });

  it("orders by outstanding work when asked who to chase", () => {
    // Aziza owes 3 (one started, two untouched), Malika owes 1, Zulfiya none.
    expect(orderAttendance(attendanceRows(cohort), "attention").map((r) => r.name))
      .toEqual(["Aziza", "Malika", "Zulfiya"]);
  });

  it("breaks a tie on outstanding work with lateness, then the name", () => {
    const tied = attendanceRows([
      { user: { xid: "a", given_name: "Bek" },
        assigned: 2, started: 2, completed: 2, late: 0 },
      { user: { xid: "b", given_name: "Anvar" },
        assigned: 2, started: 2, completed: 2, late: 2 },
      { user: { xid: "c", given_name: "Aziza" },
        assigned: 2, started: 2, completed: 2, late: 0 },
    ]);
    expect(orderAttendance(tied, "attention").map((r) => r.name))
      .toEqual(["Anvar", "Aziza", "Bek"]);
  });

  it("leaves the source array alone", () => {
    const prepared = attendanceRows(cohort);
    orderAttendance(prepared, "attention");
    expect(prepared.map((r) => r.name)).toEqual(["Zulfiya", "Aziza", "Malika"]);
  });

  it("keys a row without a user, rather than colliding on undefined", () => {
    const rows = attendanceRows([{ assigned: 1 }, { assigned: 2 }]);
    expect(rows[0]!.key).not.toBe(rows[1]!.key);
  });
});

describe("outstanding", () => {
  it("counts work not finished, however far it got", () => {
    expect(outstanding(segments(counts(
      { assigned: 5, started: 3, completed: 1, late: 1 })))).toBe(4);
  });

  it("is zero for a class that has done everything", () => {
    expect(outstanding(segments(counts(
      { assigned: 5, started: 5, completed: 5, late: 5 })))).toBe(0);
  });
});

describe("unlisted", () => {
  const members = [
    { user: { xid: "a", given_name: "Aziza" }, status: "active" },
    { user: { xid: "b", given_name: "Bek" }, status: "active" },
  ];

  it("names the class members the grid has no row for", () => {
    // Confirmed against the API: a cohort of two where only one has ever been
    // assigned anything returns one row. The other student is simply absent, and
    // absent from a page shown to a parent reads as not in the class.
    expect(unlisted(members, [{ user: { xid: "a" } }])).toEqual(["Bek"]);
  });

  it("is empty when everybody has a row", () => {
    expect(unlisted(members, [{ user: { xid: "a" } }, { user: { xid: "b" } }]))
      .toEqual([]);
  });

  it("names everybody when nobody has been set any work", () => {
    expect(unlisted(members, [])).toEqual(["Aziza", "Bek"]);
  });

  it("ignores a student who has left the class", () => {
    expect(unlisted([...members, { user: { xid: "c", given_name: "Nodir" },
                                   status: "left" }], [])).toEqual(["Aziza", "Bek"]);
  });

  it("is empty for an empty class", () => {
    expect(unlisted([], [])).toEqual([]);
  });
});

describe("weekSeries", () => {
  it("is empty for a cohort that has sat nothing", () => {
    expect(weekSeries([])).toEqual([]);
  });

  it("reads the strings the endpoint sends", () => {
    // Verbatim from a real response.
    expect(weekSeries([{ week: "2026-07-06", attempts: "2", avg_band: "5.5",
                         reading_band: "5.5", listening_band: null }]))
      .toEqual([{ week: "2026-07-06", attempts: 2, overall: 5.5, reading: 5.5,
                  listening: null }]);
  });

  it("sorts oldest first whatever order it arrived in", () => {
    const series = weekSeries([{ week: "2026-07-13" }, { week: "2026-06-29" },
                               { week: "2026-07-06" }]);
    expect(series.map((point) => point.week))
      .toEqual(["2026-06-29", "2026-07-06", "2026-07-13"]);
  });

  it("drops a row with no week, which has nowhere to go on a time axis", () => {
    expect(weekSeries([{ avg_band: "6.0" }, { week: "2026-07-06" }]))
      .toHaveLength(1);
  });
});

describe("bandHeight", () => {
  it("puts the ends of the scale at the ends of the bar", () => {
    expect(bandHeight(4)).toBe(0);
    expect(bandHeight(9)).toBe(100);
    expect(bandHeight(6.5)).toBe(50);
  });

  it("folds a band below the scale into its floor rather than dropping it", () => {
    // The same rule `distribution()` uses. A 3.5 is a real result and the student
    // who got one is in the room; the exact figure is printed in the table.
    expect(bandHeight(3)).toBe(0);
    expect(bandHeight(11)).toBe(100);
  });

  it("is null for a week with no band, so the chart can leave a gap", () => {
    // A zero-height bar and a week nobody sat anything look identical, and one
    // of them is a class that stopped turning up.
    expect(bandHeight(null)).toBeNull();
  });
});

describe("trend", () => {
  const point = (week: string, overall: number | null) =>
    ({ week, attempts: 1, overall, reading: null, listening: null });

  it("reads first week to last week", () => {
    expect(trend([point("2026-07-06", 5), point("2026-07-13", 6.5)], "overall"))
      .toEqual({ from: 5, to: 6.5, delta: 1.5 });
  });

  it("reports a DECLINE as a decline", () => {
    // The reason this exists. `delta` on each student is `max(best) - min(avg)`,
    // so a class that got worse is reported as improving by the size of its own
    // fall — and "did they improve" is the question a centre is asked.
    expect(trend([point("2026-07-06", 7), point("2026-07-13", 5)], "overall"))
      .toEqual({ from: 7, to: 5, delta: -2 });
  });

  it("skips the weeks with no band at either end", () => {
    expect(trend([point("2026-07-06", null), point("2026-07-13", 5),
                  point("2026-07-20", 6), point("2026-07-27", null)], "overall"))
      .toEqual({ from: 5, to: 6, delta: 1 });
  });

  it("is null with one week — a point is not a direction", () => {
    expect(trend([point("2026-07-06", 6)], "overall")).toBeNull();
  });

  it("is null with no weeks at all", () => {
    expect(trend([], "overall")).toBeNull();
  });

  it("is null when only one week carries a band for that skill", () => {
    // A cohort that sat reading twice and listening once has a reading trend and
    // no listening trend, from the same two rows.
    const weeks = [
      { week: "2026-07-06", attempts: 1, overall: 5, reading: 5, listening: 5 },
      { week: "2026-07-13", attempts: 1, overall: 6, reading: 6, listening: null },
    ];
    expect(trend(weeks, "reading")).toEqual({ from: 5, to: 6, delta: 1 });
    expect(trend(weeks, "listening")).toBeNull();
  });

  it("rounds off the floating point rather than showing 1.4000000000000004", () => {
    expect(trend([point("2026-07-06", 5.9), point("2026-07-13", 7.3)], "overall")
      ?.delta).toBe(1.4);
  });
});

describe("studentSpreads", () => {
  it("is empty for an empty cohort", () => {
    expect(studentSpreads([])).toEqual([]);
  });

  it("names the fields for what they are, lowest and highest", () => {
    // `first_band` is `min(avg_band)` and `latest_band` is `max(best_band)` over
    // every week, so the API's own names are the wrong way round for a student
    // who declined. Verified: bands of 7.0 then 5.0 answer first 5.0, latest 7.0.
    expect(studentSpreads([{ user: { xid: "a", given_name: "Aziza" },
                             attempts: 2, first_band: "5.0", latest_band: "7.0" }]))
      .toEqual([{ key: "a", name: "Aziza", weeks: 2, lowest: 5, highest: 7,
                  spread: 2 }]);
  });

  it("carries a single student with one week and one band", () => {
    const only = studentSpreads([{ user: { xid: "a", given_name: "Aziza" },
                                   attempts: 1, first_band: "6.0",
                                   latest_band: "6.0" }]);
    expect(only[0]!.spread).toBe(0);
  });

  it("leaves the spread null when a band is missing", () => {
    expect(studentSpreads([{ user: { xid: "a" }, attempts: 1,
                             first_band: null, latest_band: null }])[0]!.spread)
      .toBeNull();
  });

  it("sorts by highest band, students with no band last", () => {
    const sorted = studentSpreads([
      { user: { xid: "a", given_name: "Aziza" }, first_band: "5.0",
        latest_band: "6.0" },
      { user: { xid: "b", given_name: "Bek" } },
      { user: { xid: "c", given_name: "Malika" }, first_band: "6.5",
        latest_band: "8.0" },
    ]);
    expect(sorted.map((row) => row.name)).toEqual(["Malika", "Aziza", "Bek"]);
  });

  it("rounds the spread", () => {
    expect(studentSpreads([{ user: { xid: "a" }, first_band: "5.9",
                             latest_band: "7.3" }])[0]!.spread).toBe(1.4);
  });
});
