import { describe, expect, it } from "vitest";

import {
  type Item,
  type TestVersion,
  assess,
  byConcern,
  dedupeByQuestion,
  discriminationBar,
  meanTime,
  percentCorrect,
  preferredVersion,
  summarise,
  topWrongShare,
  wrongAnswers,
} from "./items";

/** An item with everything present and nothing wrong with it. */
function item(over: Partial<Item> = {}): Item {
  return {
    number: 1,
    question_xid: "q-1",
    type_key: "sentence_completion",
    n_responses: 25,
    p_value: 0.6,
    discrimination: 0.35,
    mean_time_ms: 42_000,
    option_distribution: {},
    common_wrong: [],
    flagged: false,
    flag_reasons: [],
    ...over,
  };
}

/** The screen's whole reason for existing: the key only accepts "bike". */
const badKey = item({
  number: 7,
  n_responses: 25,
  p_value: 0.6,
  discrimination: -0.51,
  common_wrong: [{ value: "bicycle", count: 10 }],
  flagged: true,
  flag_reasons: ["negative_discrimination", "common_wrong_answer"],
});

describe("wrongAnswers", () => {
  it("computes the share the API does not send", () => {
    // `stats.analyse` puts a `share` on every entry and the DTO drops it, so
    // without this the screen has a count and no denominator.
    const [top] = wrongAnswers(badKey);
    expect(top?.share).toBeCloseTo(0.4);
  });

  it("measures a wrong answer against the students who got it wrong", () => {
    // 25 sat it, 60% correct, so 10 were wrong — and all 10 wrote the same word.
    const [top] = wrongAnswers(badKey);
    expect(top?.shareOfWrong).toBeCloseTo(1);
    expect(top?.likelyMissingAnswer).toBe(true);
  });

  it("calls a scattered wrong answer wrong rather than missing", () => {
    // Half the class got it wrong and they disagreed with each other. A
    // distractor doing its job, not an answer the key should accept.
    const scattered = item({
      n_responses: 40, p_value: 0.5, discrimination: 0.4,
      common_wrong: [{ value: "kayak", count: 5 }, { value: "canoe", count: 4 }],
    });
    expect(wrongAnswers(scattered).map((w) => w.likelyMissingAnswer))
      .toEqual([false, false]);
  });

  it("trusts a negative correlation over concentration", () => {
    // Only a quarter of the wrong answers, but the students giving it are the
    // ones scoring highest on the paper. That is what a missing alternative
    // looks like when the item also has real distractors.
    const strongStudents = item({
      n_responses: 40, p_value: 0.5, discrimination: -0.3,
      common_wrong: [{ value: "bicycle", count: 5 }],
    });
    expect(wrongAnswers(strongStudents)[0]?.likelyMissingAnswer).toBe(true);
  });

  it("does not promote a rare answer on a negative-discrimination item", () => {
    // Two students out of 40 is under the threshold whatever the correlation
    // says; otherwise every stray typo on a broken item reads as a missing key.
    const rare = item({
      n_responses: 40, p_value: 0.5, discrimination: -0.3,
      common_wrong: [{ value: "trycicle", count: 2 }],
    });
    expect(wrongAnswers(rare)[0]?.likelyMissingAnswer).toBe(false);
  });

  it("drops a blank string", () => {
    // What an unanswered item normalises to. Nobody chose to give it.
    const blank = item({ common_wrong: [{ value: "", count: 9 }] });
    expect(wrongAnswers(blank)).toEqual([]);
  });

  it("is empty when nobody has sat the item", () => {
    expect(wrongAnswers(item({ n_responses: 0, p_value: null, common_wrong: [] })))
      .toEqual([]);
  });

  it("never reports more than all of the wrong answers", () => {
    // The p-value is rounded, so the derived wrong count can land a hair under
    // the real one. 112% would read as a broken screen, not a broken item.
    const rounded = item({
      n_responses: 3, p_value: 0.3333, discrimination: -0.2,
      common_wrong: [{ value: "bicycle", count: 2 }],
    });
    expect(wrongAnswers(rounded)[0]?.shareOfWrong).toBeLessThanOrEqual(1);
  });

  it("still reports a share when the p-value is missing", () => {
    const noP = item({
      n_responses: 20, p_value: null, discrimination: null,
      common_wrong: [{ value: "bicycle", count: 8 }],
    });
    const [top] = wrongAnswers(noP);
    expect(top?.share).toBeCloseTo(0.4);
    expect(top?.shareOfWrong).toBeNull();
  });
});

