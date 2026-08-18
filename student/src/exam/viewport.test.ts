/**
 * The gate that stops an exam starting on a device that cannot sit it.
 *
 * It was a stylesheet — `.exam { display: none }` below the breakpoint — and
 * CSS hides things rather than stopping them. The runner stayed mounted behind
 * the blank screen: it started the attempt, ran the clock, flushed autosave,
 * and spent the ONE audio play a listening section allows, server-side and
 * unrecoverable. A student who opened a paper on a phone to look at it lost
 * their play and their attempt without seeing a question.
 */

import { describe, expect, it } from "vitest";

import { EXAM_MIN_WIDTH, widen } from "./viewport";

describe("the breakpoint", () => {
  it("is the same one the stylesheet uses", () => {
    // Two numbers that have to agree, in a repository that has found that
    // shape eleven times. 1024 is the figure `0013 §8.1` argues for.
    expect(EXAM_MIN_WIDTH).toBe("(min-width: 1024px)");
  });
});

describe("the gate latches open", () => {
  it("is shut on a narrow screen", () => {
    expect(widen(false, false)).toBe(false);
  });

  it("opens on a wide one", () => {
    expect(widen(false, true)).toBe(true);
  });

  it("STAYS open when the window narrows mid-paper", () => {
    // Unmounting the runner under a student who resized a window, rotated a
    // tablet or opened dev tools would drop the section they are in and
    // re-enter it — and re-entering is not free once the audio grant is spent.
    // The refusal exists to stop an exam STARTING on the wrong device, which is
    // a question asked once.
    expect(widen(true, false)).toBe(true);
  });

  it("opens for someone who maximises a narrow window", () => {
    expect(widen(false, true)).toBe(true);
    // rather than telling them to reload
  });
});
