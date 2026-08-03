import { useEffect, useState } from "react";

import { isSignedIn } from "../api/session";
import { SignIn } from "../features/auth/SignIn";
import { TestLibrary } from "../features/tests/TestLibrary";

export function App() {
  const [signedIn, setSignedIn] = useState(isSignedIn);

  // The client dispatches this when a refresh fails: the session is finished and
  // no amount of retrying will help. Listening here rather than importing a
  // router into `client.ts` keeps the API layer testable without a DOM.
  useEffect(() => {
    const signOut = () => setSignedIn(false);
    window.addEventListener("ielts:signed-out", signOut);
    return () => window.removeEventListener("ielts:signed-out", signOut);
  }, []);

  if (!signedIn) return <SignIn onSignedIn={() => setSignedIn(true)} />;
  return <TestLibrary />;
}
