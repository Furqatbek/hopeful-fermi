/**
 * The safety queue, and acting on what is in it.
 *
 * This is the child-safety surface. The product matches students for live 1:1
 * speaking practice and many of them are minors; this is where a report about
 * one of those calls is read and answered. Three things shape the screen.
 *
 * **The minors queue is a separate commitment, not a filter.** `queue=minors` is
 * its own list, backed by its own partial index, with its own response time. So
 * it is not a tab beside the general one — a tab is a thing a moderator can
 * leave unopened for a shift, and an unopened tab is exactly the failure this
 * queue exists to prevent. It is rendered first, always, whether or not anything
 * is in it, with the number waiting stated in words at the top of the page.
 *
 * **A moderation action is immediate and destructive.** A `suspend` or `ban`
 * revokes every `auth_sessions` row for that person in the same transaction and
 * pushes `session.revoked` down any open socket. It cannot be undone from here.
 * So the two actions that do that are the only ones this screen asks to have
 * confirmed, and it shows `sessions_revoked` afterwards — a ban that reports
 * zero revoked is the signal that it landed on nobody.
 *
 * **The queue is thinner than the job needs, and this screen says so rather than
 * papering over it.** `SafetyReport` carries an id, a category, a status, a
 * priority, `involves_minor` and a timestamp. It does not carry the reported
 * person, the reporter, the description, or the pair the report is about, and
 * `has_evidence` is hardcoded `false` by the handler even for a report filed
 * with an audio buffer. There is no report-detail endpoint and no listing of
 * actions already taken. All of that is reported to the backend; none of it is
 * invented here. A console that rendered a plausible-looking evidence player or
 * a subject name it does not have would be worse than one that admits the gap.
 *
 * Platform admin only, and refused before the first request rather than after:
 * a centre's staff must never read this queue, and showing them an error banner
 * over an empty table reads as an outage rather than as an answer.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, problemText } from "../../api/client";
import { isPlatformAdmin, loadPrincipal } from "../../api/principal";
import { CONTENT_ACTIONS, REVOKES_SESSIONS, SUBJECT_TYPES, USER_ACTIONS,
         expiryIso, problemWith, toAction, toSubjectType } from "./action";
import type { ActionDraft, ModerationAction } from "./action";
import { forReview, unattendedCount, waited } from "./queue";
import { useSafetyStream } from "./useSafetyStream";
import "./moderation.css";

/** Poll cadence. The socket cannot be relied on to announce a new report —
 *  `safety.*` has no producer — so this interval is what makes the queue live.
 *  Half a minute because the minors queue carries a response-time promise and a
 *  moderator staring at a stale page is how one is missed. */
const POLL_MS = 30_000;

const EMPTY: ActionDraft = {
  action: "warn", reason: "", targetUserXid: "", subjectType: "",
  subjectXid: "", expiresLocal: "", acknowledged: false,
};

