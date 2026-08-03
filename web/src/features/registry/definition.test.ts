import { describe, expect, it } from "vitest";

import { canonical, definitionRef, parseDefinition, PRIMITIVES, unknownPrimitive } from "./definition";

describe("parseDefinition", () => {
  it("reads an object", () => {
    const parsed = parseDefinition('{"key": "x", "version": 1}');
    expect(parsed.ok).toBe(true);
    if (parsed.ok) expect(parsed.value["key"]).toBe("x");
  });

  it("says what is wrong with the JSON rather than throwing", () => {
    const parsed = parseDefinition('{"key": "x",}');
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.message).toMatch(/not valid json/i);
  });

  it("refuses a list, which parses but is not a definition", () => {
    // `POST /admin/question-types` takes `body: dict` and a list reaches it as a
    // 422 about the wrong thing. Saying so here costs no round trip.
    expect(parseDefinition("[1, 2]").ok).toBe(false);
  });

  it("refuses empty text instead of sending an empty body", () => {
    expect(parseDefinition("   ").ok).toBe(false);
  });
});

describe("canonical", () => {
  it("ignores key order and whitespace", () => {
    // The failure this prevents: re-indenting a definition after validating it
    // would look like a different document, disable the register button, and
    // send the admin round the validate loop again for nothing.
    expect(canonical(JSON.parse('{"b": 1, "a": 2}'))).toBe(
      canonical(JSON.parse('{\n  "a": 2,\n  "b": 1\n}')),
    );
  });

  it("notices a changed value", () => {
    // The other half, and the load-bearing one: a validation is a statement
    // about one exact document.
    expect(canonical({ scoring: { primitive: "text_per_slot" } })).not.toBe(
      canonical({ scoring: { primitive: "choice_per_slot" } }),
    );
  });

  it("keeps array order, which is meaningful", () => {
    // `authoring.form` is the order the fields appear in the teacher's editor,
    // and `normalizers` is a chain. Sorting either would make two definitions
    // that behave differently compare equal.
    expect(canonical({ form: ["a", "b"] })).not.toBe(canonical({ form: ["b", "a"] }));
  });

  it("sorts keys nested inside arrays too", () => {
    expect(canonical({ form: [{ b: 1, a: 2 }] })).toBe(canonical({ form: [{ a: 2, b: 1 }] }));
  });
});

describe("unknownPrimitive", () => {
  it("names a fourth primitive", () => {
    expect(unknownPrimitive({ scoring: { primitive: "telepathy" } })).toBe("telepathy");
  });

  it("is quiet about the three that exist", () => {
    for (const primitive of PRIMITIVES) {
      expect(unknownPrimitive({ scoring: { primitive } })).toBeNull();
    }
  });

  it("says nothing when the primitive is missing", () => {
    // A required field the server already reports. Reporting it here as well
    // would be the start of a second copy of the registry schema.
    expect(unknownPrimitive({ scoring: {} })).toBeNull();
    expect(unknownPrimitive({})).toBeNull();
  });

  it("says nothing when scoring is not an object", () => {
    expect(unknownPrimitive({ scoring: "text_per_slot" })).toBeNull();
    expect(unknownPrimitive({ scoring: null })).toBeNull();
    expect(unknownPrimitive({ scoring: ["text_per_slot"] })).toBeNull();
  });
});

describe("definitionRef", () => {
  it("names what is about to go live", () => {
    expect(definitionRef({ key: "matching_sentence_endings", version: 1 })).toBe(
      "matching_sentence_endings@v1",
    );
  });

  it("accepts a string version, because the server does", () => {
    // `QuestionTypeDef.from_dict` calls `int(raw["version"])`. A confirmation
    // that would not name `"2"` is a confirmation that goes blank on a document
    // the server will happily register.
    expect(definitionRef({ key: "x", version: "2" })).toBe("x@v2");
  });

  it("is null when there is nothing honest to name", () => {
    expect(definitionRef({ version: 1 })).toBeNull();
    expect(definitionRef({ key: "x" })).toBeNull();
    expect(definitionRef({ key: "x", version: 1.5 })).toBeNull();
    expect(definitionRef({ key: "  ", version: 1 })).toBeNull();
    expect(definitionRef({ key: "x", version: "one" })).toBeNull();
  });
});
