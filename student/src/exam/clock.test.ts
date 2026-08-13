import { describe, expect, it } from "vitest";

import { format, remaining, sync, urgency } from "./clock";

describe("the countdown is a server delta, not a device clock", () => {
  it("takes its remaining time from the server's own two timestamps", () => {
    // The device clock is irrelevant on purpose: these are both server values.
    const clock = sync(
      { serverNow: "2026-08-13T10:00:00Z", expiresAt: "2026-08-13T11:00:00Z" },
      1000,
    );
    expect(clock.remainingMs).toBe(3_600_000);
  });

  it("is unaffected by a device clock that is wildly wrong", () => {
    // Same server reading, taken at two very different monotonic stamps. The
    // remaining time is identical, because the device is never consulted for
    // anything but elapsed duration.
    const a = sync({ serverNow: "2026-08-13T10:00:00Z", expiresAt: "2026-08-13T10:30:00Z" }, 0);
    const b = sync({ serverNow: "2026-08-13T10:00:00Z", expiresAt: "2026-08-13T10:30:00Z" }, 9e9);
    expect(a.remainingMs).toBe(b.remainingMs);
  });

  it("counts down as monotonic time passes", () => {
    const clock = sync({ serverNow: "2026-08-13T10:00:00Z", expiresAt: "2026-08-13T10:10:00Z" }, 0);
    expect(remaining(clock, 60_000)).toBe(540_000);
  });

  it("never goes negative — an overrun is simply none left", () => {
    const clock = sync({ serverNow: "2026-08-13T10:00:00Z", expiresAt: "2026-08-13T10:00:10Z" }, 0);
    expect(remaining(clock, 999_000)).toBe(0);
  });

  it("clamps an already-expired attempt to zero rather than showing a negative", () => {
    const clock = sync({ serverNow: "2026-08-13T11:00:00Z", expiresAt: "2026-08-13T10:00:00Z" }, 0);
    expect(clock.remainingMs).toBe(0);
  });
});

describe("the display", () => {
  it("renders mm:ss with a padded seconds field", () => {
    expect(format(3_600_000)).toBe("60:00");
    expect(format(65_000)).toBe("1:05");
    expect(format(9_000)).toBe("0:09");
  });

  it("rounds UP, so 0:00 appears exactly when the time is gone", () => {
    // Half a second left must still read 0:01. Flooring would show 0:00 while
    // the student can still type, which reads as the exam stealing a second.
    expect(format(500)).toBe("0:01");
    expect(format(0)).toBe("0:00");
  });
});

describe("the two warning thresholds the real test uses", () => {
  it("is normal above ten minutes", () => {
    expect(urgency(11 * 60_000)).toBe("normal");
  });

  it("warns at ten minutes and again at five", () => {
    expect(urgency(10 * 60_000)).toBe("ten-minutes");
    expect(urgency(6 * 60_000)).toBe("ten-minutes");
    expect(urgency(5 * 60_000)).toBe("five-minutes");
    expect(urgency(30_000)).toBe("five-minutes");
  });

  it("stays at the five-minute warning once time is gone", () => {
    expect(urgency(0)).toBe("five-minutes");
  });
});
