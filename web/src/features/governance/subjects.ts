/**
 * What a grant or a takedown is ABOUT, named once.
 *
 * The vocabulary belongs to the server. `_SUBJECT_TABLES` in
 * `app/api/routers/platform_ops.py` is the single map from a subject type to the
 * table it lives in, and `POST /content-grants` refuses anything absent from it.
 * It reaches the contract as the `subject_type` enum on `ContentGrantCreate`, so
 * this module takes the union from the GENERATED client rather than retyping the
 * seven strings — a second copy of a vocabulary that lives in one place on the
 * server is a copy that goes stale, and it goes stale quietly.
 *
 * `LABELS` is a `Record` over that union rather than an array of pairs for the
 * one property an array cannot have: adding a subject type server-side
 * regenerates the enum and this file then stops compiling until the new type is
 * given a name. Nothing else here is un-forgettable.
 */

import type { components } from "../../api/schema";

export type SubjectType = components["schemas"]["ContentGrantCreate"]["subject_type"];

const LABELS: Record<SubjectType, string> = {
  test: "Test",
  passage: "Passage",
  audio_track: "Audio track",
  question_group: "Question group",
  question: "Question",
  cue_card_set: "Cue card set",
  band_map: "Band map",
};

/** The vocabulary, in the order the pickers offer it. Derived from `LABELS`
 *  rather than written out beside it — `Object.keys` widens to `string[]` and
 *  the assertion is only recovering what the `Record` already guarantees. */
export const SUBJECT_TYPES = Object.keys(LABELS) as SubjectType[];

/**
 * A subject type in words, and the raw word when it is not one we know.
 *
 * Both listings type `subject_type` as a plain string, not the enum, and for
 * takedowns that is accurate rather than sloppy: `POST /takedowns` is
 * unauthenticated and does not validate the type at all — it looks the table up,
 * finds nothing, and files the claim with `subject_id = 0` anyway, so that a
 * mistyped subject still reaches a human. A queue row can therefore carry a word
 * that is not in this map, and rendering it as an empty cell would hide a real
 * allegation from the person whose job is to read it.
 */
export function subjectLabel(type: string | undefined | null): string {
  if (!type) return "Unknown";
  return LABELS[type as SubjectType] ?? type;
}
