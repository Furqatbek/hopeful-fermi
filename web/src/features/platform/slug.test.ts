import { describe, expect, it } from "vitest";

import { SLUG_PATTERN, slugProblem, suggestSlug } from "./slug";

describe("suggestSlug", () => {
  it("lower-cases and joins words with hyphens", () => {
    expect(suggestSlug("Tashkent Prep Centre")).toBe("tashkent-prep-centre");
  });

  it("drops punctuation rather than encoding it", () => {
    expect(suggestSlug("Al-Xorazmiy IELTS & Co.")).toBe("al-xorazmiy-ielts-co");
  });

  it("folds the accents a Latin name carries", () => {
    // Written as escapes on purpose. These are the same name typed on two
    // keyboards, precomposed U+00D3 and O followed by a combining acute, and
    // they are indistinguishable in a source file. A permanent web address must
    // not depend on which of them the admin's keyboard produced.
    expect(suggestSlug("Nukus \u00D3quv")).toBe("nukus-oquv");
    expect(suggestSlug("Nukus O\u0301quv")).toBe("nukus-oquv");
  });

  it("keeps a word carrying the okina in one piece", () => {
    // `gʻ` and `oʻ` are single letters of Uzbek Latin. Treating the
    // okina as a separator — which the general non-alphanumeric rule does —
    // produced "farg-ona", a permanent web address with a hyphen through the
    // middle of the city's name. All four spellings staff actually type.
    expect(suggestSlug("Fargʻona")).toBe("fargona");
    expect(suggestSlug("Fargʼona")).toBe("fargona");
    expect(suggestSlug("Farg’ona")).toBe("fargona");
    expect(suggestSlug("Farg'ona")).toBe("fargona");
    expect(suggestSlug("Toshkent Oʻquv Markazi")).toBe("toshkent-oquv-markazi");
  });

  it("comes back empty for a name with nothing usable in it", () => {
    // A Cyrillic centre name has nothing to decompose into ASCII. Empty is the
    // honest answer, and the form renders it as "type one" rather than
    // submitting a slug the server would refuse.
    expect(suggestSlug("Тошкент")).toBe("");
    expect(suggestSlug("!!!")).toBe("");
  });

  it("refuses to suggest anything shorter than the server accepts", () => {
    expect(suggestSlug("AB")).toBe("");
    expect(suggestSlug("ABC")).toBe("abc");
  });

  it("cuts to 40 characters without leaving a trailing hyphen", () => {
    // The cut lands exactly on the separator between the two words, which would
    // otherwise fail the very pattern the cut exists to satisfy.
    const name = "Toshkent Shahar Ixtisoslashtirilgan Maktabi";
    const slug = suggestSlug(name);
    expect(slug.length).toBeLessThanOrEqual(40);
    expect(slug.endsWith("-")).toBe(false);
    expect(SLUG_PATTERN.test(slug)).toBe(true);
  });

  it("suggests something the server's pattern accepts", () => {
    for (const name of ["Tashkent Prep Centre", "IELTS 7+ Samarqand",
                        "Al-Xorazmiy IELTS & Co.", "Fargʻona Oquv"]) {
      expect(SLUG_PATTERN.test(suggestSlug(name))).toBe(true);
    }
  });
});

describe("slugProblem", () => {
  it("passes a slug the server would accept", () => {
    expect(slugProblem("tashkent-prep")).toBeNull();
    expect(slugProblem("abc")).toBeNull();
    expect(slugProblem("a".repeat(40))).toBeNull();
  });

  it("names each way of failing separately", () => {
    expect(slugProblem("")).toMatch(/short name/i);
    expect(slugProblem("ab")).toMatch(/three/i);
    expect(slugProblem("a".repeat(41))).toMatch(/40/);
    expect(slugProblem("Tashkent Prep")).toMatch(/lower-case/i);
    expect(slugProblem("тошкент")).toMatch(/lower-case/i);
    expect(slugProblem("prep_centre")).toMatch(/lower-case/i);
  });
});
