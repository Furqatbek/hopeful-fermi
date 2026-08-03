/**
 * A cue-card set is the three parts of an IELTS speaking test, and the only
 * thing that makes it useful is that it has prompts in it.
 *
 * `CueCardSetCreate` on the server declares `body` required and then gives all
 * three parts empty defaults, so `{"title": "x", "body": {}}` is accepted with
 * 201 — measured. What comes out is a set with a title, a version xid, and no
 * prompts, which a teacher can attach to a speaking slot and discover is a room
 * with nothing to talk about fifteen minutes into a lesson.
 *
 * There is also no way to look one up again. The API has `GET /cue-card-sets`,
 * which returns title, tags, visibility and the current version's xid, and no
 * endpoint that returns a version's BODY. So what is typed here is stored and
 * never readable through the console — which makes checking it before it is sent
 * the only check there will be.
 */

export interface CueCardBody {
  part1: string[];
  part2?: { topic: string; bullets: string[] };
  part3: string[];
}

/** One prompt per line. The same rule `PassageLibrary.toBlocks` uses for
 *  paragraphs, for the same reason: a plain textarea that round-trips exactly
 *  beats an editor that quietly reformats. */
export function lines(text: string): string[] {
  return text.split("\n").map((line) => line.trim()).filter(Boolean);
}

export function toBody(part1: string, topic: string, bullets: string,
                       part3: string): CueCardBody {
  const body: CueCardBody = { part1: lines(part1), part3: lines(part3) };
  // Omitted entirely rather than sent as an empty object: `part2.topic` is
  // required by the request model, so a part 2 with a blank topic is a 422 on
  // a set the author believes is fine.
  if (topic.trim()) body.part2 = { topic: topic.trim(), bullets: lines(bullets) };
  return body;
}

export function promptCount(body: CueCardBody): number {
  return body.part1.length + (body.part2 ? 1 : 0) + body.part3.length;
}

/**
 * Refusals. The first is the one the server does not make.
 *
 * The rest are shape: a part 2 is a topic AND its bullets — the card a candidate
 * reads for one minute and then speaks to for two — and bullets with no topic is
 * a card with no task on it.
 */
export function promptProblems(title: string, body: CueCardBody,
                               bulletsText: string): string[] {
  const problems: string[] = [];
  if (!title.trim()) {
    problems.push("Give the set a title. The library lists by title and nothing else.");
  }
  if (promptCount(body) === 0) {
    problems.push(
      "There are no prompts. A slot using this set would open with nothing to " +
      "talk about, and the set cannot be read back to fix it later.",
    );
  }
  if (!body.part2 && lines(bulletsText).length > 0) {
    problems.push("The part 2 bullets have no topic above them.");
  }
  return problems;
}
