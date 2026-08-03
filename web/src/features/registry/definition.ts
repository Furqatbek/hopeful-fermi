/**
 * Reading a pasted question-type definition, before the server sees it.
 *
 * Everything here is deliberately narrow. `POST /admin/question-types/validate`
 * is the validator, and a second copy of the registry's schema in the browser
 * would be a second source of truth that is right on the day it is written —
 * `TypeForm.tsx` makes the same argument about `payload_schema` and is right.
 *
 * So this answers only the three questions the server cannot answer for us:
 *
 *   * **Is it JSON at all?** A trailing comma should not cost a round trip.
 *   * **Is `scoring.primitive` one of the three?** The closed set is the one
 *     architectural limit of the registry (ADR-0001 §8.3): a fourth primitive is
 *     code plus a deploy. The server does refuse a fourth, but its message names
 *     the bad value and not the three that would work.
 *   * **Is the definition on screen still the one that was validated?** That is
 *     what gates the register button, and it needs a comparison that ignores
 *     reformatting and notices a changed value.
 */

/**
 * The closed set, mirroring `ScoringSpec.primitive` in the contract and
 * `qtypes.primitives.PRIMITIVES` in the engine.
 *
 * Hardcoded rather than derived from the generated types, because a TypeScript
 * union does not exist at runtime and this list is rendered to a human.
 */
export const PRIMITIVES = ["choice_per_slot", "text_per_slot", "set_selection"] as const;

export type Parsed =
  | { ok: true; value: Record<string, unknown>; canonical: string }
  | { ok: false; message: string };

/**
 * Object keys sorted, recursively; array order left alone.
 *
 * Array order is meaningful in a definition — `authoring.form` is the order the
 * fields appear in the teacher's editor — so sorting it would change what the
 * document means. Object key order never is.
 */
function sorted(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sorted);
  if (value !== null && typeof value === "object") {
    const source = value as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(source).sort()) out[key] = sorted(source[key]);
    return out;
  }
  return value;
}

/**
 * One string per distinct definition, whatever the whitespace and key order.
 *
 * The same canonicalization the server checksums with
 * (`json.dumps(body, sort_keys=True, separators=(",", ":"))`), for the same
 * reason: two documents that differ only in layout are the same definition. The
 * register button compares this against the text that was validated, so
 * re-indenting the JSON does not silently discard a passing validation — and
 * changing one accepted value does.
 */
export function canonical(value: unknown): string {
  return JSON.stringify(sorted(value));
}

export function parseDefinition(text: string): Parsed {
  if (!text.trim()) return { ok: false, message: "Paste a definition first." };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (failure) {
    return { ok: false, message: `Not valid JSON. ${(failure as Error).message}` };
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return { ok: false, message: "A definition is a JSON object, not a list or a bare value." };
  }
  return { ok: true, value: value as Record<string, unknown>, canonical: canonical(value) };
}

/**
 * The `scoring.primitive` this definition asks for, when it is not one of the
 * three. Null means "nothing to say here", which covers a definition that names
 * a real primitive AND one that names none at all.
 *
 * A missing primitive is deliberately not reported. It is a required field and
 * the server says so; reporting it here would be the start of the second schema
 * this module exists to avoid.
 */
export function unknownPrimitive(value: Record<string, unknown>): string | null {
  const scoring = value["scoring"];
  if (scoring === null || typeof scoring !== "object" || Array.isArray(scoring)) return null;
  const primitive = (scoring as Record<string, unknown>)["primitive"];
  if (typeof primitive !== "string" || primitive === "") return null;
  return (PRIMITIVES as readonly string[]).includes(primitive) ? null : primitive;
}

/**
 * `key@vN`, for the confirmation that names what is about to go live.
 *
 * A string version is accepted because the server accepts one: `from_dict` calls
 * `int(raw["version"])`, so `"2"` registers as version 2. The confirmation has to
 * name the version that will actually be created — refusing to name it, or
 * naming a different one, is worse than the ceremony being slightly lenient.
 */
export function definitionRef(value: Record<string, unknown>): string | null {
  const key = value["key"];
  if (typeof key !== "string" || key.trim() === "") return null;

  const version = value["version"];
  const number =
    typeof version === "number" && Number.isInteger(version)
      ? version
      : typeof version === "string" && /^\d+$/.test(version.trim())
        ? Number(version.trim())
        : null;
  if (number === null) return null;

  return `${key}@v${number}`;
}
