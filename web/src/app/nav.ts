/**
 * The console's information architecture, as data.
 *
 * ── what was wrong with the old one ──────────────────────────────────────────
 *
 * Twenty-six pages behind five tabs in a two-row horizontal bar, with only one
 * tab's contents visible at a time. Three separate failures, and they compound:
 *
 *   1. **You had to already know where a thing lived.** Finding Billing meant
 *      guessing it was under "Centre" and clicking there to look. Navigation
 *      that requires recall instead of recognition is the oldest usability
 *      mistake there is, and a horizontal bar that hides four fifths of itself
 *      forces it by construction.
 *   2. **The group names were the architect's, not the teacher's.** "Content",
 *      "Delivery", "Insight", "Centre", "Platform". Nobody arrives at work
 *      wanting to do some Insight.
 *   3. **The labels were truncated past the point of meaning** — "Types",
 *      "Keys", "Items", "Orgs" — because a horizontal bar has no room, which is
 *      the layout choosing the words.
 *
 * ── what this is ────────────────────────────────────────────────────────────
 *
 * A persistent sidebar showing every destination at once, grouped by the job
 * somebody came to do, with labels long enough to mean something.
 *
 * **The four skills are the entry points to the library**, which is what the
 * owner asked for and where they are actually true. A skill is a property of the
 * material you author — a passage is Reading, an audio track is Listening, a cue
 * card is Speaking — so those three make excellent front doors to the content.
 * They are NOT workspaces: an assignment, a result, an invoice and a takedown
 * belong to no skill at all, and filing twenty screens under "Settings" to force
 * a four-way split would have traded one bad nav for a worse one.
 *
 * **Writing is deliberately absent.** There are no Writing screens and no
 * marking engine (ADR-0001 Assumption 5: "Reading and Listening only at MVP.
 * Writing/Speaking scoring is modelled in the schema but has no engine"). A nav
 * entry leading to an empty page is a promise the product does not keep, so it
 * appears when the screens do.
 *
 * Kept as data, and pure, so `activeFor` can be tested without a DOM — the
 * highlighting rule has to survive nested routes like `/versions/:xid/preview`,
 * and that is exactly the sort of thing that silently stops working.
 */

export type NavItem = {
  to: string;
  label: string;
  /** Shown small beside the label. Says what the section actually holds. */
  hint?: string;
};

export type NavGroup = {
  key: string;
  label: string;
  items: NavItem[];
  /** Platform-admin only. Hidden entirely from a centre teacher. */
  platformOnly?: boolean;
};

export const NAV: NavGroup[] = [
  {
    key: "library",
    label: "Library",
    items: [
      // The three live skills, first, as the front door to authoring.
      { to: "/passages", label: "Reading", hint: "passages" },
      { to: "/audio", label: "Listening", hint: "audio" },
      { to: "/cue-cards", label: "Speaking", hint: "cue cards" },
      // Writing goes here when it has screens. See the header.
      { to: "/tests", label: "Tests", hint: "papers" },
      { to: "/questions", label: "Questions" },
      { to: "/groups", label: "Question groups" },
      { to: "/import", label: "Import" },
    ],
  },
  {
    key: "teaching",
    label: "Teaching",
    items: [
      { to: "/assignments", label: "Assignments" },
      { to: "/results", label: "Results" },
      { to: "/competitions", label: "Contests" },
      { to: "/speaking", label: "Speaking slots" },
    ],
  },
  {
    key: "reports",
    label: "Reports",
    items: [
      { to: "/progress", label: "Student progress" },
      { to: "/item-analysis", label: "Item analysis" },
      { to: "/flagged-items", label: "Flagged items" },
      { to: "/attendance", label: "Attendance" },
      { to: "/regrades", label: "Answer keys", hint: "regrades" },
      { to: "/exposure", label: "Content exposure" },
    ],
  },
  {
    key: "centre",
    label: "My centre",
    items: [
      { to: "/centre", label: "People" },
      { to: "/billing", label: "Billing" },
      { to: "/sharing", label: "Sharing" },
    ],
  },
  {
    key: "platform",
    label: "Platform",
    // Six items a centre teacher can never use. The server refuses them anyway,
    // so hiding them removes nothing but noise — and noise is the complaint this
    // rewrite exists to answer.
    platformOnly: true,
    items: [
      { to: "/question-types", label: "Question types" },
      { to: "/lexicon", label: "Lexicon" },
      { to: "/band-maps", label: "Band maps" },
      { to: "/safety", label: "Safety queue" },
      { to: "/takedowns", label: "Takedowns" },
      { to: "/organizations", label: "Organizations" },
    ],
  },
];

/** Bottom of the sidebar, below the rule. */
export const ACCOUNT: NavItem = { to: "/account", label: "Account" };

/**
 * Routes with no nav entry of their own, and which item should light up while
 * you are on them.
 *
 * Without this a teacher who opens a test drops into `/versions/{xid}` and the
 * whole sidebar goes dim — the interface stops telling them where they are at
 * exactly the moment they have navigated deepest into it.
 */
const NESTED: [prefix: string, itemPath: string][] = [
  ["/versions", "/tests"],   // composing and previewing a version
  ["/invites", "/account"],  // an invitation is an account action
];

/** The nav item that should be marked current for a pathname, if any. */
export function activeFor(pathname: string): string | undefined {
  const all = [...NAV.flatMap((group) => group.items), ACCOUNT];
  // Longest match first, so `/tests/{xid}` prefers `/tests` over any shorter
  // path that happens to be a prefix of it.
  const direct = [...all]
    .sort((a, b) => b.to.length - a.to.length)
    .find((item) => pathname === item.to || pathname.startsWith(`${item.to}/`));
  if (direct) return direct.to;

  const nested = NESTED.find(([prefix]) =>
    pathname === prefix || pathname.startsWith(`${prefix}/`));
  return nested?.[1];
}

/** The groups to render for this principal. */
export function groupsFor(isPlatformAdmin: boolean): NavGroup[] {
  return NAV.filter((group) => !group.platformOnly || isPlatformAdmin);
}
