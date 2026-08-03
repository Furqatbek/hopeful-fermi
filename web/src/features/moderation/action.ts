/**
 * What a moderation action needs before it is worth sending.
 *
 * Pure, and separate from the form, because two of these rules are the backend's
 * and one is not — and which is which matters:
 *
 *   * `reason` has `min_length=1` on the server. Kept here as well so an empty
 *     box is refused before the request rather than as a 422.
 *   * a `content_*` action must name its content, and the two subject fields go
 *     together or not at all. Both are enforced by a Pydantic model validator.
 *   * **a user action must name a user, and the server does not check that.**
 *     `take_moderation_action` revokes sessions only `if body.action in
 *     ("suspend", "ban") and target_id` — so a ban with no `target_user_xid`,
 *     or with one that matches nobody, answers 201 with `sessions_revoked: 0`
 *     and writes a row into the immutable audit log saying a user was banned.
 *     That is the worst of the three possible outcomes: not a refusal, not an
 *     action, but a record that the report was dealt with. This is the only
 *     place it is caught.
 */

/** Actions aimed at a person. */
export const USER_ACTIONS = ["warn", "mute", "shadow_limit", "suspend", "ban"] as const;

/** Actions aimed at a piece of content. */
export const CONTENT_ACTIONS = ["content_hide", "content_remove"] as const;

/** `_SUBJECT_TABLES` in `platform_ops.py`; anything else is a 404. */
export const SUBJECT_TYPES = ["test", "passage", "audio_track", "question_group",
                              "question", "cue_card_set", "band_map"] as const;

export type ModerationAction =
  (typeof USER_ACTIONS)[number] | (typeof CONTENT_ACTIONS)[number];

export type SubjectType = (typeof SUBJECT_TYPES)[number];

/** The two that end every live session on every device, at once. */
export const REVOKES_SESSIONS: readonly ModerationAction[] = ["suspend", "ban"];

/** A `<select>` yields a `string`. Narrowing it against the list rather than
 *  asserting it keeps the contract's enum as the only source of what is
 *  sendable — a value the API would refuse cannot be built here at all. */
export function toAction(value: string): ModerationAction | null {
  return [...USER_ACTIONS, ...CONTENT_ACTIONS].find((known) => known === value) ?? null;
}

export function toSubjectType(value: string): SubjectType | "" {
  return SUBJECT_TYPES.find((known) => known === value) ?? "";
}

export interface ActionDraft {
  action: ModerationAction;
  reason: string;
  targetUserXid: string;
  subjectType: SubjectType | "";
  subjectXid: string;
  /** `datetime-local`, so no timezone. See `expiryIso`. */
  expiresLocal: string;
  /** Ticked by a moderator who has read what a suspend or ban does. */
  acknowledged: boolean;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function isContent(action: string): boolean {
  return action.startsWith("content_");
}

/**
 * The first thing wrong with this draft, or `null`.
 *
 * One at a time, because these are form fields a person is filling in and a list
 * of five complaints about a mostly empty form is noise.
 */
export function problemWith(draft: ActionDraft): string | null {
  if (!draft.reason.trim()) {
    return "Give a reason. It is written to a log that cannot be edited, and "
      + "somebody reads it months later.";
  }

  if (isContent(draft.action)) {
    if (!draft.subjectXid.trim()) return "Name the content this acts on.";
    if (!draft.subjectType) return "Choose what kind of content that is.";
    if (!UUID.test(draft.subjectXid.trim())) return "That is not a content id.";
    return null;
  }

  if (!draft.targetUserXid.trim()) {
    return "Name the person. An action with nobody named is recorded as done "
      + "and ends no session.";
  }
  if (!UUID.test(draft.targetUserXid.trim())) return "That is not a user id.";
  if (REVOKES_SESSIONS.includes(draft.action) && !draft.acknowledged) {
    return "Confirm that you are ending every session this person has open.";
  }
  return null;
}

/**
 * A `datetime-local` value as an instant the server can store.
 *
 * The input has no timezone, and `expires_at` lands in a `timestamptz` column
 * that the expiry sweeper reads to lift a temporary suspension. Sending the raw
 * string means the database resolves it against the SERVER's timezone, so a
 * suspension set to end at midday in Tashkent ends five hours late in UTC. The
 * browser knows its own offset; this is where it gets applied.
 */
export function expiryIso(local: string): string | null {
  if (!local.trim()) return null;
  const at = new Date(local);
  return Number.isNaN(at.getTime()) ? null : at.toISOString();
}
