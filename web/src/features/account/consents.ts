/**
 * Folding a consent LOG into the state of each consent.
 *
 * `GET /me/consents` is a ledger, not a set of switches: every `POST` appends a
 * row, and re-accepting an updated privacy notice leaves two `privacy` rows that
 * both look current. "Do they hold privacy consent, and against which document"
 * is answered by the newest row for that kind and by no other, so the screen
 * needs that reduction before it can render a straight answer.
 *
 * Answering it as "a row exists" would be wrong in the direction that matters:
 * `consents.revoked_at` is returned by the endpoint, and a revoked latest grant
 * has to read as consent NOT held even though earlier rows for the same kind sit
 * right underneath it.
 *
 * Sorted here rather than trusted from the server. The handler does order by
 * `granted_at DESC`, but this function decides which grant is authoritative and
 * that decision should not silently depend on an ORDER BY in another repository.
 */

/** The five the contract names. Fixed, so a kind nobody has ever granted still
 *  gets a row — "no marketing consent on file" is the answer to a question
 *  somebody asks, and an absent row does not answer it. */
export const CONSENT_KINDS = [
  "terms", "privacy", "parental", "stranger_matching", "marketing",
] as const;

export type ConsentKind = (typeof CONSENT_KINDS)[number];

/** Narrowed to what this reduction reads. Matches `components.schemas.Consent`
 *  minus the fields the fold does not use. */
export interface ConsentRow {
  kind: ConsentKind;
  doc_version: string;
  granted_by_kind: "self" | "parent" | "centre_admin";
  channel?: "web" | "telegram" | "sms" | "paper";
  granted_at: string;
  revoked_at?: string | null;
}

export interface ConsentState {
  kind: ConsentKind;
  /** Held right now: a latest grant exists and it is not revoked. */
  held: boolean;
  /** The newest grant of this kind, or null when there has never been one. */
  latest: ConsentRow | null;
  /** Earlier grants of the same kind. Kept as a count rather than dropped —
   *  a document version accepted three times is a history somebody may need. */
  superseded: number;
}

/**
 * The state of every consent kind, in the fixed order above.
 *
 * `granted_at` is an ISO 8601 timestamp from the server and is compared as a
 * number, not as a string: the endpoint's `iso()` output is UTC and uniform, but
 * a lexicographic comparison of dates is the kind of thing that keeps working
 * until the day a format changes underneath it.
 */
export function currentConsents(rows: ConsentRow[]): ConsentState[] {
  return CONSENT_KINDS.map((kind) => {
    const ofKind = rows
      .filter((row) => row.kind === kind)
      .sort((a, b) => Date.parse(b.granted_at) - Date.parse(a.granted_at));
    // Indexing yields `ConsentRow | undefined` under `noUncheckedIndexedAccess`,
    // which is the honest type here: an empty list is the ordinary case for a
    // member of staff who has never been asked for `parental`.
    const latest = ofKind[0] ?? null;
    return {
      kind,
      held: latest !== null && !latest.revoked_at,
      latest,
      superseded: Math.max(0, ofKind.length - 1),
    };
  });
}

/**
 * Whether this consent may be submitted, and what is missing when it may not.
 *
 * `POST /me/consents` refuses `stranger_matching` for a minor unless
 * `granted_by_kind` is `parent` AND a parent phone number is present — 403
 * `parental_consent_required`. A form that can send that request is a form that
 * produces a refusal it could have prevented, and worse, one whose staff user
 * cannot tell whether the refusal is about them or about the person the consent
 * is for.
 *
 * The parent's NAME is required here although the server checks only the phone.
 * This row is evidence for a regulator asking who authorised a fifteen-year-old
 * into voice calls with strangers, and a bare number does not name anybody.
 * Deliberately stricter than the server, and only in the direction of more
 * evidence.
 *
 * Returns null when the draft is submittable.
 */
export function consentProblem(draft: {
  kind: ConsentKind;
  docVersion: string;
  grantedByKind: "self" | "parent" | "centre_admin";
  parentName: string;
  parentPhone: string;
  isMinor: boolean;
}): string | null {
  if (!draft.docVersion.trim()) {
    return "Give the version of the document that was accepted. The version and "
      + "its hash are what make this consent evidence rather than a checkbox.";
  }
  if (draft.kind === "stranger_matching" && draft.isMinor) {
    if (draft.grantedByKind !== "parent") {
      return "This account belongs to someone under 18, so stranger matching "
        + "can only be consented to by a parent.";
    }
    if (!draft.parentName.trim()) {
      return "Give the parent's name. It is the record of who authorised this.";
    }
    if (!/^\+998[0-9]{9}$/.test(draft.parentPhone.trim())) {
      return "Give the parent's phone number as +998 and nine digits.";
    }
  }
  if (draft.grantedByKind === "parent" && !draft.parentPhone.trim()) {
    return "A consent given by a parent needs the parent's contact details.";
  }
  return null;
}
