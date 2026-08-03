import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, BrowserRouter, Routes } from "react-router-dom";

import { clearSession, isSignedIn } from "../api/session";
import { AudioLibrary } from "../features/audio/AudioLibrary";
import { SignIn } from "../features/auth/SignIn";
import { Assignments } from "../features/assignments/Assignments";
import { Composition } from "../features/compose/Composition";
import { GroupLibrary } from "../features/groups/GroupLibrary";
import { PassageLibrary } from "../features/passages/PassageLibrary";
import { QuestionLibrary } from "../features/questions/QuestionLibrary";
import { Regrades } from "../features/regrade/Regrades";
import { Results } from "../features/results/Results";
import { Roster } from "../features/roster/Roster";
import { TestDetail } from "../features/tests/TestDetail";
import { TestLibrary } from "../features/tests/TestLibrary";

export function App() {
  const [signedIn, setSignedIn] = useState(isSignedIn);

  // The API client dispatches this when a refresh fails: the session is over and
  // no retry helps. Listening here rather than importing a router into
  // `client.ts` keeps the API layer testable without a DOM.
  useEffect(() => {
    const signOut = () => setSignedIn(false);
    window.addEventListener("ielts:signed-out", signOut);
    return () => window.removeEventListener("ielts:signed-out", signOut);
  }, []);

  if (!signedIn) return <SignIn onSignedIn={() => setSignedIn(true)} />;

  return (
    <BrowserRouter>
      <nav className="nav">
        <strong>IELTS Hub</strong>
        <NavLink to="/tests">Tests</NavLink>
        <NavLink to="/passages">Passages</NavLink>
        <NavLink to="/questions">Questions</NavLink>
        <NavLink to="/groups">Groups</NavLink>
        <NavLink to="/audio">Audio</NavLink>
        <NavLink to="/assignments">Assignments</NavLink>
        <NavLink to="/results">Results</NavLink>
        <NavLink to="/regrades">Keys</NavLink>
        <NavLink to="/centre">Centre</NavLink>
        <button
          className="link"
          onClick={() => {
            clearSession();
            setSignedIn(false);
          }}
        >
          Sign out
        </button>
      </nav>
      <Routes>
        <Route path="/tests" element={<TestLibrary />} />
        <Route path="/tests/:xid" element={<TestDetail />} />
        <Route path="/versions/:xid" element={<Composition />} />
        <Route path="/passages" element={<PassageLibrary />} />
        <Route path="/questions" element={<QuestionLibrary />} />
        <Route path="/groups" element={<GroupLibrary />} />
        <Route path="/audio" element={<AudioLibrary />} />
        <Route path="/assignments" element={<Assignments />} />
        <Route path="/results" element={<Results />} />
        <Route path="/regrades" element={<Regrades />} />
        <Route path="/centre" element={<Roster />} />
        <Route path="*" element={<Navigate to="/tests" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
