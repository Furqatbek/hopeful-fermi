/**
 * Highlights, as character offsets into the passage text.
 *
 * The obvious implementation is `Range.surroundContents` — wrap the live
 * selection in a `<mark>` and let the DOM hold the state. It is rejected for two
 * reasons, and both bite in an exam:
 *
 *   1. It throws outright on a selection that crosses element boundaries, which
 *      is most real selections: a student drags across the end of one paragraph
 *      and into the next and gets nothing.
 *   2. The state lives in the DOM, so it is destroyed the moment React
 *      re-renders the passage — which happens whenever the student changes
 *      question. Losing your highlights by clicking question 5 is not a bug you
 *      want to find during a mock.
 *
 * So highlights are `{start, end}` offsets into the passage's plain text, held
 * in React state, and the passage is RENDERED from them. Re-render is then free
 * and correct, and the whole thing is a pure function of text plus ranges,
 * testable without a DOM.
 *
 * **These never leave the browser.** They are scratch, not answers, and
 * `docs/design/0013` §3.1 says so: recording a student's private thinking to a
 * server would be surveillance with no purpose.
 */

export type Span = { start: number; end: number };

/** Normalised: sorted, non-empty, and with overlaps and touching pairs merged. */
export function add(spans: readonly Span[], span: Span): Span[] {
  const start = Math.min(span.start, span.end);
  const end = Math.max(span.start, span.end);
  if (end <= start) return [...spans];          // an empty selection is a click

  const merged: Span[] = [];
  let pending: Span = { start, end };
  for (const existing of [...spans].sort((a, b) => a.start - b.start)) {
    // `<` not `<=` on the far side: two spans that merely touch are joined, so
    // highlighting a word and then the word after it leaves one span rather
    // than two abutting ones that "clear" would then only half remove.
    if (existing.end < pending.start || existing.start > pending.end) {
      merged.push(existing);
    } else {
      pending = {
        start: Math.min(pending.start, existing.start),
        end: Math.max(pending.end, existing.end),
      };
    }
  }
  merged.push(pending);
  return merged.sort((a, b) => a.start - b.start);
}

/** Remove whichever span contains this offset. The real client clears a whole
 *  highlight rather than splitting it, and so do we. */
export function removeAt(spans: readonly Span[], offset: number): Span[] {
  return spans.filter((s) => offset < s.start || offset >= s.end);
}

export function spanAt(spans: readonly Span[], offset: number): Span | undefined {
  return spans.find((s) => offset >= s.start && offset < s.end);
}

export type Segment = { text: string; highlighted: boolean; start: number };

/**
 * Cut the text into alternating plain and highlighted runs, in order.
 *
 * Every segment carries its own start offset, because the renderer needs it to
 * translate a click back into a text position for "clear".
 */
export function segments(text: string, spans: readonly Span[]): Segment[] {
  const ordered = [...spans].sort((a, b) => a.start - b.start);
  const out: Segment[] = [];
  let cursor = 0;

  for (const span of ordered) {
    const start = Math.max(0, Math.min(span.start, text.length));
    const end = Math.max(0, Math.min(span.end, text.length));
    if (end <= cursor) continue;                       // fully behind us
    if (start > cursor) {
      out.push({ text: text.slice(cursor, start), highlighted: false, start: cursor });
    }
    out.push({ text: text.slice(Math.max(cursor, start), end), highlighted: true,
               start: Math.max(cursor, start) });
    cursor = end;
  }

  if (cursor < text.length) {
    out.push({ text: text.slice(cursor), highlighted: false, start: cursor });
  }
  // One empty segment rather than none, so the renderer always has a node to
  // hang a selection on. An empty passage container has no text node, and a
  // selection inside it cannot be resolved to an offset at all.
  return out.length ? out : [{ text, highlighted: false, start: 0 }];
}

/**
 * Character offset of a DOM position within a container, counting only text.
 *
 * `Selection` gives a node and an offset inside that node; we need an offset
 * into the passage as a whole. Walking the text nodes in order and accumulating
 * is the only reliable way, because the passage is rendered as many spans and
 * the browser will happily anchor a selection in any of them.
 */
export function offsetOf(container: Node, node: Node, offsetInNode: number): number {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  let total = 0;
  let current = walker.nextNode();
  while (current) {
    if (current === node) return total + offsetInNode;
    total += current.textContent?.length ?? 0;
    current = walker.nextNode();
  }
  // The anchor was not a text node inside the container — a selection that
  // started on the element itself. Falling back to the accumulated length is
  // wrong in principle but harmless: it produces an empty span, which `add`
  // discards.
  return total;
}
