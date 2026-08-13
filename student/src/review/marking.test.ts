import { describe, expect, it } from "vitest";

import { answersFor, nearMiss, organise, tally, type Item, type Section } from "./marking";

const item = (over: Partial<Item> = {}): Item => ({
  question_version_xid: "q1", slot_key: "s1", verdict: "correct",
  awarded: 1, max_points: 1, ...over,
});

const section = (position: number, xids: string[]): Section => ({
  position, skill: "reading", title: `Part ${position}`,
  groups: [{
    questions: xids.map((xid, i) => ({
      number: position * 10 + i, question_version_xid: xid,
      type_key: "sentence_completion", payload: {}, slot_keys: ["s1"],
    })),
  }],
});

describe("tallying", () => {
  it("counts whole slots right, not points", () => {
    // "18 of 20" is the sentence a student says. A half-credited slot is not
    // one of the eighteen.
    const t = tally([
      item({ verdict: "correct", awarded: 1 }),
      item({ verdict: "partial", awarded: 0.5 }),
      item({ verdict: "incorrect", awarded: 0 }),
    ]);
    expect(t.right).toBe(1);
    expect(t.of).toBe(3);
    expect(t.awarded).toBe(1.5);
    expect(t.max).toBe(3);
  });

  it("handles nothing at all", () => {
    expect(tally([])).toEqual({ awarded: 0, max: 0, right: 0, of: 0 });
  });
});

describe("grouping the marking under the paper", () => {
  it("puts each question under the section it came from", () => {
    const out = organise(
      [section(1, ["a"]), section(2, ["b"])],
      [item({ question_version_xid: "b" }), item({ question_version_xid: "a" })],
    );
    expect(out.map((s) => s.title)).toEqual(["Part 1", "Part 2"]);
    expect(out[0]!.questions[0]!.xid).toBe("a");
    expect(out[1]!.questions[0]!.xid).toBe("b");
  });

  it("gathers every slot of a multi-gap question into one entry", () => {
    const out = organise([section(1, ["a"])], [
      item({ question_version_xid: "a", slot_key: "s1" }),
      item({ question_version_xid: "a", slot_key: "s2", verdict: "incorrect", awarded: 0 }),
    ]);
    expect(out[0]!.questions).toHaveLength(1);
    expect(out[0]!.questions[0]!.items).toHaveLength(2);
    expect(out[0]!.questions[0]!.awarded).toBe(1);
    expect(out[0]!.questions[0]!.max).toBe(2);
  });

  it("orders slots numerically, so s10 follows s2", () => {
    const out = organise([section(1, ["a"])], [
      item({ question_version_xid: "a", slot_key: "s10" }),
      item({ question_version_xid: "a", slot_key: "s2" }),
    ]);
    expect(out[0]!.questions[0]!.items.map((i) => i.slot_key)).toEqual(["s2", "s10"]);
  });

  it("leaves out a section with nothing marked in it", () => {
    const out = organise([section(1, ["a"]), section(2, ["b"])],
                         [item({ question_version_xid: "a" })]);
    expect(out).toHaveLength(1);
  });

  it("NEVER drops an item the paper does not mention", () => {
    // A question whose key was missing at publish still produces marks. Silently
    // discarding them would show a student a total that does not add up.
    const out = organise([section(1, ["a"])], [
      item({ question_version_xid: "a" }),
      item({ question_version_xid: "ghost", number: 99, verdict: "void", awarded: 0 }),
    ]);
    expect(out).toHaveLength(2);
    expect(out[1]!.title).toBe("Marked, but not on the paper");
    expect(out[1]!.questions[0]!.question).toBeNull();
    expect(tally(out.flatMap((s) => s.questions.flatMap((q) => q.items))).of).toBe(2);
  });

  it("survives a paper it has never seen", () => {
    expect(organise([], [])).toEqual([]);
    expect(organise([], [item()])).toHaveLength(1);
  });
});

describe("the near-miss hint", () => {
  it("names the answer a one-character slip missed", () => {
    expect(nearMiss(item({
      verdict: "incorrect", raw_response: "bicicle",
      normalized_response: "bicicle", accepted_answers: ["bicycle"],
    }))).toContain("bicycle");
  });

  it("says nothing when the answer was right", () => {
    expect(nearMiss(item({ verdict: "correct", raw_response: "bicycle",
                           accepted_answers: ["bicycle"] }))).toBeNull();
  });

  it("says nothing when the answer was not close", () => {
    expect(nearMiss(item({
      verdict: "incorrect", raw_response: "train",
      normalized_response: "train", accepted_answers: ["bicycle"],
    }))).toBeNull();
  });

  it("says nothing about a blank", () => {
    // "One character away" over an empty box would be nonsense.
    expect(nearMiss(item({ verdict: "unanswered", raw_response: "",
                           accepted_answers: ["bicycle"] }))).toBeNull();
  });

  it("does not fire when only spacing differs but the mark was lost anyway", () => {
    expect(nearMiss(item({
      verdict: "incorrect", raw_response: "car park",
      normalized_response: "carpark", accepted_answers: ["car park"],
    }))).toContain("car park");
  });
});

/** A marked question whose declared slot is `s1`, as every seeded one is. */
const entryOf = (items: Item[]) => ({
  question: {
    number: 1, question_version_xid: "m", type_key: "mcq_multi",
    payload: {}, slot_keys: ["s1"],
  },
  xid: "m", number: 1, items, awarded: 1, max: 2,
});

describe("a set answer, which is not one value", () => {
  // Reported under the scorer's synthetic slot name, not the question's own.
  const set = (raw: string): Item => ({
    question_version_xid: "m", slot_key: "selection", verdict: "partial",
    awarded: 1, max_points: 2, raw_response: raw, normalized_response: raw,
    accepted_answers: ["B", "D"], explain: { primitive: "set_selection" },
  });

  it("comes back as a LIST, so the renderer can tick the boxes", () => {
    // The scorer joins the picks for display; this reverses that one join.
    // Without it a multi-select renders with nothing ticked directly under the
    // words "You wrote B, C".
    expect(answersFor(entryOf([set("B, C")]))).toEqual({ s1: ["B", "C"] });
  });

  it("survives odd spacing and a trailing comma", () => {
    expect(answersFor(entryOf([set("B,C ,")]))).toEqual({ s1: ["B", "C"] });
  });

  it("is empty when nothing was picked", () => {
    expect(answersFor(entryOf([set("")]))).toEqual({ s1: [] });
  });

  it("leaves an ordinary text answer exactly as it was written", () => {
    const text: Item = {
      question_version_xid: "q", slot_key: "s1", verdict: "correct",
      awarded: 1, max_points: 1, raw_response: "car, park",
      explain: { primitive: "text_per_slot" },
    };
    expect(answersFor(entryOf([text]))).toEqual({ s1: "car, park" });
  });

  it("offers NO spelling hint, because single letters are all one edit apart", () => {
    expect(nearMiss(set("B, C"))).toBeNull();
  });
});
