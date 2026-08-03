import { describe, expect, it } from "vitest";

import { formatMs, overlaps, parseSubtitles, parseTimestamp } from "./transcript";

const VTT = `WEBVTT

1
00:00:00.000 --> 00:00:04.500
Good morning. You will hear a conversation

2
00:00:04.500 --> 00:00:09.000
NARRATOR: between a student and a receptionist.
`;

const SRT = `1
00:00:00,000 --> 00:00:04,500
Good morning.

2
00:00:04,500 --> 00:00:09,000
I came by bicycle.
`;

describe("parseTimestamp", () => {
  it("reads hours, minutes, seconds and millis", () => {
    expect(parseTimestamp("01:02:03.004")).toBe(3_723_004);
  });

  it("reads the SRT comma separator", () => {
    expect(parseTimestamp("00:00:04,500")).toBe(4500);
  });

  it("allows the hour to be omitted", () => {
    expect(parseTimestamp("02:03.500")).toBe(123_500);
  });

  it("pads a short fraction rather than reading it as milliseconds", () => {
    // `.5` is half a second. Reading it as 5ms would put every segment in a
    // file written that way just off the words it covers.
    expect(parseTimestamp("00:00:01.5")).toBe(1500);
    expect(parseTimestamp("00:00:01.50")).toBe(1500);
  });

  it("rejects what is not a timestamp", () => {
    expect(parseTimestamp("soon")).toBeNull();
    expect(parseTimestamp("")).toBeNull();
  });
});

describe("parseSubtitles", () => {
  it("reads WebVTT", () => {
    const { segments, problems } = parseSubtitles(VTT);
    expect(problems).toEqual([]);
    expect(segments).toHaveLength(2);
    expect(segments[0]).toEqual({
      start_ms: 0, end_ms: 4500,
      text: "Good morning. You will hear a conversation",
    });
  });

  it("reads SRT", () => {
    const { segments, problems } = parseSubtitles(SRT);
    expect(problems).toEqual([]);
    expect(segments.map((s) => s.text)).toEqual(["Good morning.", "I came by bicycle."]);
  });

  it("lifts a speaker label out of the text", () => {
    const { segments } = parseSubtitles(VTT);
    expect(segments[1]).toMatchObject({
      speaker: "NARRATOR",
      text: "between a student and a receptionist.",
    });
  });

  it("does not mistake a sentence containing a colon for a speaker", () => {
    const { segments } = parseSubtitles(
      "00:00:01.000 --> 00:00:02.000\nThere were three: a bike, a bus and a car.\n",
    );
    expect(segments[0]!.speaker).toBeUndefined();
    expect(segments[0]!.text).toBe("There were three: a bike, a bus and a car.");
  });

  it("joins a cue wrapped across lines into one sentence", () => {
    const { segments } = parseSubtitles(
      "00:00:01.000 --> 00:00:03.000\nI came\nby bicycle.\n",
    );
    expect(segments[0]!.text).toBe("I came by bicycle.");
  });

  it("reports every bad cue rather than stopping at the first", () => {
    // The publish gate's rule: an author fixing a hundred-cue file one upload at
    // a time gives up.
    const { segments, problems } = parseSubtitles(
      "00:00:05.000 --> 00:00:01.000\nreversed\n\n"
      + "bad --> worse\nunreadable\n\n"
      + "00:00:10.000 --> 00:00:12.000\n\n"
      + "00:00:20.000 --> 00:00:22.000\ngood\n",
    );
    expect(problems).toHaveLength(3);
    expect(problems[0]).toContain("end is not after the start");
    expect(problems[1]).toContain("could not read the timestamps");
    expect(problems[2]).toContain("no text");
    // And the good one still comes through.
    expect(segments.map((s) => s.text)).toEqual(["good"]);
  });

  it("refuses a reversed span rather than repairing it", () => {
    // Guessing which bound was meant would attach somebody's words to the wrong
    // question, silently.
    const { segments } = parseSubtitles("00:00:05.000 --> 00:00:01.000\nwords\n");
    expect(segments).toEqual([]);
  });

  it("says what it wanted when given something that is not subtitles", () => {
    const { segments, problems } = parseSubtitles("just some prose about a bicycle");
    expect(segments).toEqual([]);
    expect(problems[0]).toContain("WebVTT or SRT");
  });

  it("is empty for empty input, with a problem rather than silence", () => {
    expect(parseSubtitles("").problems).toHaveLength(1);
  });
});

describe("overlaps", () => {
  it("counts segments covering the same moment", () => {
    // Both attach to a question whose window they cross, and the excerpt reads
    // as a stutter.
    expect(overlaps([
      { start_ms: 0, end_ms: 5000, text: "a" },
      { start_ms: 4000, end_ms: 9000, text: "b" },
    ])).toBe(1);
  });

  it("is zero when they only touch", () => {
    expect(overlaps([
      { start_ms: 0, end_ms: 4500, text: "a" },
      { start_ms: 4500, end_ms: 9000, text: "b" },
    ])).toBe(0);
  });

  it("does not depend on the order they were listed in", () => {
    expect(overlaps([
      { start_ms: 4000, end_ms: 9000, text: "b" },
      { start_ms: 0, end_ms: 5000, text: "a" },
    ])).toBe(1);
  });
});

describe("formatMs", () => {
  it("reads against a player's clock", () => {
    expect(formatMs(0)).toBe("0:00");
    expect(formatMs(65_000)).toBe("1:05");
    expect(formatMs(3_601_000)).toBe("60:01");
  });
});
