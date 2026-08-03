/**
 * What a slot's settings commit its author to, and who will ever be able to
 * enter it.
 *
 * Pure and separate from the screen for two reasons. The first is the ordinary
 * one: these rules are testable without a DOM. The second is that most of them
 * are not about validity at all — they are about REACH, and reach is the thing a
 * teacher gets wrong silently. Every refusal below was measured against the
 * running API, and so was every case where the server accepts a slot that nobody
 * will ever see.
 */

export type AgeBand = "minor" | "adult" | "mixed_supervised";
export type Audience = "public" | "org" | "cohort";

/** The three values `SpeakingSlotCreate.age_band` accepts. Read from the
 *  handler's pattern, not guessed: anything else is a 422. */
export const AGE_BANDS: readonly AgeBand[] = ["minor", "adult", "mixed_supervised"];

export interface SlotDraft {
  startsAt: string;              // the datetime-local value, browser zone
  ageBand: AgeBand | "";
  audience: Audience;
  cohortXid: string;
  bandMin: string;
  bandMax: string;
}

/** Who is creating it, which decides what they can reach. */
export interface Creator {
  isPlatformAdmin: boolean;
  hasOrg: boolean;
  isMinor: boolean;
}

function band(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

/**
 * Refusals: the server would reject this, or accept it and produce a slot that
 * cannot work.
 *
 * The first three mirror checks in `speaking.create_slot` — `mixed_requires_cohort`
 * (409), `invalid_band_range` (409), and the `ge=0, le=9` bounds on the request
 * model (422). Refusing here is not a substitute for the server, which checks
 * again; it is so the teacher sees the reason next to the control that caused it
 * rather than as a sentence at the top of the page.
 *
 * The last two are refusals the server does NOT make. A slot starting in the
 * past is created with 201 and appears in no listing, because `list_slots`
 * filters `starts_at >= now`. An `org` slot from an account with no organization
 * stores `org_id = NULL` and matches `s.org_id = ANY(:orgs)` for nobody — both
 * measured.
 */
export function slotProblems(draft: SlotDraft, creator: Creator): string[] {
  const problems: string[] = [];

  if (draft.ageBand === "") {
    problems.push("Choose who this session is for. It decides who may ever enter it.");
  }
  if (draft.ageBand === "mixed_supervised" && draft.audience !== "cohort") {
    problems.push(
      "A mixed-age session has to be a class session, so a teacher is present. " +
      "Choose a class, or set the age group to under 18 or 18 and over.",
    );
  }
  if (draft.audience === "cohort" && !draft.cohortXid) {
    problems.push("Choose the class this session is for.");
  }
  if (draft.audience === "org" && !creator.hasOrg) {
    problems.push(
      "You are not a member of a centre, so a centre session would be saved with " +
      "no centre attached and shown to nobody. Open a public session instead.",
    );
  }

  const low = band(draft.bandMin);
  const high = band(draft.bandMax);
  for (const [value, label] of [[low, "lowest"], [high, "highest"]] as const) {
    if (value !== null && (Number.isNaN(value) || value < 0 || value > 9)) {
      problems.push(`The ${label} band must be between 0 and 9.`);
    }
  }
  if (low !== null && high !== null && !Number.isNaN(low) && !Number.isNaN(high)
      && low > high) {
    problems.push("The band range must run from low to high.");
  }

  if (!draft.startsAt) {
    problems.push("Set a start time.");
  } else if (new Date(draft.startsAt).getTime() <= Date.now()) {
    problems.push(
      "The start time has passed. A session in the past is saved but appears in " +
      "nobody's list, so it cannot be booked.",
    );
  }

  return problems;
}

/**
 * Will the slot this account just created come back in its own
 * `GET /speaking/slots`?
 *
 * That listing is the BOOKABLE list for the caller, not a list of what they
 * created, and the difference is not cosmetic. Measured: a teacher who creates a
 * cohort slot and is not a member of that cohort gets `[]` — the cohort branch
 * of the query is `cohort_id IN (SELECT ... FROM cohort_members WHERE user_id =
 * :u)`, and a cohort roster holds students. An adult teacher who creates a minor
 * slot gets `[]` too, because the age filter is on the CALLER's age and not on
 * anything they asked for.
 *
 * Both of those are correct for the endpoint's purpose and leave this console
 * with no listing of the slots it creates. Saying so beside the result is the
 * honest option; inventing a list is not.
 */
export function appearsInMyList(
  // `| undefined` spelled out, not just `?`: under `exactOptionalPropertyTypes`
  // an absent field and a field holding undefined are different types, and the
  // caller passes the create response straight through.
  slot: { audience?: Audience | string | undefined; age_band?: string | undefined },
  creator: Creator,
): { shown: boolean; reason: string } {
  const permitted = creator.isMinor
    ? ["minor", "mixed_supervised"]
    : ["adult", "mixed_supervised"];
  if (slot.age_band && !permitted.includes(slot.age_band)) {
    return {
      shown: false,
      reason:
        "The list below only ever shows sessions for your own age group, so this " +
        "one will not be in it. That filter is what keeps minors and adults apart " +
        "and it applies to staff accounts too.",
    };
  }
  if (slot.audience === "cohort") {
    return {
      shown: false,
      reason:
        "The list below shows class sessions only to members of that class, so " +
        "this one will not be in it unless you are on the class roster. Keep the " +
        "session details above if you need them.",
    };
  }
  return { shown: true, reason: "" };
}

/** A plain sentence for the age band, used where the radio is not visible. */
export function ageBandLabel(band: AgeBand | string): string {
  if (band === "minor") return "Under 18 only";
  if (band === "adult") return "18 and over only";
  if (band === "mixed_supervised") return "Mixed ages, teacher supervised";
  return band;
}

/** How a slot's ability range reads to a person. Matches `_range_label` on the
 *  server, which is the wording a refused student is shown. */
export function rangeLabel(min: number | null | undefined,
                           max: number | null | undefined): string {
  if (min == null && max == null) return "Any band";
  if (min != null && max != null) return `Band ${min}-${max}`;
  return min != null ? `Band ${min} and above` : `Band ${max} and below`;
}
