/**
 * Turning a media grant into something an `<audio>` element can play, and
 * knowing when it has stopped being one.
 *
 * Pure and separate from the screens because two decisions live here and both
 * are invisible in JSX.
 *
 * **The grant travels in the query string, and that is not laziness.** A media
 * element cannot set request headers — there is no way to put a bearer token on
 * `<audio src>` — which is exactly why `GET /media/{xid}/content` takes a signed
 * grant bound to the user, the object and a ~120 s expiry instead. The URL this
 * builds is therefore a working download link for anyone holding it, for two
 * minutes, for one account. Never store it, never log it, never offer it as a
 * "copy link".
 *
 * **Expiry is counted from when the grant ARRIVED, not from the device clock.**
 * `expires_at` is a server timestamp and a phone in a Tashkent classroom can be
 * minutes out; comparing it against `Date.now()` on a device set ten minutes
 * fast declares every fresh grant dead, and one set ten minutes slow declares a
 * dead one alive. Measuring elapsed local time against the lifetime we read at
 * receipt gets the second case right — the one that matters, because an expired
 * grant does not produce an error dialogue, it produces an `<audio>` element
 * that silently refuses to play.
 */

import { API_PREFIX } from "../../api/client";

/** The three fields both grant endpoints return — the authoring one on
 *  `POST /audio-tracks/{xid}/grant` and the exam one on
 *  `POST /attempts/{xid}/sections/{position}/audio-grant`. Identical shapes, so
 *  one player serves the library and the preview. */
export interface Issued {
  grant: string;
  media_xid: string;
  expires_at: string;
}

/** Re-request this far before the server would refuse. A grant that expires
 *  mid-request is a failed play, and the cost of asking again early is one
 *  cheap POST. */
export const MARGIN_MS = 10_000;

/**
 * The URL to put on `src`.
 *
 * Same-origin: Vite proxies `/api` in development and Caddy proxies it in
 * production, so this never needs a host and never triggers a preflight. The
 * grant is encoded because it is base64url plus dots — safe today, and one
 * signature format change away from not being.
 */
export function mediaUrl(issued: Issued): string {
  return `${API_PREFIX}/media/${issued.media_xid}/content`
    + `?grant=${encodeURIComponent(issued.grant)}`;
}

/**
 * Milliseconds of playback authority left, floored at zero.
 *
 * `receivedAt` and `now` are both local clock readings, so their difference is
 * a real elapsed duration whatever the device thinks the date is. The lifetime
 * itself is read once at receipt; a device whose clock is fast reads a short or
 * negative lifetime and simply asks for a new grant sooner, which is the safe
 * direction to be wrong in.
 */
export function remainingMs(issued: Issued, receivedAt: number, now: number): number {
  const lifetime = Date.parse(issued.expires_at) - receivedAt;
  if (Number.isNaN(lifetime)) return 0;
  return Math.max(0, lifetime - (now - receivedAt));
}

/** Whether to fetch a new grant before touching the element. */
export function isStale(issued: Issued, receivedAt: number, now: number): boolean {
  return remainingMs(issued, receivedAt, now) <= MARGIN_MS;
}

/**
 * What to tell someone whose audio did not play.
 *
 * A refused range request surfaces on the element as a `MediaError` and nowhere
 * else: no exception, no rejected promise, no status code the screen can read.
 * Without this the whole failure is a player that sits at 0:00, which is the
 * report support actually receives — "the audio does not work" — and cannot act
 * on. The codes are the four `MediaError` constants.
 */
export function playbackFailure(code: number | undefined, stale: boolean): string {
  if (stale) return "The playback link expired. Ask for a new one and try again.";
  switch (code) {
    case 1:
      return "Playback was stopped.";
    case 2:
      return "The connection dropped while loading the audio. Try again.";
    case 3:
      return "This file could not be decoded. It may not have finished processing.";
    case 4:
      return "The audio could not be loaded. The playback link may have expired — "
        + "ask for a new one.";
    default:
      return "The audio could not be played. Ask for a new playback link and try again.";
  }
}
