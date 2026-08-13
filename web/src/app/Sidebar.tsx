/**
 * The persistent sidebar. Every destination visible at once, grouped by job.
 *
 * The rules it follows, which the old horizontal bar broke:
 *
 *   * **Nothing is hidden behind a click.** Recognition, not recall. You should
 *     never have to guess which tab Billing lives under in order to find out.
 *   * **One thing is current, always.** `activeFor` in `nav.ts` keeps the right
 *     item lit on nested routes too, so the interface still says where you are
 *     when you are three levels deep in a version.
 *   * **A teacher does not see six platform-admin screens** they can never open.
 *
 * Collapsible only down to icons? No. Half the point is the words, and a
 * 26-destination product whose sidebar is a column of glyphs is the recall
 * problem again with better graphics. It collapses on narrow viewports into a
 * disclosure, which is a different thing: hidden but one predictable tap away.
 */

import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { isPlatformAdmin, loadPrincipal, type Principal } from "../api/principal";
import { signOut } from "../features/account/signOut";
import { ACCOUNT, activeFor, groupsFor } from "./nav";

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

export function Sidebar({ onSignedOut }: { onSignedOut: () => void }) {
  const queries = useQueryClient();
  const { pathname } = useLocation();
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => { void loadPrincipal().then(setPrincipal); }, []);
  // Close the mobile drawer on navigation, or it covers the page you just asked
  // for.
  useEffect(() => { setOpen(false); }, [pathname]);

  const current = activeFor(pathname);
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
          <span className="side__org">
            {principal?.user.given_name ?? " "}
          </span>
        </div>

        <div className="side__scroll">
          {groups.map((group) => (
            <section key={group.key} className="side__group">
              <h2 className="side__heading">{group.label}</h2>
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
            </section>
          ))}
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
