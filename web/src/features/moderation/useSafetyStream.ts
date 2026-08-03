/**
 * The `safety` channel, held open for as long as the queue is on screen.
 *
 * This is the React half of `api/realtime.ts`, kept out of that module so the
 * connection state machine can be tested without a renderer.
 *
 * **The socket is an accelerator, never the source of truth.** `safety.*` is
 * declared on the channel, authorized, and emitted by nothing today — the API
 * contract lists it under "declared, no producer yet" (§4.8), and neither
 * `POST /speaking/pairs/{xid}/report` nor `POST /admin/moderation-actions`
 * writes an outbox row that becomes one. A queue that refreshed only on a frame
 * would therefore never refresh at all. So the screen polls, and a frame — if a
 * producer is ever wired — just makes the next poll happen sooner. The socket
 * still earns its place: it is the only thing that can tell a moderator their
 * view has stopped being live.
 */

import { useEffect, useRef, useState } from "react";

import { api } from "../../api/client";
import { connect, socketUrl } from "../../api/realtime";
import type { Frame, StreamStatus } from "../../api/realtime";

const OFFLINE: StreamStatus = { state: "closed", attempt: 0, refused: [] };

async function mintTicket(): Promise<string> {
  const { data, error } = await api.POST("/realtime/ticket");
  if (error || !data) throw error ?? new Error("no ticket");
  // The response also carries a `url`, and it is not used: the gateway is
  // mounted at `/realtime` while `docker-compose.yml` advertises
  // `/api/v1/realtime`, a path the application does not serve. Same-origin is
  // both correct and what the rest of this client already assumes.
  return data.ticket;
}

/**
 * Hold `safety` open while `enabled`, and call `onFrame` for every frame.
 *
 * `onFrame` is read through a ref so that a new closure on each render does not
 * tear the connection down and build a new one — which, with a single-use
 * ticket, would mean a fresh ticket per render.
 */
export function useSafetyStream(
  enabled: boolean,
  onFrame: (frame: Frame) => void,
): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>(OFFLINE);
  const latest = useRef(onFrame);
  latest.current = onFrame;

  useEffect(() => {
    if (!enabled) {
      setStatus(OFFLINE);
      return;
    }
    const stream = connect({
      url: socketUrl(window.location.href),
      channels: ["safety"],
      mintTicket,
      onFrame: (frame) => latest.current(frame),
      onStatus: setStatus,
    });
    return () => stream.close();
  }, [enabled]);

  return status;
}
