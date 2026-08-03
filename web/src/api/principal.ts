/**
 * Who is signed in, and what they may do.
 *
 * `Principal` is described in the contract as "everything the client needs to
 * render navigation without permission probes", and the console threw it away:
 * `POST /auth/otp/verify` returns one and sign-in kept only the tokens. So every
 * screen so far has either offered a control to everybody and let the server
 * refuse, or guessed.
 *
 * Guessing is the thing to avoid. Two controls genuinely cannot be offered
 * blindly — a public competition and a competition regrade decision are both
 * platform-admin acts, and showing either to a centre admin is a button that can
 * only ever fail. Everything else stays server-decided; this is for rendering,
 * never for authority. The server checks again regardless.
 *
 * Fetched rather than cached across reloads: a role can be taken away, and a
 * console that believed a stale localStorage copy would keep drawing controls
 * for authority the person no longer has.
 */

import { api } from "./client";

/** Narrowed to what the console renders from. `User` carries a phone number and
 *  a date-derived `is_minor`; neither belongs in a module every screen imports. */
export interface Principal {
  user: { xid: string; given_name: string };
  memberships: { role?: string; status?: string }[];
  platform_roles: string[];
}

let cached: Principal | null = null;

export async function loadPrincipal(): Promise<Principal | null> {
  if (cached) return cached;
  const { data, error } = await api.GET("/auth/session");
  if (error || !data) return null;
  cached = {
    user: { xid: data.user.xid, given_name: data.user.given_name },
    memberships: (data.memberships ?? []).map((m) => ({
      role: m.role, status: m.status,
    })),
    platform_roles: data.platform_roles ?? [],
  };
  return cached;
}

export function forgetPrincipal(): void {
  cached = null;
}

export function isPlatformAdmin(principal: Principal | null): boolean {
  return principal?.platform_roles?.includes("platform_admin") ?? false;
}
