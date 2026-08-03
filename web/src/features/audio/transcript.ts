/**
 * Turning a subtitle file into transcript segments.
 *
 * A centre's listening transcript exists as a subtitle file or as a document —
 * nobody has it as JSON, and nobody is going to type a hundred timed segments
 * into a form. So the editor takes a paste and this parses it.
 *
 * WebVTT and SRT, which between them cover what transcription tools emit. They
 * differ in three ways that matter and in nothing else: SRT numbers its cues,
 * SRT separates timestamps with a comma rather than a full stop, and VTT opens
 * with a `WEBVTT` line. Handling both is a few lines more than handling one.
 *
 * **Timings are the point, not decoration.** `exam.session._excerpt` selects the
 * segments overlapping a question's audio window, so a segment with the wrong
 * span attaches the wrong words to a question in post-exam review — the single
 * most useful support artefact the product produces. A segment whose end is not
 * after its start is refused at upload by the server; refusing it here too means
 * the author is told which LINE is wrong rather than which array index.
 */

export interface Segment {
  start_ms: number;
  end_ms: number;
  text: string;
  speaker?: string;
}

export interface ParseResult {
  segments: Segment[];
  /** Human-readable problems, each naming the line it came from. */
  problems: string[];
}

/** `HH:MM:SS.mmm`, `MM:SS.mmm`, or either with a comma — SRT's separator. */
export function parseTimestamp(raw: string): number | null {
  const match = raw.trim().match(/^(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})$/);
  if (!match) return null;
  const [, hours, minutes, seconds, fraction] = match;
  // Padded, not parsed as-is: `.5` in a subtitle file means 500ms, not 5ms, and
  // reading it as 5 would put a segment half a second off the words it covers.
  const millis = Number((fraction ?? "0").padEnd(3, "0"));
  return (
    Number(hours ?? 0) * 3_600_000 +
    Number(minutes ?? 0) * 60_000 +
    Number(seconds ?? 0) * 1000 +
    millis
  );
}

const CUE = /^(.+?)\s*-->\s*(.+?)(?:\s+.*)?$/;

/**
 * Parse a pasted WebVTT or SRT document.
 *
 * Returns everything it could read AND everything it could not, rather than
 * throwing on the first bad line: a transcript is a hundred cues and an author
 * who has to fix them one upload at a time will give up. This is the same
 * "report every finding at once" the publish gate does.
 */
export function parseSubtitles(raw: string): ParseResult {
  const segments: Segment[] = [];
  const problems: string[] = [];
  const lines = raw.replace(/\r\n?/g, "\n").split("\n");

  let index = 0;
  while (index < lines.length) {
    const line = lines[index] ?? "";
    const cue = line.match(CUE);
    if (!cue) {
      index += 1;
      continue;
    }
    const lineNo = index + 1;
    const start = parseTimestamp(cue[1]!);
    const end = parseTimestamp(cue[2]!);
    index += 1;

    // Text runs until the next blank line. Joined with a space rather than a
    // newline: a cue wrapped across two lines is one sentence, and the review
    // screen shows it inline.
    const body: string[] = [];
    while (index < lines.length && (lines[index] ?? "").trim() !== "") {
      body.push((lines[index] ?? "").trim());
      index += 1;
    }
    const text = body.join(" ").trim();

    if (start === null || end === null) {
      problems.push(`Line ${lineNo}: could not read the timestamps.`);
      continue;
    }
    if (end <= start) {
      // Refused rather than repaired. A reversed span overlaps windows it has
      // nothing to do with, and guessing which bound was meant would attach
      // somebody's words to the wrong question silently.
      problems.push(`Line ${lineNo}: the end is not after the start.`);
      continue;
    }
    if (!text) {
      problems.push(`Line ${lineNo}: the cue has no text.`);
      continue;
    }

    // "NARRATOR: You will hear…" — a speaker label, which the review screen can
    // show. Recognised only as ALL CAPS (`NARRATOR`, `WOMAN 2`) or a single
    // capitalised word (`Man`). Anything looser swallows ordinary prose: a
    // first pass allowed a capitalised phrase and turned "There were three: a
    // bike, a bus and a car" into a speaker called "There were three".
    const speaker = text.match(/^([A-Z][A-Z0-9 ]{0,20}|[A-Z][a-z]{0,19}):\s+(.*)$/s);
    segments.push(
      speaker
        ? { start_ms: start, end_ms: end, speaker: speaker[1]!.trim(), text: speaker[2]!.trim() }
        : { start_ms: start, end_ms: end, text },
    );
  }

  if (segments.length === 0 && problems.length === 0) {
    problems.push(
      "No cues found. Paste a WebVTT or SRT file — lines of the form "
      + "00:00:12.000 --> 00:00:15.500 followed by the words.",
    );
  }
  return { segments, problems };
}

/** `mm:ss` for display. Segment lists are read against an audio player. */
export function formatMs(ms: number): string {
  const total = Math.floor(ms / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/**
 * Segments that overlap each other.
 *
 * Not fatal, and not refused by the server — but worth showing, because the
 * review excerpt is built by OVERLAP against a question's window, so two
 * segments covering the same moment both attach to the same question and the
 * excerpt reads as a stutter.
 */
export function overlaps(segments: Segment[]): number {
  const ordered = [...segments].sort((a, b) => a.start_ms - b.start_ms);
  let count = 0;
  for (let i = 1; i < ordered.length; i += 1) {
    if (ordered[i]!.start_ms < ordered[i - 1]!.end_ms) count += 1;
  }
  return count;
}
