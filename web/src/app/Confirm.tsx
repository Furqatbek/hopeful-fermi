/**
 * The stop before something that cannot be taken back.
 *
 * **This is not a detail view, and the rule it might look like it breaks is a
 * different rule.** §7.1 says a row's detail is an accent panel BELOW the table
 * rather than a route or a modal, and that stands: a modal for reading is a
 * modal you have to dismiss to look at the thing you were comparing it against.
 * This is a modal for DECIDING, which is the one job the pattern is good at —
 * it takes the page away, so the decision is the only thing in front of you.
 *
 * The student app already made this argument for Finish: "distance in the tab
 * order is the fix; the dialog is the belt to that braces", and its dialog names
 * how many questions are still blank. Same shape here, and the console has three
 * actions that deserve it:
 *
 *   * applying a regrade, which rewrites bands on exams that are already sat;
 *   * a moderation action that ends every session a person has open;
 *   * creating a band map with no organization, which every centre inherits.
 *
 * All three had an acknowledgement checkbox, and a checkbox has one weakness
 * this fixes: it gates the button without ever stating the consequence AT the
 * moment of the click. `count` is that sentence — "34 attempts, 12 bands" — and
 * it is the number a teacher has to be willing to defend to that many students.
 *
 * `<dialog>` rather than a div with a high z-index. The element brings focus
 * containment, Escape, the top layer above every stacking context on the page,
 * and inertness of the content behind it — four things a hand-rolled modal gets
 * wrong one at a time, and the last of which is what lets somebody tab into the
 * table underneath and act on it while the question is still open.
 */

import { useEffect, useRef } from "react";

export function Confirm({ open, title, detail, confirmLabel, onConfirm, onCancel,
                          danger = true, busy = false }: {
  open: boolean;
  title: string;
  /** What is about to happen, in numbers where there are numbers. The whole
   *  point of the dialog over a checkbox. */
  detail: React.ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  danger?: boolean;
  busy?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    // Escape closes a `<dialog>` without telling React, which would leave the
    // component believing it is still open and refusing to reopen it.
    const onClose = () => onCancel();
    dialog.addEventListener("close", onClose);
    return () => dialog.removeEventListener("close", onClose);
  }, [onCancel]);

  return (
    <dialog ref={ref} className="confirm" aria-labelledby="confirm-title">
      <h2 id="confirm-title">{title}</h2>
      <div className="confirm__detail">{detail}</div>
      <div className="row confirm__actions">
        {/* Cancel FIRST in the DOM, so it is what a keyboard lands on and what
            Enter takes. The destructive one is never the default. */}
        <button type="button" className="secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="button" className={danger ? "danger" : undefined}
                onClick={onConfirm} disabled={busy}>
          {busy ? "Working…" : confirmLabel}
        </button>
      </div>
    </dialog>
  );
}
