/**
 * The persistent sidebar: one group open at a time, animated.
 *
 * ── the accordion, and the two things that make it safe ─────────────────────
 *
 * Showing one group at a time is in tension with why this sidebar exists. The
 * horizontal nav it replaced was bad precisely because it hid four fifths of
 * itself, and an accordion hides four fifths of itself too. Two rules pay that
 * back, and neither is optional:
 *
 *   1. **The group holding your current page opens by itself**, on load and on
 *      every navigation. Land on Billing and My centre is already open. Without
 *      this you would arrive somewhere and be shown a different part of the
 *      product, which is worse than the horizontal bar ever was.
 *   2. **A closed group says when your page is inside it** — a small dot on its
 *      header. You can browse Library while working in Billing and still see
 *      where you actually are.
 *
 * ── the animation ──────────────────────────────────────────────────────────
 *
 * `grid-template-rows: 0fr -> 1fr`, not `max-height`. Height cannot be animated
 * to `auto`, and the usual workaround — a `max-height` guessed high enough —
 * has two visible faults: the easing applies to the guess rather than the
 * content, so short groups snap and long ones drag, and any group taller than
 * the guess is silently clipped. The `fr` technique animates to the content's
 * real height, so Teaching (four items) and Library (seven) take the same time
 * and neither is cut off.
 */

import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { isPlatformAdmin, loadPrincipal, type Principal } from "../api/principal";
import { signOut } from "../features/account/signOut";
import { ACCOUNT, activeFor, groupKeyFor, groupsFor } from "./nav";

function Item({ to, label, hint, current }: {
  to: string; label: string; hint?: string; current: boolean;
}) {
  return (
    <li>
      <NavLink
        to={to}
        // The function form rather than NavLink's own matching, because a parent
        // stays current on its detail routes and NavLink cannot know that rule.
        className={() => `side__link${current ? " side__link--current" : ""}`}
        aria-current={current ? "page" : undefined}
      >
        <span className="side__label">{label}</span>
        {hint && <span className="side__hint">{hint}</span>}
      </NavLink>
    </li>
  );
}

/** A caret that turns. Inline rather than a font glyph so it is the same shape
 *  on every platform and inherits the text colour. */
function Caret() {
  return (
    <svg className="side__caret" viewBox="0 0 12 12" aria-hidden="true" width="12" height="12">
      <path d="M4.5 2.5 L8 6 L4.5 9.5" fill="none" stroke="currentColor"
            strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Sidebar({ onSignedOut }: { onSignedOut: () => void }) {
  const queries = useQueryClient();
  const { pathname } = useLocation();
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const current = activeFor(pathname);
  const activeGroup = groupKeyFor(pathname);
  const [openKey, setOpenKey] = useState<string | undefined>(activeGroup);

  useEffect(() => { void loadPrincipal().then(setPrincipal); }, []);
  // Close the mobile drawer on navigation, or it covers the page just asked for.
  useEffect(() => { setOpen(false); }, [pathname]);
  // Rule 1: follow the page. Navigating into a group opens it — including a
  // navigation this sidebar did not start, like a link inside a screen or a
  // pasted URL.
  useEffect(() => { if (activeGroup) setOpenKey(activeGroup); }, [activeGroup]);

  const groups = groupsFor(isPlatformAdmin(principal));

  return (
    <>
      <button
        type="button"
        className="side__toggle"
        aria-expanded={open}
        aria-controls="sidebar"
        onClick={() => setOpen((o) => !o)}
      >
        {open ? "Close" : "Menu"}
      </button>

      <nav
        id="sidebar"
        className={`side${open ? " side--open" : ""}`}
        aria-label="Sections"
      >
        <div className="side__brand">
          <strong>IELTS Hub</strong>
          <span className="side__org">{principal?.user.given_name ?? " "}</span>
        </div>

        <div className="side__scroll">
          {groups.map((group) => {
            const isOpen = openKey === group.key;
            // Rule 2: a closed group still says your page is inside it.
            const holdsCurrent = group.items.some((item) => item.to === current);
            const panelId = `side-panel-${group.key}`;

            return (
              <section key={group.key} className="side__group">
                <h2 className="side__heading">
                  <button
                    type="button"
                    className="side__disclose"
                    aria-expanded={isOpen}
                    aria-controls={panelId}
                    // Clicking the open one closes it, which is what a
                    // disclosure does. Opening any other closes this one —
                    // that is the "never two at once" rule, and it lives here
                    // rather than in an effect so the state is never briefly
                    // wrong.
                    onClick={() => setOpenKey(isOpen ? undefined : group.key)}
                  >
                    <Caret />
                    <span>{group.label}</span>
                    {holdsCurrent && !isOpen && (
                      <span className="side__dot" aria-label="contains the current page" />
                    )}
                  </button>
                </h2>

                <div
                  id={panelId}
                  className={`side__panel${isOpen ? " side__panel--open" : ""}`}
                  // Hidden from the accessibility tree and from tab order when
                  // closed. `overflow: hidden` alone would leave the links
                  // focusable and a keyboard user would tab into a group they
                  // cannot see.
                  {...(isOpen ? {} : { inert: true })}
                >
                  <ul className="side__list">
                    {group.items.map((item) => (
                      <Item
                        key={item.to}
                        to={item.to}
                        label={item.label}
                        {...(item.hint ? { hint: item.hint } : {})}
                        current={current === item.to}
                      />
                    ))}
                  </ul>
                </div>
              </section>
            );
          })}
        </div>

        <div className="side__foot">
          <ul className="side__list">
            <Item to={ACCOUNT.to} label={ACCOUNT.label} current={current === ACCOUNT.to} />
          </ul>
          <button
            type="button"
            className="side__signout"
            onClick={() => {
              void signOut(queries).then((problem) => {
                setFailure(problem);
                onSignedOut();
              });
            }}
          >
            Sign out
          </button>
          {/* Held rather than swallowed: the local session is cleared either
              way (see signOut.ts), so this is information, not a retry prompt. */}
          {failure && <p className="side__error">{failure}</p>}
        </div>
      </nav>
    </>
  );
}