export function Moderation() {
  const queries = useQueryClient();
  const [acting, setActing] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const principal = useQuery({
    queryKey: ["principal"],
    queryFn: loadPrincipal,
    staleTime: Infinity,
  });
  const admin = isPlatformAdmin(principal.data ?? null);

  const minors = useQuery({
    queryKey: ["reports", "minors"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/admin/reports", {
        params: { query: { queue: "minors" } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: admin,
    refetchInterval: POLL_MS,
  });

  const general = useQuery({
    queryKey: ["reports", "general"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/admin/reports", {
        params: { query: { queue: "general" } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: admin,
    refetchInterval: POLL_MS,
  });

  // A frame does not carry the report, so it is a prompt to refetch rather than
  // data. `resync` is here for the same reason it exists at all: it is the
  // server saying this connection has a hole and state must be reloaded over
  // HTTP, which for the `safety` family it names as `/api/v1/admin/reports`.
  const stream = useSafetyStream(admin, (frame) => {
    if (frame.type.startsWith("safety.") || frame.type === "resync") {
      setNow(Date.now());
      void queries.invalidateQueries({ queryKey: ["reports"] });
    }
  });

  // Every age on the page is measured against this. Without the tick a report
  // that has been waiting an hour keeps reading "5 min" until something else
  // happens to re-render, and the one number this screen is worked against is
  // the one that would be wrong.
  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), POLL_MS);
    return () => clearInterval(tick);
  }, []);

  if (principal.isPending) return <div className="page"><p className="muted">Checking…</p></div>;

  if (!admin) {
    return (
      <div className="page">
        <h1>Safety</h1>
        <p className="muted">
          Only a platform admin can open the safety queue. Reports name a
          reporter, a person reported and a category, and the highest-priority
          ones involve children. Centre staff, including a centre's own admin,
          never see them.
        </p>
      </div>
    );
  }

  const minorRows = forReview(minors.data?.items ?? []);
  const generalRows = forReview(general.data?.items ?? []);
  const waitingOnMinors = unattendedCount(minorRows);

  const refetching = () => {
    setNow(Date.now());
    void queries.invalidateQueries({ queryKey: ["reports"] });
  };

  return (
    <div className="page">
      <header>
        <h1>Safety</h1>
        <span className="muted">
          <Connection state={stream.state} refused={stream.refused.length > 0} />
        </span>
      </header>

      {minors.isError && <p className="error">{problemText(minors.error)}</p>}
      {general.isError && <p className="error">{problemText(general.error)}</p>}

      {/* Rendered whether or not anything is waiting, and rendered first. A
          minors queue that appears only when it is non-empty is a queue a
          moderator has no habit of looking at. */}
      <section className={waitingOnMinors > 0 ? "minors waiting" : "minors"}>
        <h2>Reports involving a minor</h2>
        {waitingOnMinors > 0 ? (
          <p>
            <span className="count">{waitingOnMinors}</span>
            {waitingOnMinors === 1 ? " report is waiting." : " reports are waiting."}{" "}
            This queue has its own response time. Answer these before anything
            else on this page.
          </p>
        ) : (
          <p className="muted">Nothing is waiting.</p>
        )}
        <Queue rows={minorRows} now={now} acting={acting} onAct={setActing}
               empty="No reports involving a minor." />
      </section>

      <h2>All reports</h2>
      <p className="muted">
        Everything the queue holds, including the ones above — the general list
        is not the remainder. One page only: the server returns a fixed number
        of rows and offers no way to ask for the next.
      </p>
      <Queue rows={generalRows} now={now} acting={acting} onAct={setActing}
             empty="No reports." />

      {acting && (
        <ActionForm
          reportXid={acting}
          onClose={() => setActing(null)}
          onDone={refetching}
        />
      )}

      <h2>What this screen cannot tell you</h2>
      <ul className="muted">
        <li>
          The queue does not name the person reported. To act on somebody you
          need their user id from elsewhere; pasting it here is deliberate, not
          a shortcut.
        </li>
        <li>
          Recording an action does not close the report. Its status here does
          not change, so a report you have already answered still reads as new,
          and nothing on this page lists the actions already taken.
        </li>
        <li>
          Evidence audio is not readable here. A report filed from a call can
          carry a 60-second buffer, and nothing in this console can reach it.
        </li>
      </ul>
    </div>
  );
}

/** The live-ness of the socket, in words.
 *
 *  Shown even when it is fine, because the state that matters is the one nobody
 *  would notice: a connection that has quietly stopped delivering looks exactly
 *  like a queue with nothing new in it. */
function Connection({ state, refused }: { state: string; refused: boolean }) {
  if (refused) {
    // Both refusal codes mean the same thing here and the server sends no more
    // than that on purpose, so this says no more than that either.
    return <>Live updates not available. The list still refreshes.</>;
  }
  if (state === "live") return <>Live</>;
  if (state === "connecting") return <>Connecting…</>;
  if (state === "retrying") return <>Reconnecting. The list still refreshes.</>;
  return <>Live updates off. The list still refreshes.</>;
}

interface Row {
  xid?: string;
  category?: string;
  status?: string;
  priority?: string;
  involves_minor?: boolean;
  created_at?: string;
}

