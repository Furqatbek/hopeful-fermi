/**
 * The student app shell.
 *
 * Two surfaces that deliberately do not look alike:
 *
 *   * **Home, results, account** — the drill surface. The console's design
 *     language: soft ground, bento compartments, responsive to a phone.
 *   * **The exam runner** — imitates the real computer-delivered client
 *     (`docs/design/0013`), full-viewport and non-scrolling, and refuses to run
 *     under 1024px.
 *
 * The difference is a feature. A student should be able to tell at a glance
 * whether they are inside a timed exam, and the surest signal is that the whole
 * interface changes.
 *
 * `isSignedIn` is a hint, not an authority: it cannot read the httpOnly refresh
 * cookie. A wrong `true` costs one 401 and a bounce back to sign-in, which is
 * the same path an expired session already takes.
 */

import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { isSignedIn } from "../api/session";
import { SignIn } from "../auth/SignIn";
import { Home } from "../home/Home";
import { ExamRunner } from "../exam/Runner";
import { Result } from "../result/Result";
import { Review } from "../review/Review";

export function App() {
  const [signedIn, setSignedIn] = useState(isSignedIn);

  // The API client dispatches this when a refresh fails: the session is over and
  // no retry helps.
  useEffect(() => {
    const ended = () => setSignedIn(false);
    window.addEventListener("ielts:signed-out", ended);
    return () => window.removeEventListener("ielts:signed-out", ended);
  }, []);

  if (!signedIn) return <SignIn onSignedIn={() => setSignedIn(true)} />;

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/exam/:xid" element={<ExamRunner />} />
        <Route path="/result/:xid" element={<Result />} />
        <Route path="/review/:xid" element={<Review />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