describe("assess", () => {
  it("calls a negative correlation broken", () => {
    expect(assess(badKey).verdict).toBe("broken");
    expect(assess(badKey).reasons[0]).toBe("negative_discrimination");
  });

  it("calls an item nobody could answer broken", () => {
    const unanswerable = item({
      n_responses: 30, p_value: 0, discrimination: null,
      common_wrong: [{ value: "bicycle", count: 30 }],
      flagged: true, flag_reasons: ["near_zero_p", "common_wrong_answer"],
    });
    expect(assess(unanswerable).verdict).toBe("broken");
  });

  it("calls a working item with one popular wrong answer a key to check", () => {
    // Discriminates normally, so the item is not broken — but a tenth of the
    // class wrote the same thing and it is probably an answer.
    const missingAlternative = item({
      n_responses: 30, p_value: 0.6, discrimination: 0.3,
      common_wrong: [{ value: "bicycle", count: 9 }],
    });
    expect(assess(missingAlternative).verdict).toBe("check-key");
  });

  it("calls a sound item probably fine", () => {
    expect(assess(item()).verdict).toBe("fine");
    expect(assess(item()).reasons).toEqual([]);
  });

  it("does not call an item everybody answered broken", () => {
    // Too easy is a reason without being a fault. Flattening it into "broken"
    // next to a bad key destroys the distinction the screen is for.
    const tooEasy = item({
      n_responses: 30, p_value: 1, discrimination: null,
      flagged: true, flag_reasons: ["near_one_p"],
    });
    const assessment = assess(tooEasy);
    expect(assessment.verdict).toBe("fine");
    expect(assessment.reasons).toEqual(["near_one_p"]);
  });

  it("diagnoses a class too small for the server to flag", () => {
    // The centre this product is for runs classes of fifteen. Waiting for
    // twenty responses would show them a clean paper for ever.
    const smallClass = item({
      n_responses: 15, p_value: 0.6, discrimination: -0.5,
      common_wrong: [{ value: "bicycle", count: 6 }],
      flagged: false, flag_reasons: [],
    });
    const assessment = assess(smallClass);
    expect(assessment.verdict).toBe("broken");
    expect(assessment.thin).toBe(true);
  });

  it("stops calling the evidence thin at the server's own threshold", () => {
    expect(assess(item({ n_responses: 19 })).thin).toBe(true);
    expect(assess(item({ n_responses: 20 })).thin).toBe(false);
  });

  it("keeps a reason the server flagged that the numbers no longer show", () => {
    // The flagged list is a ninety-day projection. A badge that contradicts the
    // action printed beside it is worse than either alone.
    const projection = item({
      n_responses: 30, p_value: 0.6, discrimination: 0.4, common_wrong: [],
      flagged: true, flag_reasons: ["negative_discrimination"],
    });
    expect(assess(projection).verdict).toBe("broken");
  });

  it("reports no data rather than a verdict when nobody has sat it", () => {
    const unsat = item({
      n_responses: 0, p_value: null, discrimination: null,
      mean_time_ms: null, common_wrong: [],
    });
    expect(assess(unsat)).toEqual({
      verdict: "no-data", reasons: [], thin: true, n: 0,
    });
  });

  it("survives one attempt, where no correlation exists", () => {
    // The point-biserial needs two responses, so a single sitting always sends
    // null — which must not read as a discrimination of zero.
    const single = item({
      n_responses: 1, p_value: 1, discrimination: null, common_wrong: [],
    });
    const assessment = assess(single);
    expect(assessment.verdict).toBe("fine");
    expect(assessment.thin).toBe(true);
    expect(assessment.n).toBe(1);
  });

  it("survives an all-correct item, where the correlation is undefined", () => {
    const perfect = item({
      n_responses: 25, p_value: 1, discrimination: null, common_wrong: [],
      flagged: true, flag_reasons: ["near_one_p"],
    });
    expect(assess(perfect).verdict).toBe("fine");
  });

  it("does not read a null discrimination as a negative one", () => {
    // The single most damaging confusion available here: "cannot be computed"
    // rendered as a bad key would send an author to rewrite a sound item.
    const noVariance = item({
      n_responses: 25, p_value: 0.5, discrimination: null, common_wrong: [],
    });
    expect(assess(noVariance).reasons).not.toContain("negative_discrimination");
    expect(assess(noVariance).verdict).toBe("fine");
  });

  it("survives an item with no fields at all", () => {
    expect(assess({}).verdict).toBe("no-data");
  });
});

