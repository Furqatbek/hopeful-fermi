/**
 * The typed API client.
 *
 * `paths` is GENERATED from `openapi/openapi.yaml` by `npm run codegen`, and the
 * generated file is committed. That is the whole reason this project is a
 * monorepo: `scripts/check_api_coverage.py` already fails CI when the running
 * application and the contract disagree, so the contract is trustworthy — and
 * this turns it into compile errors here. Remove a field from a response schema
 * and the screen that reads it stops building, in the same CI run.
 *
 * There is no `VITE_API_URL`. Vite proxies `/api` in development and Caddy
 * proxies the same prefix in production, so the browser only ever talks to one
 * origin: no CORS, no preflight, and no environment variable to get wrong on a
 * Friday.
 */

import createClient, { type Middleware } from "openapi-fetch";

import { clearSession, getAccessToken, refreshSession } from "./session";
import type { paths } from "./schema";

export const API_PREFIX = "/api/v1";

/** Routes that must NOT carry a token or trigger a refresh. */
const ANONYMOUS = ["/auth/otp/request", "/auth/otp/verify", "/auth/telegram/verify",
                   "/auth/refresh"];

function isAnonymous(url: string): boolean {
  return ANONYMOUS.some((path) => url.includes(path));
}

/**
 * A pristine copy of each in-flight request, keyed by the middleware `id`
 * openapi-fetch hands to every hook, taken BEFORE fetch consumes the body.
 *
 * `Request.clone()` throws once the body has been read, and openapi-fetch
 * passes `onResponse` the very object it gave `fetch` — so a clone cut there
 * works for a GET and a body-less POST and throws `TypeError` for anything
 * carrying JSON. That made the 401 replay fail for exactly the requests that
 * matter: `POST /attempts` and `POST /attempts/{xid}/answers` rejected once
 * per token expiry even though the refresh had succeeded, and a flush that
 * threw right before Finish let `outbox.drop` discard deltas the server never
 * received. Every entry is removed on the first of onResponse/onError, so the
 * map holds only what is actually in flight.
 */
const pending = new Map<string, Request>();

/** How many replay clones are currently held. Exposed for the test that
 *  proves the map does not grow; nothing in the app reads it. */
export function pendingReplays(): number {
  return pending.size;
}

/**
 * Attach the bearer token, and on a 401 refresh ONCE and replay.
 *
 * The replay is deliberately capped at a single attempt. A second 401 after a
 * successful refresh is not a timing problem, it is the server saying this
 * principal may not do this — retrying that in a loop turns a permission error
 * into a denial-of-service against our own rate limiter, which answers 429 and
 * would then look like an outage.
 */
const auth: Middleware = {
  async onRequest({ request, id }) {
    if (isAnonymous(request.url)) return request;
    const token = getAccessToken();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    // The body is still untouched here; see `pending` for why the clone cannot
    // wait until the response is in.
    pending.set(id, request.clone());
    return request;
  },

  async onResponse({ request, response, id }) {
    const stored = pending.get(id);
    pending.delete(id);
    if (response.status !== 401 || isAnonymous(request.url)) return response;

    const renewed = await refreshSession();
    if (!renewed) {
      clearSession();
      // The router listens for this rather than this module importing the
      // router: an API client that knows how to navigate is an API client you
      // cannot test without a DOM.
      window.dispatchEvent(new CustomEvent("ielts:signed-out"));
      return response;
    }

    const retry = stored ?? request.clone();
    retry.headers.set("Authorization", `Bearer ${getAccessToken() ?? ""}`);
    return fetch(retry);
  },

  async onError({ id }) {
    // A network failure never reaches onResponse; drop the clone so the map
    // cannot grow. Returning nothing lets the original error propagate.
    pending.delete(id);
  },
};

export const api = createClient<paths>({ baseUrl: API_PREFIX });
api.use(auth);

/**
 * Turn an RFC 9457 problem document into a sentence.
 *
 * The backend answers `application/problem+json` with `title`, `code` and
 * sometimes `findings` — `ValidationFailed` carries EVERY finding rather than
 * the first, which is a deliberate property of the authoring flow (an author
 * fixing one error at a time across six round trips is the failure it exists to
 * prevent). Flattening that to `error.title` here would throw away the thing
 * the server went to trouble to provide.
 */
export type Problem = {
  title?: string;
  code?: string;
  status?: number;
  findings?: { code?: string; message?: string; path?: string }[];
};

export function problemText(error: unknown): string {
  const problem = error as Problem | undefined;
  if (!problem) return "Something went wrong.";
  if (problem.findings?.length) {
    return problem.findings
      .map((f) => (f.path ? `${f.path}: ${f.message ?? f.code}` : f.message ?? f.code))
      .join("\n");
  }
  return problem.title ?? problem.code ?? "Something went wrong.";
}
