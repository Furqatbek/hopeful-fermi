/**
 * The console shell: a sidebar and the routes it points at.
 *
 * The navigation itself lives in `nav.ts` (the model) and `Sidebar.tsx` (the
 * rendering), and the argument for its shape is written there. In short: this
 * file used to carry a two-row horizontal bar hiding twenty-six pages behind
 * five tabs, which made finding anything a matter of remembering where it was
 * filed. It is a persistent sidebar now.
 */

import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { isSignedIn } from "../api/session";
import { Sidebar } from "./Sidebar";
import { Account } from "../features/account/Account";
import { Invites } from "../features/account/Invites";
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
      <div className="shell">
      <Sidebar onSignedOut={() => setSignedIn(false)} />
      <main className="shell__main">
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
      </main>
      </div>
    </BrowserRouter>
  );
}
