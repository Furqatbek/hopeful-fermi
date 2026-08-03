import { describe, expect, it } from "vitest";

import { type ConsentRow, consentProblem, currentConsents } from "./consents";

function grant(over: Partial<ConsentRow> = {}): ConsentRow {
  return {
    kind: "privacy",
    doc_version: "privacy-2026-01",
    granted_by_kind: "self",
    granted_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

describe("currentConsents", () => {
  it("gives every kind a row even when nothing has been granted", () => {
    const states = currentConsents([]);
    expect(states.map((s) => s.kind)).toEqual([
      "terms", "privacy", "parental", "stranger_matching", "marketing",
    ]);
    expect(states.every((s) => !s.held && s.latest === null)).toBe(true);
  });

  it("answers with the newest grant, not the first one it meets", () => {
    // Deliberately out of order: the fold sorts rather than trusting the
    // endpoint's ORDER BY, so a change there cannot silently pick the old
    // document version.
    const [, privacy] = currentConsents([
      grant({ doc_version: "privacy-2026-01", granted_at: "2026-01-01T00:00:00Z" }),
      grant({ doc_version: "privacy-2026-06", granted_at: "2026-06-01T00:00:00Z" }),
      grant({ doc_version: "privacy-2026-03", granted_at: "2026-03-01T00:00:00Z" }),
    ]);
    expect(privacy?.held).toBe(true);
    expect(privacy?.latest?.doc_version).toBe("privacy-2026-06");
    expect(privacy?.superseded).toBe(2);
  });

  it("reads a revoked newest grant as consent not held", () => {
    // The failure this prevents: answering "any row exists" would report consent
    // held on the strength of an older grant sitting underneath a withdrawal.
    const [, privacy] = currentConsents([
      grant({ granted_at: "2026-01-01T00:00:00Z" }),
      grant({
        doc_version: "privacy-2026-06",
        granted_at: "2026-06-01T00:00:00Z",
        revoked_at: "2026-07-01T00:00:00Z",
      }),
    ]);
    expect(privacy?.held).toBe(false);
    expect(privacy?.latest?.doc_version).toBe("privacy-2026-06");
  });

  it("keeps kinds apart", () => {
    const states = currentConsents([
      grant({ kind: "terms", granted_at: "2026-02-01T00:00:00Z" }),
      grant({ kind: "marketing", granted_at: "2026-05-01T00:00:00Z" }),
    ]);
    const byKind = Object.fromEntries(states.map((s) => [s.kind, s]));
    expect(byKind["terms"]?.held).toBe(true);
    expect(byKind["marketing"]?.held).toBe(true);
    expect(byKind["privacy"]?.held).toBe(false);
    expect(byKind["terms"]?.superseded).toBe(0);
  });
});

describe("consentProblem", () => {
  const base = {
    kind: "terms" as const,
    docVersion: "terms-2026-01",
    grantedByKind: "self" as const,
    parentName: "",
    parentPhone: "",
    isMinor: false,
  };

  it("accepts an ordinary self-granted consent", () => {
    expect(consentProblem(base)).toBeNull();
  });

  it("refuses a blank document version", () => {
    expect(consentProblem({ ...base, docVersion: "   " })).toMatch(/version/i);
  });

  it("refuses stranger matching granted by a minor themselves", () => {
    // The exact request `POST /me/consents` answers 403
    // `parental_consent_required` for.
    const problem = consentProblem({
      ...base, kind: "stranger_matching", isMinor: true,
    });
    expect(problem).toMatch(/parent/i);
  });

  it("still requires the parent's contact details for a minor", () => {
    const named = {
      ...base,
      kind: "stranger_matching" as const,
      isMinor: true,
      grantedByKind: "parent" as const,
      parentName: "Nodira Karimova",
    };
    expect(consentProblem({ ...named, parentPhone: "" })).toMatch(/\+998/);
    expect(consentProblem({ ...named, parentPhone: "998901234567" }))
      .toMatch(/\+998/);
    expect(consentProblem({ ...named, parentPhone: "+998901234567" })).toBeNull();
  });

  it("requires a name as well as a number, which the server does not", () => {
    // Stricter than the backend on purpose: the row is evidence of who
    // authorised a minor into voice calls, and a bare number names nobody.
    expect(consentProblem({
      ...base,
      kind: "stranger_matching",
      isMinor: true,
      grantedByKind: "parent",
      parentName: "  ",
      parentPhone: "+998901234567",
    })).toMatch(/name/i);
  });

  it("lets an adult consent to stranger matching themselves", () => {
    expect(consentProblem({ ...base, kind: "stranger_matching" })).toBeNull();
  });
});
