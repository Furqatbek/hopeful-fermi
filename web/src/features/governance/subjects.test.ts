import { describe, expect, it } from "vitest";

import { SUBJECT_TYPES, subjectLabel } from "./subjects";

describe("SUBJECT_TYPES", () => {
  it("is the server's vocabulary, all seven of it", () => {
    // `_SUBJECT_TABLES` in platform_ops.py. If this fails, either the server
    // grew a subject type or the labels lost one — and a picker missing a type
    // is content that can never be shared.
    expect([...SUBJECT_TYPES].sort()).toEqual([
      "audio_track", "band_map", "cue_card_set", "passage", "question",
      "question_group", "test",
    ]);
  });

  it("names every one of them", () => {
    expect(SUBJECT_TYPES.every((type) => subjectLabel(type) !== type)).toBe(true);
  });
});

describe("subjectLabel", () => {
  it("reads as words, not as a column name", () => {
    expect(subjectLabel("audio_track")).toBe("Audio track");
    expect(subjectLabel("band_map")).toBe("Band map");
  });

  it("shows a type it does not know rather than nothing", () => {
    // `POST /takedowns` is unauthenticated and does not validate `subject_type`,
    // so this is a row that can genuinely arrive. Blanking it would hide a real
    // allegation from the queue.
    expect(subjectLabel("video_lesson")).toBe("video_lesson");
  });

  it("says Unknown when the field is absent", () => {
    expect(subjectLabel(undefined)).toBe("Unknown");
    expect(subjectLabel(null)).toBe("Unknown");
    expect(subjectLabel("")).toBe("Unknown");
  });
});
