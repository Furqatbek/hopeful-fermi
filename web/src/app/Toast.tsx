/**
 * A message for an action that removed the place its message would have gone.
 *
 * This console reports results INLINE and keeps them on screen — `.issued` for
 * a success, `.error` for a refusal — and that is the right default: a
 * confirmation you can still read ten seconds later is better than one that
 * faded while you were looking at the table. So a toast is not a general
 * notification channel here, and adding one would have been the Bento grid
 * again: a primitive no screen needs.
 *
 * There is one case the inline pattern genuinely cannot serve, and it is what
 * this exists for. **An action that deletes the row it was invoked from has
 * nowhere left to say so.** `ArchiveButton` retires a passage, the mutation
 * invalidates the listing, the row disappears — and the only evidence the click
 * did anything is an absence. Four controls are in that position, and the
 * server is what decides it rather than the screen: the listings behind them
 * filter the row out (`WHERE g.revoked_at IS NULL` for a content grant,
 * `status = 'active'` for a roster) so the row is gone by the next refetch.
 *
 * **Success only, and that is a rule rather than an omission.** Revoking an
 * entitlement looks like the same shape and is not one: that listing keeps
 * revoked rows, greyed, with the reason beside them, so it already says what
 * happened in the place you were looking. And no FAILURE belongs here either —
 * every refusal in this console leaves its row exactly where it was, so the
 * message has somewhere to live, and a message about a write that did not
 * happen must not be on a timer. There is no error tone below because there is
 * nothing that would use one.
 *
 * Two rules the pattern usually gets wrong, and this does not:
 *
 *   * **It never carries the only copy of anything.** No undo, no "click here
 *     to retry" — a control that vanishes on a timer is a control a person can
 *     miss. Retiring is reversible from the listing itself; that is where the
 *     way back lives.
 *   * **`role="status"`, not `alert`.** A polite announcement is read at the
 *     next pause; an assertive one interrupts whatever a screen reader was in
 *     the middle of saying, which for a confirmation is rude.
 */

import { createContext, useCallback, useContext, useEffect, useState } from "react";

type Note = { id: number; text: string };

/** How long a confirmation stays. Long enough to look up from the table you
 *  were reading, short enough not to stack while somebody archives six items. */
export const TOAST_MS = 6000;

const Ctx = createContext<((text: string) => void) | null>(null);

/**
 * `say("Retired 'The Dead Sea'.")` from anywhere under the provider.
 *
 * Returns a no-op outside one rather than throwing. A component rendered in a
 * test without the provider should not fail on a confirmation message — the
 * message is the least important thing that component does.
 */
export function useToast(): (text: string) => void {
  return useContext(Ctx) ?? noop;
}

/** Module-level, so `useToast()` returns the SAME function on every render
 *  outside a provider — a fresh closure each time would change the identity of
 *  anything that lists it as a dependency, on every render. */
const noop = () => {};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [notes, setNotes] = useState<Note[]>([]);

  const dismiss = useCallback((id: number) => {
    setNotes((held) => held.filter((n) => n.id !== id));
  }, []);

  // The id comes off the previous state rather than a ref, because two calls in
  // one batch both read a ref's current value before either writes it — and two
  // notes sharing a key is a React warning and a stale row.
  const say = useCallback((text: string) => {
    setNotes((held) => [...held, { id: (held.at(-1)?.id ?? 0) + 1, text }]);
  }, []);

  return (
    <Ctx.Provider value={say}>
      {children}
      {/* `aria-live` on the CONTAINER, which has to exist before the message
          does — a live region added to the DOM already populated is announced
          by nothing, which is the single most common way this pattern silently
          fails for a screen reader. */}
      <div className="toasts" aria-live="polite" aria-atomic="false">
        {notes.map((note) => (
          <Toast key={note.id} id={note.id} text={note.text} onDismiss={dismiss} />
        ))}
      </div>
    </Ctx.Provider>
  );
}

function Toast({ id, text, onDismiss }: {
  id: number;
  text: string;
  onDismiss: (id: number) => void;
}) {
  useEffect(() => {
    const timer = setTimeout(() => onDismiss(id), TOAST_MS);
    return () => clearTimeout(timer);
  }, [id, onDismiss]);

  return (
    <div className="toast" role="status">
      <span>{text}</span>
      {/* A way out before the timer, for anyone who reads it at once and wants
          the corner of the screen back. Labelled, because "Dismiss" next to
          six stacked messages does not say which one. */}
      <button type="button" className="link toast__close"
              onClick={() => onDismiss(id)}
              aria-label={`Dismiss: ${text}`}>
        Dismiss
      </button>
    </div>
  );
}
