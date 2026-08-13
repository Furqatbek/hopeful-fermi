/**
 * The Reading section: passage LEFT, questions RIGHT, a divider you can drag.
 *
 * All three are verified against the real computer-delivered client — see
 * `docs/design/0013-student-exam-ui.md` §3. The layout is not a preference: a
 * student who has drilled on passage-left/questions-right reads the screen
 * without thinking about it, and that is the whole point of an exam-realistic
 * mock.
 *
 * ── the context menu is the part to get right ────────────────────────────────
 *
 * The official instruction, verbatim: "left click and drag your cursor over the
 * section of text you wish to highlight, then right click and select the
 * 'highlight' option. To remove a highlight, simply right click on the
 * highlighted area and select 'clear all'."
 *
 * So highlighting is a RIGHT-CLICK menu, not a toolbar button and not a floating
 * bubble on selection. That matters more than it looks: right-click is the
 * muscle memory the real test builds, and a product that puts a highlighter in a
 * toolbar teaches a reflex that costs seconds on the day, hunting for a control
 * that is not there.
 *
 * "Add note" is included because the real client has a note-taking function, but
 * its exact interaction is marked [unconfirmed] in §3.1 — settle it against the
 * official familiarisation test before calling this faithful.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { add, offsetOf, removeAt, segments, spanAt, type Span } from "./highlights";

type Menu = { x: number; y: number; offset: number; hasSelection: boolean };

export function Reading({
  title, passage, children,
}: {
  title: string;
  /** Plain text. Offsets in `highlights` index into exactly this string. */
  passage: string;
  /** The question pane. Rendered on the right. */
  children: React.ReactNode;
}) {
  const [spans, setSpans] = useState<Span[]>([]);
  const [menu, setMenu] = useState<Menu | null>(null);
  // Starts at an even split, which is what the real client opens with. The
  // student moves it and it stays where they put it for the rest of the section.
  const [split, setSplit] = useState("1fr");
  const passageRef = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);

  // ── the divider ──────────────────────────────────────────────────────────
  const startDrag = useCallback((event: React.PointerEvent) => {
    event.preventDefault();
    const grid = gridRef.current;
    if (!grid) return;

    const move = (e: PointerEvent) => {
      const box = grid.getBoundingClientRect();
      const x = e.clientX - box.left;
      // Clamped well away from both edges. A divider that can be dragged to zero
      // hides one pane with no obvious way back, and rediscovering it costs exam
      // minutes.
      const min = box.width * 0.2;
      const max = box.width * 0.8;
      setSplit(`${Math.min(max, Math.max(min, x))}px`);
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  }, []);

  // Keyboard-resizable too. A candidate whose mouse fails must still be able to
  // rebalance the panes (§5).
  const nudge = useCallback((event: React.KeyboardEvent) => {
    const grid = gridRef.current;
    if (!grid) return;
    const delta = event.key === "ArrowLeft" ? -40 : event.key === "ArrowRight" ? 40 : 0;
    if (!delta) return;
    event.preventDefault();
    const box = grid.getBoundingClientRect();
    const current = passageRef.current?.getBoundingClientRect().width ?? box.width / 2;
    const next = Math.min(box.width * 0.8, Math.max(box.width * 0.2, current + delta));
    setSplit(`${next}px`);
  }, []);

  // ── the context menu ─────────────────────────────────────────────────────
  function openMenu(event: React.MouseEvent) {
    const container = passageRef.current;
    if (!container) return;
    event.preventDefault();          // replace the browser menu with ours

    const selection = window.getSelection();
    const hasSelection = !!selection && !selection.isCollapsed
      && container.contains(selection.anchorNode);

    // Where in the text did they click? Needed so "clear all" knows which
    // highlight is under the cursor.
    //
    // Two APIs for one job, because neither is universal: `caretPositionFromPoint`
    // is the standard and shipped in Firefox for years, and Chrome only added it
    // recently — WebKit and older Android browsers have the non-standard
    // `caretRangeFromPoint` instead. A student on a three-year-old handset is
    // exactly the person this product is for, so both are tried.
    let offset = 0;
    const document_ = document as Document & {
      caretRangeFromPoint?: (x: number, y: number) => globalThis.Range | null;
    };
    const caret = document_.caretPositionFromPoint?.(event.clientX, event.clientY);
    const legacy = caret ? null : document_.caretRangeFromPoint?.(event.clientX, event.clientY);
    if (caret) offset = offsetOf(container, caret.offsetNode, caret.offset);
    else if (legacy) offset = offsetOf(container, legacy.startContainer, legacy.startOffset);
    else if (selection?.anchorNode) {
      offset = offsetOf(container, selection.anchorNode, selection.anchorOffset);
    }

    setMenu({ x: event.clientX, y: event.clientY, offset, hasSelection });
  }

  function highlight() {
    const container = passageRef.current;
    const selection = window.getSelection();
    if (!container || !selection || selection.rangeCount === 0) return setMenu(null);
    const range = selection.getRangeAt(0);
    const start = offsetOf(container, range.startContainer, range.startOffset);
    const end = offsetOf(container, range.endContainer, range.endOffset);
    setSpans((current) => add(current, { start, end }));
    selection.removeAllRanges();
    setMenu(null);
  }

  function clearAll() {
    if (menu) setSpans((current) => removeAt(current, menu.offset));
    setMenu(null);
  }

  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(null); };
    window.addEventListener("click", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  const overHighlight = menu ? !!spanAt(spans, menu.offset) : false;

  // The split rides on a CSS custom property, so the grid template stays in the
  // stylesheet with the rest of the layout rather than moving into JS.
  // `CSSProperties` has no index signature for custom properties, hence the cast.
  const splitStyle = { "--split": split } as React.CSSProperties;

  return (
    <div className="reading" ref={gridRef} style={splitStyle}>
      <div className="reading__passage" ref={passageRef} onContextMenu={openMenu}>
        <h2>{title}</h2>
        <p className="reading__hint">
          Select text, then right-click to highlight.
        </p>
        <div className="reading__text">
          {segments(passage, spans).map((segment) =>
            segment.highlighted
              ? <mark key={segment.start}>{segment.text}</mark>
              : <span key={segment.start}>{segment.text}</span>,
          )}
        </div>
      </div>

      <div
        className="reading__divider"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the passage"
        tabIndex={0}
        onPointerDown={startDrag}
        onKeyDown={nudge}
      />

      <div className="reading__questions">{children}</div>

      {menu && (
        <ul
          className="reading__menu"
          style={{ top: menu.y, left: menu.x }}
          onClick={(e) => e.stopPropagation()}
        >
          <li>
            <button type="button" onClick={highlight} disabled={!menu.hasSelection}>
              Highlight
            </button>
          </li>
          <li>
            <button type="button" onClick={clearAll} disabled={!overHighlight}>
              Clear all
            </button>
          </li>
          <li>
            {/* The real client has a note function; its exact interaction is
                [unconfirmed] in §3.1. Present and disabled is more honest than
                absent — it says the gap is known rather than forgotten. */}
            <button type="button" disabled title="Not built yet — see design 0013 §3.1">
              Add note
            </button>
          </li>
        </ul>
      )}
    </div>
  );
}