describe("byConcern", () => {
  const fine = item({ number: 1 });
  const alsoFine = item({ number: 2 });
  const worse = item({
    number: 30, n_responses: 25, p_value: 0.6, discrimination: -0.8,
    common_wrong: [{ value: "bicycle", count: 10 }],
  });
  const bad = item({
    number: 12, n_responses: 25, p_value: 0.6, discrimination: -0.2,
    common_wrong: [{ value: "bicycle", count: 10 }],
  });
  const checkKey = item({
    number: 3, n_responses: 30, p_value: 0.6, discrimination: 0.3,
    common_wrong: [{ value: "bicycle", count: 9 }],
  });
  const unsat = item({ number: 4, n_responses: 0, p_value: null, discrimination: null });

  it("puts the broken items first, worst correlation at the top", () => {
    // The finding, not the paper's numbering. Question 30 leads because it is
    // the one that will lose the school.
    expect(byConcern([fine, bad, checkKey, worse, unsat]).map((i) => i.number))
      .toEqual([30, 12, 3, 1, 4]);
  });

  it("does not sort the array it was given", () => {
    // It is handed the array inside a query cache entry.
    const given = [fine, worse];
    byConcern(given);
    expect(given.map((i) => i.number)).toEqual([1, 30]);
  });

  it("puts a null correlation at the end of the broken items, not the middle", () => {
    const nullDiscrimination = item({
      number: 5, n_responses: 30, p_value: 0, discrimination: null,
      common_wrong: [{ value: "bicycle", count: 30 }],
    });
    expect(byConcern([nullDiscrimination, bad, worse]).map((i) => i.number))
      .toEqual([30, 12, 5]);
  });

  it("ranks thin evidence below solid evidence of the same verdict", () => {
    const thin = item({
      number: 2, n_responses: 6, p_value: 0.5, discrimination: -0.9,
    });
    expect(byConcern([thin, bad]).map((i) => i.number)).toEqual([12, 2]);
  });

  it("still floats a thin broken item above a solid healthy one", () => {
    // A class of fifteen has nothing better-evidenced to compete with.
    const thin = item({
      number: 2, n_responses: 15, p_value: 0.5, discrimination: -0.9,
    });
    expect(byConcern([fine, thin]).map((i) => i.number)).toEqual([2, 1]);
  });

  it("orders items needing a key check by how popular the wrong answer is", () => {
    const louder = item({
      number: 9, n_responses: 30, p_value: 0.4, discrimination: 0.3,
      common_wrong: [{ value: "bicycle", count: 15 }],
    });
    expect(byConcern([checkKey, louder]).map((i) => i.number)).toEqual([9, 3]);
  });

  it("falls back to the paper's numbering, so the order does not wobble", () => {
    expect(byConcern([alsoFine, fine]).map((i) => i.number)).toEqual([1, 2]);
  });

  it("is empty for a paper nobody has sat", () => {
    expect(byConcern([])).toEqual([]);
  });
});

describe("dedupeByQuestion", () => {
  it("lists a question once when the endpoint sends it twice", () => {
    // The real shape: the projection writes a centre row and a platform row for
    // the same sitting, and the endpoint filters on neither.
    const centre = item({ question_xid: "q-7", number: 7, n_responses: 24 });
    const platform = item({ question_xid: "q-7", number: 7, n_responses: 24 });
    expect(dedupeByQuestion([centre, platform])).toHaveLength(1);
  });

  it("keeps the row computed over more responses", () => {
    const centre = item({ question_xid: "q-7", n_responses: 24 });
    const platform = item({ question_xid: "q-7", n_responses: 310 });
    expect(dedupeByQuestion([centre, platform])[0]?.n_responses).toBe(310);
    expect(dedupeByQuestion([platform, centre])[0]?.n_responses).toBe(310);
  });

  it("keeps the first of two identical rows, whatever order they arrive in", () => {
    const first = item({ question_xid: "q-7", number: 7, n_responses: 24 });
    const second = item({ question_xid: "q-7", number: 99, n_responses: 24 });
    expect(dedupeByQuestion([first, second])[0]?.number).toBe(7);
  });

  it("does not collapse two different questions", () => {
    expect(dedupeByQuestion([
      item({ question_xid: "q-1" }), item({ question_xid: "q-2" }),
    ])).toHaveLength(2);
  });

  it("leaves rows with no question alone rather than folding them together", () => {
    // Every field on the generated DTO is optional, so a row with no
    // `question_xid` is a shape the types allow — and folding two of them
    // together would hide a broken question.
    const anonymous: Item[] = [{ number: 1, n_responses: 25 },
                               { number: 2, n_responses: 25 }];
    expect(dedupeByQuestion(anonymous)).toHaveLength(2);
  });

  it("is empty for an empty list", () => {
    expect(dedupeByQuestion([])).toEqual([]);
  });
});

