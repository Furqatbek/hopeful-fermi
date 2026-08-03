/**
 * Ending a session — on the server as well as in this browser.
 *
 * The nav's "Sign out" called `clearSession()` and nothing else. That forgets
 * the in-memory access token and removes the refresh token from
 * `localStorage`, and it leaves `auth_sessions.revoked_at` NULL: the refresh
 * token stays mintable for `refresh_token_ttl_days` — ninety — so anything
 * still holding a copy of it can go on issuing access tokens for that account
 * long after the person believes they have left. On the shared computer at the
 * front desk of a prep centre, which is where this console is actually used,
 * that is a session that outlives the human it belongs to.
 *
 * `POST /auth/logout` is what closes it, and nothing in the console had ever
 * called it.
 *
 * **The server call goes first, and that ordering is the whole point.**
 * `clearSession()` sets the access token to null, and this request authenticates
 * with that access token — `deps.principal` reads the `Authorization` header and
 * raises `unauthenticated` without one. Clear first and the revocation is sent
 * unsigned, refused, and never happens; the button behaves identically either
 * way, which is exactly why the original bug survived.
 *
 * **The local clear happens even when the server call does not.** A teacher on a
 * dropped connection presses Sign out; if we returned early on the network
 * failure, this browser would keep a working refresh token while its owner had
 * been told they were signed out. Signed out locally with a session still live
 * server-side is recoverable — the token is somewhere nobody can reach it from
 * this machine, and it expires. The reverse is not.
 */

import type { QueryClient } from "@tanstack/react-query";

import { api, problemText } from "../../api/client";
import { forgetPrincipal } from "../../api/principal";
import { clearSession } from "../../api/session";

/**
 * Revoke server-side, then clear this browser.
 *
 * `queries` is optional and worth passing: TanStack holds every screen's data
 * for `staleTime` (30 s) and `SignIn` does not reload the page — it flips a
 * React state flag — so without a clear, the next person to sign in at this desk
 * is shown the previous person's roster, results and profile until each query
 * refetches.
 *
 * Returns null when the server confirmed the revocation, or a sentence to show
 * when it did not. Not thrown, because the caller must not be able to skip the
 * local clear by forgetting a `catch`.
 */
export async function signOut(queries?: QueryClient): Promise<string | null> {
  let failed: string | null = null;
  try {
    const { error } = await api.POST("/auth/logout");
    if (error) failed = problemText(error);
  } catch {
    // Thrown rather than returned means the request never reached the server —
    // offline, or the API is down. Not distinguished from a refusal here
    // because the remedy is the same and the user is already leaving.
    failed = "Could not reach the server, so this session may still be open "
      + "elsewhere. It will be closed when you next sign in.";
  } finally {
    clearSession();
    // `principal.ts` caches the resolved principal in a module-level variable
    // for the life of the page. Signing out and back in as somebody else in the
    // same tab would otherwise render the second person with the FIRST person's
    // `platform_roles` — a centre admin shown platform-admin controls.
    forgetPrincipal();
    queries?.clear();
    // The same event `client.ts` fires when a refresh fails, and `App` already
    // listens for it. Dispatching here means any control that signs out lands on
    // the sign-in screen without each one needing its own state wiring.
    window.dispatchEvent(new CustomEvent("ielts:signed-out"));
  }
  return failed;
}
