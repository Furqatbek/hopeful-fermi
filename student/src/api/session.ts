/**
 * Token custody and refresh.
 *
 * The backend rotates refresh tokens and **revokes the entire chain when one is
 * reused** (`auth.refresh`: "Reuse of an already-rotated token revokes the whole
 * chain — that is how a stolen refresh token is detected"). That is the right
 * server behaviour and it puts one hard requirement on this client:
 *
 *   REFRESH MUST BE SINGLE-FLIGHT.
 *
 * An admin screen fires several requests at once — the test list, the org, the
 * current principal. When the access token expires they all get 401 together. A
 * naive client refreshes once per 401, so the second request sends a token the
 * first has already rotated, the server correctly reads that as theft, and the
 * teacher is logged out of every device mid-upload. The bug is ours, the
 * detection is right, and it only appears under concurrency — which is to say,
 * always in production and never in a manual test.
 *
 * `refreshing` below is the whole fix: the first 401 starts a refresh, every
 * other 401 awaits that same promise.
 *
 * ── where the tokens live ──────────────────────────────────────────────────
 *
 * **Access token: memory only.** Short (900 s) and never written to storage, so
 * an injected script cannot read it out of a previous tab.
 *
 * **Refresh token: nowhere this code can reach it.** It lives in the
 * `ielts_refresh` cookie — `HttpOnly`, `Secure`, `SameSite=Strict`, scoped to
 * `/api/v1/auth` — set by the server on every sign-in and every rotation
 * (ADR-0002 §6). There is no `getRefreshToken()` any more, and that is the
 * point: this file cannot read the credential, so neither can an XSS. What an
 * injected script can steal is a token that expires in fifteen minutes.
 *
 * This file used to keep it in `localStorage` and argued the trade honestly —
 * "the alternative is an httpOnly cookie, which this API cannot set — it is
 * bearer-token throughout, and adding a cookie path would mean CSRF defence on
 * every mutating route". The premise was the wrong half. A cookie that
 * authenticates EVERY route would need CSRF defence everywhere; this one
 * authenticates exactly one route, `POST /auth/refresh`, while every other call
 * still carries an `Authorization` header that a cross-origin page cannot set.
 * `SameSite=Strict` closes even that single endpoint, and it is affordable
 * because the console and its API are one origin.
 *
 * ── the hint flag ──────────────────────────────────────────────────────────
 *
 * `App` decides between the sign-in screen and the app synchronously, on
 * first render. It cannot ask about an httpOnly cookie — so a flag records that
 * we believe a session exists. It is NOT a credential and grants nothing: forge
 * it and you get a console shell that 401s on its first request and bounces you
 * to sign-in. It exists only to avoid rendering the sign-in form for a tenth of
 * a second to somebody who is already signed in.
 */

// Distinct from the console's key. The two apps are separate origins in
// production so their storage never meets — but in development they are
// localhost on two ports, which IS one origin for `localStorage`, and a
// shared key would have signing out of one silently sign you out of the other.
const SESSION_HINT = "ielts.student.session";

let accessToken: string | null = null;
let refreshing: Promise<boolean> | null = null;

export type Principal = {
  user: { xid: string; given_name: string; phone: string | null };
  memberships: { org_id: number; role: string; status: string }[];
  platform_roles: string[];
};

export function getAccessToken(): string | null {
  return accessToken;
}

/**
 * Do we believe there is a session? A hint, not an authority — see above.
 *
 * A `true` here that turns out to be wrong costs one 401 and a redirect, which
 * is the same path an expired session already takes.
 */
export function isSignedIn(): boolean {
  return localStorage.getItem(SESSION_HINT) === "1";
}

/**
 * Called on sign-in. Takes only the access token: the refresh token arrived in
 * a `Set-Cookie` this code never sees and the browser stores by itself.
 */
export function storeSession(access: string): void {
  accessToken = access;
  localStorage.setItem(SESSION_HINT, "1");
}

/**
 * Forget the session on THIS side.
 *
 * It cannot clear the cookie — httpOnly means script cannot delete it any more
 * than it can read it. Only the server can, and `POST /auth/logout` does, which
 * is why `signOut` calls the server first and this second. Calling this alone
 * leaves a live session on the server: that is the bug
 * `test_clearing_the_browser_alone_leaves_the_session_live` pins down.
 */
export function clearSession(): void {
  accessToken = null;
  localStorage.removeItem(SESSION_HINT);
}

/**
 * Exchange the refresh cookie for a new access token. At most one runs at a time.
 *
 * Returns false when the session is finished — expired, revoked, never existed,
 * or the chain was killed by a reuse detection. The caller's job is then to send
 * the user to the sign-in screen, not to retry.
 */
export async function refreshSession(): Promise<boolean> {
  if (refreshing) return refreshing;

  refreshing = (async () => {
    try {
      // No body and no token: the browser attaches the cookie. `credentials`
      // is spelled out rather than left to the default — the default is
      // `same-origin`, which is correct here and is exactly the assumption
      // worth stating, because the day someone points this at another origin
      // the cookie silently stops being sent.
      const response = await fetch("/api/v1/auth/refresh", {
        method: "POST",
        credentials: "same-origin",
      });
      if (!response.ok) {
        // 401 here is `no_session`, `invalid_token`, `session_expired` or
        // `token_reuse_detected`. None is retryable and all four mean the same
        // thing to a user: sign in again.
        clearSession();
        return false;
      }
      const body = (await response.json()) as { access_token: string };
      storeSession(body.access_token);
      return true;
    } catch {
      // A network failure is NOT an expired session. Leave the hint flag alone
      // so a reconnect can try again — clearing here would sign a teacher out
      // because their wifi dropped for a second.
      return false;
    } finally {
      refreshing = null;
    }
  })();

  return refreshing;
}
