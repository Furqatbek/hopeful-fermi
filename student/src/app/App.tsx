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
import { DisplaySettingsProvider, SettingsButton } from "./Settings";
import { DrillLayout } from "./DrillLayout";
import { Join } from "../auth/Join";
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

  // An invitation link, read BEFORE the sign-in gate rather than as a route.
  // A brand-new student has no session, so the router below never renders for
  // them — putting `/join` in it would make the one screen they need the one
  // screen they cannot reach.
  const invite = new URLSearchParams(window.location.search).get("invite");

  // `DisplaySettingsProvider` wraps BOTH branches, not just the routes below —
  // the chosen size and theme live in localStorage and outlive a session, so a
  // returning student who set "yellow on black" mid-exam should see it on
  // sign-in too, not just after they are back in.
  //
  // `SettingsButton` here is not just consistency with the drill surface. It is
  // the ONLY way a student who has never signed in yet can reach the setting at
  // all — a brand-new student on `Join`, reading the smallest text on the whole
  // screen (`phone_hint`, the four digits that prove an invitation is theirs),
  // has no assignment to open and no exam to start, and the exam runner's own
  // entry point is two screens away behind a sign-in they may not yet be able
  // to read. Aligned to the sign-in card's own width rather than the full
  // viewport, so it reads as this screen's control and not a stray top bar.
  if (!signedIn) {
    return (
      <DisplaySettingsProvider>
        <div style={{
          display: "flex", justifyContent: "flex-end",
          maxWidth: "24rem", margin: "1.25rem auto 0", padding: "0 1.25rem",
        }}>
          <SettingsButton />
        </div>
        {invite
          ? <Join token={invite} onJoined={() => setSignedIn(true)} />
          : <SignIn onSignedIn={() => setSignedIn(true)} />}
      </DisplaySettingsProvider>
    );
  }

  return (
    <DisplaySettingsProvider>
      <BrowserRouter>
        <Routes>
          {/* Home, Result and Review are the drill surface and share
              `DrillLayout` for exactly one reason: somewhere to reach
              Settings that is not "start a mock". The exam runner keeps its
              own, inside `Chrome`'s top bar, imitating the real client. */}
          <Route element={<DrillLayout />}>
            <Route path="/" element={<Home />} />
            <Route path="/result/:xid" element={<Result />} />
            <Route path="/review/:xid" element={<Review />} />
          </Route>
          <Route path="/exam/:xid" element={<ExamRunner />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </DisplaySettingsProvider>
  );
}
