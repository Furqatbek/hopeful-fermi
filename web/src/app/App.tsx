/**
 * The console shell: routes, and a navigation that had to stop being a row.
 *
 * Eleven destinations fitted on one line. Thirty-two do not, and a row that
 * wraps to three lines is not a navigation — it is a list the eye gives up on.
 * So the nav is two levels: sections on top, and the pages of the current
 * section beneath. The grouping is by the QUESTION being asked, not by the
 * module the endpoint lives in, because a teacher does not know which router
 * serves attendance:
 *
 *   Content   what we have to give students
 *   Delivery  getting it in front of them
 *   Insight   what happened when we did
 *   Centre    the organization and its money
 *   Platform  things only we operate
 *
 * `Insight` deliberately holds Keys (regrades) beside item analysis: the whole
 * reason to look at item analysis is to find the key that needs fixing, and
 * putting the finding and the remedy in different sections is how the finding
 * stops leading anywhere.
 */

import { useEffect, useState } from "react";
import {
  NavLink,
  Navigate,
  Route,
  BrowserRouter,
  Routes,
  useLocation,
} from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { isSignedIn } from "../api/session";
import { Account } from "../features/account/Account";
import { Invites } from "../features/account/Invites";
import { signOut } from "../features/account/signOut";
import { Attendance } from "../features/analytics/Attendance";
import { CohortProgress } from "../features/analytics/CohortProgress";
import { FlaggedItems } from "../features/analytics/FlaggedItems";
import { ItemAnalysis } from "../features/analytics/ItemAnalysis";
import { Assignments } from "../features/assignments/Assignments";
import { AudioLibrary } from "../features/audio/AudioLibrary";
import { SignIn } from "../features/auth/SignIn";
import { BandMaps } from "../features/bandmaps/BandMaps";
import { Billing } from "../features/billing/Billing";
import { Competitions } from "../features/competitions/Competitions";
import { Composition } from "../features/compose/Composition";
import { CueCards } from "../features/cuecards/CueCards";
import { Exposure } from "../features/governance/Exposure";
import { Sharing } from "../features/governance/Sharing";
import { Takedowns } from "../features/governance/Takedowns";
import { GroupLibrary } from "../features/groups/GroupLibrary";
import { Import } from "../features/imports/Import";
import { Moderation } from "../features/moderation/Moderation";
import { PassageLibrary } from "../features/passages/PassageLibrary";
import { Organizations } from "../features/platform/Organizations";
import { Preview } from "../features/preview/Preview";
import { QuestionLibrary } from "../features/questions/QuestionLibrary";
import { Lexicon } from "../features/registry/Lexicon";
import { QuestionTypes } from "../features/registry/QuestionTypes";
import { Regrades } from "../features/regrade/Regrades";
import { Results } from "../features/results/Results";
import { Roster } from "../features/roster/Roster";
import { Slots } from "../features/speaking/Slots";
import { TestDetail } from "../features/tests/TestDetail";
import { TestLibrary } from "../features/tests/TestLibrary";

type Page = { to: string; label: string };
type Section = { key: string; label: string; pages: Page[] };

const SECTIONS: Section[] = [
  {
    key: "content",
    label: "Content",
    pages: [
      { to: "/tests", label: "Tests" },
      { to: "/passages", label: "Passages" },
      { to: "/questions", label: "Questions" },
      { to: "/groups", label: "Groups" },
      { to: "/audio", label: "Audio" },
      { to: "/cue-cards", label: "Cue cards" },
      { to: "/import", label: "Import" },
    ],
  },
  {
    key: "delivery",
    label: "Delivery",
    pages: [
      { to: "/assignments", label: "Assignments" },
      { to: "/results", label: "Results" },
      { to: "/competitions", label: "Contests" },
      { to: "/speaking", label: "Speaking" },
    ],
  },
  {
    key: "insight",
    label: "Insight",
    pages: [
      { to: "/item-analysis", label: "Items" },
      { to: "/flagged-items", label: "Flagged" },
      { to: "/progress", label: "Progress" },
      { to: "/attendance", label: "Attendance" },
      { to: "/exposure", label: "Exposure" },
      { to: "/regrades", label: "Keys" },
    ],
  },
  {
    key: "centre",
    label: "Centre",
    pages: [
      { to: "/centre", label: "People" },
      { to: "/billing", label: "Billing" },
      { to: "/sharing", label: "Sharing" },
    ],
  },
  {
    key: "platform",
    label: "Platform",
    pages: [
      { to: "/question-types", label: "Types" },
      { to: "/lexicon", label: "Lexicon" },
      { to: "/band-maps", label: "Band maps" },
      { to: "/safety", label: "Safety" },
      { to: "/takedowns", label: "Takedowns" },
      { to: "/organizations", label: "Orgs" },
    ],
  },
];

