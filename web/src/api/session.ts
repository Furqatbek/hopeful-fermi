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
 * Access token: memory only. It is short (900 s) and never touches storage, so
 * an injected script cannot read it out of a previous tab.
 *
 * Refresh token: `localStorage`, and this is a real trade rather than an
 * oversight. The alternative is an httpOnly cookie, which this API cannot set —
 * it is bearer-token throughout, and adding a cookie path would mean CSRF
 * defence on every mutating route. So: localStorage, reachable by XSS. What
 * makes that survivable is the rotation above — a stolen refresh token is
 * single-use, and the moment the real client uses its copy the whole chain dies
 * and the theft is visible in `auth_sessions.revoked_reason`.
 */

const REFRESH_KEY = "ielts.refresh";

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

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY);
}

export function isSignedIn(): boolean {
  return getRefreshToken() !== null;
}

/** Called on sign-in and after every rotation. */
export function storeSession(access: string, refresh: string): void {
  accessToken = access;
  localStorage.setItem(REFRESH_KEY, refresh);
}

export function clearSession(): void {
  accessToken = null;
  localStorage.removeItem(REFRESH_KEY);
}

/**
 * Exchange the refresh token for a new pair. At most one runs at a time.
 *
 * Returns false when the session is finished — expired, revoked, or the chain
 * was killed by a reuse detection. The caller's job is then to send the user to
 * the sign-in screen, not to retry.
 */
export async function refreshSession(): Promise<boolean> {
  if (refreshing) return refreshing;

  const token = getRefreshToken();
  if (!token) return false;

  refreshing = (async () => {
    try {
      const response = await fetch("/api/v1/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: token }),
      });
      if (!response.ok) {
        // 403 here is `session_expired`, `invalid_token` or
        // `token_reuse_detected`. None is retryable and all three mean the same
        // thing to a user: sign in again.
        clearSession();
        return false;
      }
      const body = (await response.json()) as {
        access_token: string;
        refresh_token: string;
      };
      storeSession(body.access_token, body.refresh_token);
      return true;
    } catch {
      // A network failure is NOT an expired session. Leave the stored refresh
      // token alone so a reconnect can use it — clearing here would sign a
      // teacher out because their wifi dropped for a second.
      return false;
    } finally {
      refreshing = null;
    }
  })();

  return refreshing;
}