describe("summarise", () => {
  it("counts the paper before a single row is read", () => {
    expect(summarise([
      item({ discrimination: -0.4 }),
      item({ common_wrong: [{ value: "bicycle", count: 9 }] }),
      item(),
      item({ n_responses: 0, p_value: null, discrimination: null }),
    ])).toEqual({ broken: 1, checkKey: 1, fine: 1, noData: 1 });
  });

  it("is all zeroes for nothing", () => {
    expect(summarise([])).toEqual({ broken: 0, checkKey: 0, fine: 0, noData: 0 });
  });
});

describe("topWrongShare", () => {
  it("is the most popular wrong answer's share of the cohort", () => {
    expect(topWrongShare(badKey)).toBeCloseTo(0.4);
  });

  it("is zero when nothing was typed wrong", () => {
    expect(topWrongShare(item())).toBe(0);
  });
});

describe("discriminationBar", () => {
  it("points the other way for a negative correlation", () => {
    expect(discriminationBar(-0.5)).toEqual({ side: "negative", percent: 50 });
    expect(discriminationBar(0.5)).toEqual({ side: "positive", percent: 50 });
  });

  it("is null rather than an empty bar when there is no correlation", () => {
    // A zero-width bar at the centre line is a picture of "no relationship",
    // which is a claim. "Cannot be computed" is not that claim.
    expect(discriminationBar(null)).toBeNull();
    expect(discriminationBar(undefined)).toBeNull();
  });

  it("draws a zero as a positive hairline, not as nothing", () => {
    expect(discriminationBar(0)).toEqual({ side: "positive", percent: 0 });
  });

  it("clamps a value outside the correlation range", () => {
    expect(discriminationBar(-4.2)?.percent).toBe(100);
    expect(discriminationBar(9)?.percent).toBe(100);
  });
});

describe("meanTime", () => {
  it("reads in seconds and minutes", () => {
    expect(meanTime(42_000)).toBe("42s");
    expect(meanTime(95_000)).toBe("1m 35s");
    expect(meanTime(120_000)).toBe("2m 00s");
  });

  it("is null for a reported zero, not 0s", () => {
    // `attempt_answers.time_spent_ms` is NOT NULL DEFAULT 0 and client
    // reported, so zero means "no timing" and never "answered instantly".
    expect(meanTime(0)).toBeNull();
    expect(meanTime(null)).toBeNull();
    expect(meanTime(undefined)).toBeNull();
  });
});

describe("percentCorrect", () => {
  it("says what a p-value means", () => {
    expect(percentCorrect(0.6)).toBe("60%");
    expect(percentCorrect(0)).toBe("0%");
    expect(percentCorrect(1)).toBe("100%");
  });

  it("does not render a missing p-value as nobody getting it right", () => {
    expect(percentCorrect(null)).toBe("—");
    expect(percentCorrect(undefined)).toBe("—");
  });
});

describe("preferredVersion", () => {
  const version = (over: Partial<TestVersion>): TestVersion => ({
    xid: `v-${over.version_no ?? 1}`, version_no: 1, status: "draft", ...over,
  });

  it("opens on the newest published version", () => {
    expect(preferredVersion([
      version({ version_no: 1, status: "published" }),
      version({ version_no: 2, status: "published" }),
      version({ version_no: 3, status: "draft" }),
    ])?.version_no).toBe(2);
  });

  it("falls back to an archived version, which has still been sat", () => {
    expect(preferredVersion([
      version({ version_no: 1, status: "archived" }),
      version({ version_no: 2, status: "draft" }),
    ])?.version_no).toBe(1);
  });

  it("offers a draft rather than nothing", () => {
    // It will analyse to an empty screen, but choosing nothing looks like the
    // picker is broken.
    expect(preferredVersion([version({ version_no: 4 })])?.version_no).toBe(4);
  });

  it("is null for a test with no versions", () => {
    expect(preferredVersion([])).toBeNull();
  });
});
