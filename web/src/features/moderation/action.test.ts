import { describe, expect, it } from "vitest";

import { REVOKES_SESSIONS, expiryIso, problemWith } from "./action";
import type { ActionDraft } from "./action";

const USER = "018f2a1c-4c6e-7b3a-9d21-5f0e6a7b8c90";
const CONTENT = "018f2a1c-4c6e-7b3a-9d21-5f0e6a7b8c91";

const draft = (over: Partial<ActionDraft> = {}): ActionDraft => ({
  action: "warn",
  reason: "Sent the same abusive message to four students.",
  targetUserXid: USER,
  subjectType: "",
  subjectXid: "",
  expiresLocal: "",
  acknowledged: false,
  ...over,
});

describe("problemWith", () => {
  it("passes a complete warning", () => {
    expect(problemWith(draft())).toBeNull();
  });

  it("refuses an empty reason", () => {
    // `reason` is `min_length=1` on the server for the same reason the row is
    // immutable: "" is not a reason, and somebody reads this months later.
    expect(problemWith(draft({ reason: "   " }))).toContain("reason");
  });

  it("refuses a user action with nobody named", () => {
    // The server does not. `take_moderation_action` revokes sessions only when a
    // target was found, so a ban with no target answers 201, revokes nothing,
    // and writes an audit row saying the user was banned — after which the queue
    // reads as handled.
    expect(problemWith(draft({ action: "ban", targetUserXid: "", acknowledged: true })))
      .toContain("Name the person");
  });

  it("refuses a user id that is not an id", () => {
    expect(problemWith(draft({ targetUserXid: "Aziza" }))).toContain("not a user id");
  });

  it("makes a suspend and a ban be confirmed", () => {
    for (const action of REVOKES_SESSIONS) {
      expect(problemWith(draft({ action }))).toContain("Confirm");
      expect(problemWith(draft({ action, acknowledged: true }))).toBeNull();
    }
  });

  it("does not make a warning be confirmed", () => {
    // A warning ends no session. Ceremony on every action is how a moderator
    // learns to click through the one that matters.
    expect(problemWith(draft({ action: "warn" }))).toBeNull();
    expect(problemWith(draft({ action: "mute" }))).toBeNull();
  });

  it("makes a content action name its content", () => {
    // Mirrors the server's model validator, which refuses the same thing.
    expect(problemWith(draft({ action: "content_remove", subjectXid: "" })))
      .toContain("Name the content");
    expect(problemWith(draft({ action: "content_remove", subjectXid: CONTENT })))
      .toContain("kind of content");
    expect(problemWith(draft({
      action: "content_remove", subjectXid: CONTENT, subjectType: "passage",
    }))).toBeNull();
  });

  it("does not ask a content action for a user", () => {
    expect(problemWith(draft({
      action: "content_hide", targetUserXid: "", subjectXid: CONTENT,
      subjectType: "question",
    }))).toBeNull();
  });
});

describe("expiryIso", () => {
  it("is null when no expiry was set", () => {
    // Absent, not epoch. A permanent ban with an `expires_at` of 1970 is a ban
    // the sweeper lifts on its next pass.
    expect(expiryIso("")).toBeNull();
    expect(expiryIso("   ")).toBeNull();
  });

  it("carries the browser's offset into the instant", () => {
    // `datetime-local` has no timezone, and `expires_at` is a timestamptz the
    // expiry sweeper reads. Sent raw, the database resolves it against the
    // SERVER's timezone and the suspension ends at the wrong hour.
    const iso = expiryIso("2026-08-10T12:00");
    expect(iso).toMatch(/Z$/);
    expect(iso).toBe(new Date(2026, 7, 10, 12, 0).toISOString());
  });

  it("is null rather than an invalid date", () => {
    expect(expiryIso("tomorrow")).toBeNull();
  });
});