function Queue({ rows, now, acting, onAct, empty }: {
  rows: Row[];
  now: number;
  acting: string | null;
  onAct: (xid: string | null) => void;
  empty: string;
}) {
  return (
    <table>
      <thead>
        <tr>
          <th>Waiting</th><th>Priority</th><th>Category</th><th>Status</th>
          <th>Report</th><th />
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.xid}>
            <td className="num">{waited(row.created_at, now)}</td>
            <td className={row.priority === "critical" ? "urgent" : undefined}>
              {row.priority}
              {/* Stated on the row as well as by which list it is in: the
                  general list carries these too, and a critical report read
                  there is the same report. */}
              {row.involves_minor && <span className="muted"> · minor</span>}
            </td>
            <td>{row.category}</td>
            <td className="muted">{row.status}</td>
            <td className="muted"><code>{row.xid?.slice(0, 8)}</code></td>
            <td>
              <button
                className="link"
                onClick={() => onAct(acting === row.xid ? null : row.xid ?? null)}
              >
                {acting === row.xid ? "Cancel" : "Act"}
              </button>
            </td>
          </tr>
        ))}
        {rows.length === 0 && (
          <tr><td colSpan={6} className="muted">{empty}</td></tr>
        )}
      </tbody>
    </table>
  );
}

/**
 * Taking an action, attached to the report that prompted it.
 *
 * `report_xid` is sent on every action from this screen. It is optional on the
 * endpoint, and an action recorded without it is an entry in the audit log with
 * no answer to "what was this about" — which is the question the log exists for.
 */
