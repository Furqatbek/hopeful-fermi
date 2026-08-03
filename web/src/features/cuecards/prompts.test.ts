import { describe, expect, it } from "vitest";

import { lines, promptCount, promptProblems, toBody } from "./prompts";

describe("lines", () => {
  it("takes one prompt per line and drops the blanks", () => {
    expect(lines("  Do you work?\n\n  Where do you live?  \n")).toEqual([
      "Do you work?", "Where do you live?",
    ]);
  });

  it("is empty for whitespace", () => {
    expect(lines("   \n\n")).toEqual([]);
  });
});

describe("toBody", () => {
  it("builds the three parts", () => {
    expect(toBody("Do you work?", "A journey", "where\nwhen", "Why travel?")).toEqual({
      part1: ["Do you work?"],
      part2: { topic: "A journey", bullets: ["where", "when"] },
      part3: ["Why travel?"],
    });
  });

  it("omits part 2 entirely when there is no topic", () => {
    // `part2.topic` is required by the request model, so sending `part2: {}`
    // would be a 422 on a set the author thought was complete.
    expect(toBody("a", "", "where\nwhen", "b").part2).toBeUndefined();
  });
});

describe("promptCount", () => {
  it("counts a part 2 card as one prompt", () => {
    expect(promptCount(toBody("a\nb", "Topic", "x\ny", "c"))).toBe(4);
  });

  it("is zero for the body the server accepts and should not", () => {
    expect(promptCount(toBody("", "", "", ""))).toBe(0);
  });
});

describe("promptProblems", () => {
  it("passes a complete set", () => {
    const body = toBody("Do you work?", "A journey", "where", "Why travel?");
    expect(promptProblems("Describe a journey", body, "where")).toEqual([]);
  });

  it("refuses a set with no prompts at all", () => {
    // `{"title": "x", "body": {}}` returns 201 from the running API.
    const problems = promptProblems("x", toBody("", "", "", ""), "");
    expect(problems.some((p) => p.includes("no prompts"))).toBe(true);
  });

  it("refuses an untitled set", () => {
    const body = toBody("Do you work?", "", "", "");
    expect(promptProblems("  ", body, "").some((p) => p.includes("title"))).toBe(true);
  });

  it("refuses bullets with no topic above them", () => {
    const body = toBody("Do you work?", "", "where\nwhen", "");
    expect(promptProblems("t", body, "where\nwhen")
      .some((p) => p.includes("no topic"))).toBe(true);
  });

  it("accepts a part-1-only set", () => {
    const body = toBody("Do you work?\nWhere do you live?", "", "", "");
    expect(promptProblems("Small talk", body, "")).toEqual([]);
  });
});
