import { describe, expect, it } from "vitest";

import { type Issued, MARGIN_MS, isStale, mediaUrl, playbackFailure, remainingMs }
  from "./player";

const issued = (expiresAt: string): Issued => ({
  grant: "v1.eyJ1IjoiYSJ9.sig",
  media_xid: "068e3608-8589-47a8-8a19-0826c8dbec5e",
  expires_at: expiresAt,
});

describe("mediaUrl", () => {
  it("puts the grant in the query string, where a media element can carry it", () => {
    // The whole reason the grant is not a header: `<audio>` cannot set one.
    expect(mediaUrl(issued("2026-08-03T18:36:38Z"))).toBe(
      "/api/v1/media/068e3608-8589-47a8-8a19-0826c8dbec5e/content"
      + "?grant=v1.eyJ1IjoiYSJ9.sig",
    );
  });

  it("encodes a grant that is not URL-safe", () => {
    const token = { ...issued("2026-08-03T18:36:38Z"), grant: "v1.a+b/c=.s?g&h" };
    expect(mediaUrl(token)).toContain("grant=v1.a%2Bb%2Fc%3D.s%3Fg%26h");
  });

  it("is same-origin, so no host and no preflight", () => {
    expect(mediaUrl(issued("2026-08-03T18:36:38Z")).startsWith("/api/v1/")).toBe(true);
  });
});

describe("remainingMs", () => {
  const at = Date.parse("2026-08-03T18:34:38Z");

  it("counts down the lifetime read at receipt", () => {
    const grant = issued("2026-08-03T18:36:38Z");           // 120 s TTL
    expect(remainingMs(grant, at, at)).toBe(120_000);
    expect(remainingMs(grant, at, at + 30_000)).toBe(90_000);
  });

  it("floors at zero rather than going negative", () => {
    const grant = issued("2026-08-03T18:36:38Z");
    expect(remainingMs(grant, at, at + 500_000)).toBe(0);
  });

  it("treats a device clock running fast as no time left", () => {
    // A phone ten minutes ahead reads a negative lifetime. Asking for a fresh
    // grant is the safe way to be wrong; the other direction plays an expired
    // grant and gets silence.
    const grant = issued("2026-08-03T18:36:38Z");
    expect(remainingMs(grant, at + 600_000, at + 600_000)).toBe(0);
  });

  it("is zero for a timestamp it cannot parse", () => {
    expect(remainingMs(issued("not a date"), at, at)).toBe(0);
  });
});

describe("isStale", () => {
  const at = Date.parse("2026-08-03T18:34:38Z");
  const grant = issued("2026-08-03T18:36:38Z");

  it("is false on a grant that has just arrived", () => {
    expect(isStale(grant, at, at)).toBe(false);
  });

  it("turns true a clear margin before the server would refuse", () => {
    // Written as concrete milliseconds rather than in terms of MARGIN_MS: a
    // test that computes the boundary from the constant it is checking passes
    // for every value of that constant, including zero. Sabotaged to confirm.
    //
    // The failure the margin prevents: a play that starts at 119 s and is
    // refused at 121 s, which reaches the user as a player stuck at 0:00.
    expect(isStale(grant, at, at + 109_000)).toBe(false);
    expect(isStale(grant, at, at + 111_000)).toBe(true);
  });

  it("keeps a margin big enough for a slow phone to start playing", () => {
    expect(MARGIN_MS).toBeGreaterThanOrEqual(5_000);
    expect(MARGIN_MS).toBeLessThan(120_000);
  });

  it("is true long after expiry, which is the page-left-open case", () => {
    expect(isStale(grant, at, at + 3_600_000)).toBe(true);
  });
});

describe("playbackFailure", () => {
  it("names expiry first, because that is what a page left open produces", () => {
    expect(playbackFailure(undefined, true)).toMatch(/expired/);
    expect(playbackFailure(2, true)).toMatch(/expired/);
  });

  it("distinguishes a dropped connection from a file that will not decode", () => {
    expect(playbackFailure(2, false)).not.toBe(playbackFailure(3, false));
  });

  it("says something actionable for an unsupported source, which is what a "
     + "refused grant looks like to the element", () => {
    expect(playbackFailure(4, false)).toMatch(/playback link/);
  });

  it("still answers for a code no browser documents", () => {
    expect(playbackFailure(99, false)).toMatch(/could not be played/);
  });
});