function ActionForm({ reportXid, onClose, onDone }: {
  reportXid: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [draft, setDraft] = useState<ActionDraft>(EMPTY);
  const [failed, setFailed] = useState<string | null>(null);
  const [outcome, setOutcome] =
    useState<{ action: ModerationAction; revoked: number } | null>(null);

  const change = (patch: Partial<ActionDraft>) =>
    setDraft((previous) => ({ ...previous, ...patch }));

  const content = draft.action.startsWith("content_");
  const revokes = REVOKES_SESSIONS.includes(draft.action);
  const wrong = problemWith(draft);

  const take = useMutation({
    mutationFn: async () => {
      const problem = problemWith(draft);
      if (problem) throw new Error(problem);
      const expires = expiryIso(draft.expiresLocal);
      const kind = draft.subjectType;
      // `problemWith` has already refused a content action with no kind. This
      // narrows it for the typed client, which accepts only a declared subject
      // type — the same seven `_SUBJECT_TABLES` knows about, and the ones the
      // endpoint answers 404 for anything outside.
      if (content && kind === "") throw new Error("Choose what kind of content that is.");
      const { data, error: failure } = await api.POST("/admin/moderation-actions", {
        body: {
          action: draft.action,
          reason: draft.reason.trim(),
          report_xid: reportXid,
          ...(content && kind !== ""
            ? { target_subject_type: kind,
                target_subject_xid: draft.subjectXid.trim() }
            : { target_user_xid: draft.targetUserXid.trim() }),
          ...(expires ? { expires_at: expires } : {}),
        },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setFailed(null);
      setOutcome({ action: draft.action, revoked: data?.sessions_revoked ?? 0 });
      setDraft(EMPTY);
      onDone();
    },
    onError: (failure) => setFailed(problemText(failure) || String(failure)),
  });

  if (outcome) {
    return (
      <div className="issued">
        <h2>Recorded</h2>
        <p>
          <strong>{outcome.action}</strong> recorded against report{" "}
          <code>{reportXid.slice(0, 8)}</code>.
        </p>
        {REVOKES_SESSIONS.includes(outcome.action) && (
          outcome.revoked > 0 ? (
            <p>
              {outcome.revoked === 1
                ? "One signed-in session was ended."
                : `${outcome.revoked} signed-in sessions were ended.`}
            </p>
          ) : (
            /* Not a success message. The handler revokes only when it found the
               user, and returns 201 either way — so zero here most often means
               the id matched nobody, and the account is still open. */
            <p className="error">
              No session was ended. Either the person was not signed in
              anywhere, or the user id matched nobody. Check the id before
              treating this report as handled.
            </p>
          )
        )}
        <p className="muted">
          The report's status is unchanged. Nothing in this console can close
          it.
        </p>
        <button className="link" onClick={onClose}>Close</button>
      </div>
    );
  }

  return (
    <div className="issued">
      <h2>Act on report {reportXid.slice(0, 8)}</h2>
      {failed && <p className="error">{failed}</p>}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          setFailed(null);
          take.mutate();
        }}
      >
        <label htmlFor="mod-action">Action</label>
        <select
          id="mod-action"
          value={draft.action}
          onChange={(event) => {
            const chosen = toAction(event.target.value);
            // The acknowledgement is cleared with every change of action, so a
            // tick made for a `warn` cannot be carried into a `ban`.
            if (chosen) change({ action: chosen, acknowledged: false });
          }}
        >
          <optgroup label="A person">
            {USER_ACTIONS.map((action) => (
              <option key={action} value={action}>{action}</option>
            ))}
          </optgroup>
          <optgroup label="A piece of content">
            {CONTENT_ACTIONS.map((action) => (
              <option key={action} value={action}>{action}</option>
            ))}
          </optgroup>
        </select>

        {content ? (
          <>
            <label htmlFor="mod-subject-type">Kind of content</label>
            <select
              id="mod-subject-type"
              value={draft.subjectType}
              onChange={(event) =>
                change({ subjectType: toSubjectType(event.target.value) })}
            >
              <option value="">— choose —</option>
              {SUBJECT_TYPES.map((kind) => (
                <option key={kind} value={kind}>{kind}</option>
              ))}
            </select>

            <label htmlFor="mod-subject">Content id</label>
            <input
              id="mod-subject"
              value={draft.subjectXid}
              onChange={(event) => change({ subjectXid: event.target.value })}
              placeholder="018f2a1c-4c6e-7b3a-9d21-5f0e6a7b8c90"
            />
          </>
        ) : (
          <>
            <label htmlFor="mod-user">User id of the person</label>
            <input
              id="mod-user"
              value={draft.targetUserXid}
              onChange={(event) => change({ targetUserXid: event.target.value })}
              placeholder="018f2a1c-4c6e-7b3a-9d21-5f0e6a7b8c90"
            />
            <p className="muted">
              The queue does not carry it, so it has to be pasted. An action
              with nobody named is still recorded, and ends nothing.
            </p>
          </>
        )}

        <label htmlFor="mod-reason">Reason</label>
        <textarea
          id="mod-reason"
          rows={3}
          value={draft.reason}
          onChange={(event) => change({ reason: event.target.value })}
          placeholder="Repeated sexual messages to a 15-year-old in a paired call."
        />
        <p className="muted">
          Written to a log that cannot be edited or deleted, against your name.
          Somebody reads it months later, possibly a regulator.
        </p>

        {!content && (
          <>
            <label htmlFor="mod-expires">Ends at (optional)</label>
            <input
              id="mod-expires"
              type="datetime-local"
              value={draft.expiresLocal}
              onChange={(event) => change({ expiresLocal: event.target.value })}
            />
            <p className="muted">
              Leave empty for an action with no end. Your own timezone is
              applied.
            </p>
          </>
        )}

        {revokes && (
          <label className="choice">
            <input
              type="checkbox"
              checked={draft.acknowledged}
              onChange={(event) => change({ acknowledged: event.target.checked })}
            />
            {/* The confirmation is on these two alone. Asking for one on every
                action is how a moderator learns to tick without reading, and
                these are the two that end a live call as they are taken. */}
            I am ending every session this person has open, on every device,
            now.
          </label>
        )}

        <div className="row">
          <button disabled={take.isPending || wrong !== null}>
            {take.isPending ? "Recording…" : "Record this action"}
          </button>
          <button type="button" className="link" onClick={onClose}>Cancel</button>
        </div>
        {wrong && <p className="muted">{wrong}</p>}
      </form>
    </div>
  );
}
