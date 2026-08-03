import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, BrowserRouter, Routes } from "react-router-dom";

import { clearSession, isSignedIn } from "../api/session";
import { AudioLibrary } from "../features/audio/AudioLibrary";
import { SignIn } from "../features/auth/SignIn";
import { Composition } from "../features/compose/Composition";
import { PassageLibrary } from "../features/passages/PassageLibrary";
import { QuestionLibrary } from "../features/questions/QuestionLibrary";
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
        <NavLink to="/audio">Audio</NavLink>
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
        <Route path="/audio" element={<AudioLibrary />} />
        <Route path="*" element={<Navigate to="/tests" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