/** Which section a path belongs to. Longest prefix wins so `/tests/:xid` and
 *  `/versions/:xid` both keep Content open while you are inside a paper. */
function sectionFor(pathname: string): Section | undefined {
  const nested: Record<string, string> = { "/versions": "content" };
  const direct = SECTIONS.find((s) =>
    s.pages.some((p) => pathname === p.to || pathname.startsWith(`${p.to}/`)));
  if (direct) return direct;
  const key = Object.entries(nested).find(([prefix]) =>
    pathname.startsWith(prefix))?.[1];
  return SECTIONS.find((s) => s.key === key);
}

function Navigation({ onSignedOut }: { onSignedOut: () => void }) {
  const queries = useQueryClient();
  const { pathname } = useLocation();
  const active = sectionFor(pathname);
  // Held so a failed revocation is visible rather than swallowed. The local
  // session is cleared either way — see signOut.ts — so this is information,
  // not a retry prompt.
  const [failure, setFailure] = useState<string | null>(null);

  return (
    <>
      <nav className="nav">
        <strong>IELTS Hub</strong>
        {SECTIONS.map((section) => (
          <NavLink
            key={section.key}
            to={section.pages[0]!.to}
            // The function form, and not NavLink's own matching: a section is
            // current when ANY of its pages is, and this link only points at
            // the first. `exactOptionalPropertyTypes` also refuses a bare
            // `undefined` here, which is the compiler making the same point.
            className={() => (active?.key === section.key ? "active" : "")}
          >
            {section.label}
          </NavLink>
        ))}
        <NavLink to="/account">Account</NavLink>
        <button
          className="link"
          onClick={() => {
            void signOut(queries).then((problem) => {
              setFailure(problem);
              onSignedOut();
            });
          }}
        >
          Sign out
        </button>
      </nav>
      {active && (
        <nav className="nav subnav">
          {active.pages.map((page) => (
            <NavLink key={page.to} to={page.to}>{page.label}</NavLink>
          ))}
        </nav>
      )}
      {failure && <p className="page error">{failure}</p>}
    </>
  );
}

export function App() {
  const [signedIn, setSignedIn] = useState(isSignedIn);

  // The API client dispatches this when a refresh fails: the session is over and
  // no retry helps. Listening here rather than importing a router into
  // `client.ts` keeps the API layer testable without a DOM.
  useEffect(() => {
    const ended = () => setSignedIn(false);
    window.addEventListener("ielts:signed-out", ended);
    return () => window.removeEventListener("ielts:signed-out", ended);
  }, []);

  if (!signedIn) return <SignIn onSignedIn={() => setSignedIn(true)} />;

  return (
    <BrowserRouter>
      <Navigation onSignedOut={() => setSignedIn(false)} />
      <Routes>
        <Route path="/tests" element={<TestLibrary />} />
        <Route path="/tests/:xid" element={<TestDetail />} />
        <Route path="/versions/:xid" element={<Composition />} />
        <Route path="/versions/:xid/preview" element={<Preview />} />
        <Route path="/passages" element={<PassageLibrary />} />
        <Route path="/questions" element={<QuestionLibrary />} />
        <Route path="/groups" element={<GroupLibrary />} />
        <Route path="/audio" element={<AudioLibrary />} />
        <Route path="/cue-cards" element={<CueCards />} />
        <Route path="/import" element={<Import />} />

        <Route path="/assignments" element={<Assignments />} />
        <Route path="/results" element={<Results />} />
        <Route path="/competitions" element={<Competitions />} />
        <Route path="/speaking" element={<Slots />} />

        <Route path="/item-analysis" element={<ItemAnalysis />} />
        <Route path="/flagged-items" element={<FlaggedItems />} />
        <Route path="/progress" element={<CohortProgress />} />
        <Route path="/attendance" element={<Attendance />} />
        <Route path="/exposure" element={<Exposure />} />
        <Route path="/regrades" element={<Regrades />} />

        <Route path="/centre" element={<Roster />} />
        <Route path="/billing" element={<Billing />} />
        <Route path="/sharing" element={<Sharing />} />

        <Route path="/question-types" element={<QuestionTypes />} />
        <Route path="/lexicon" element={<Lexicon />} />
        <Route path="/band-maps" element={<BandMaps />} />
        <Route path="/safety" element={<Moderation />} />
        <Route path="/takedowns" element={<Takedowns />} />
        <Route path="/organizations" element={<Organizations />} />

        <Route path="/account" element={<Account />} />
        <Route path="/invites" element={<Invites />} />
        <Route path="*" element={<Navigate to="/tests" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
