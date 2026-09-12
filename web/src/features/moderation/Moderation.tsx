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
 * **The queue now names who a report is about, and whether there is anything to
 * listen to.** Both used to be missing: `subject_user_id` is written on every
 * report and was dropped by the DTO, and `has_evidence` was the literal `false`
 * even for a report filed with an audio buffer — so a moderator could not learn
 * from this queue who to act on, while the action form below demands exactly
 * that user's xid. Clicking the name fills it in, which is the whole point.
 *
 * Still missing and still not invented here: the reporter, the description, the
 * pair a speaking report is about, and any way to PLAY the evidence. There is
 * no report-detail endpoint. A console that rendered a plausible-looking
 * evidence player it cannot actually fill would be worse than one that admits
 * the gap. What HAS been done is listed — `History`, below the form — because
 * a moderator who cannot see that a person was actioned an hour ago is one who
 * actions them twice.
 *
 * Platform admin only, and refused before the first request rather than after:
 * a centre's staff must never read this queue, and showing them an error banner
 * over an empty table reads as an outage rather than as an answer.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { Confirm } from "../../app/Confirm";
import { Status } from "../../app/Icon";
import { api, problemText } from "../../api/client";
import { isPlatformAdmin, loadPrincipal } from "../../api/principal";
import { CONTENT_ACTIONS, REVOKES_SESSIONS, SUBJECT_TYPES, USER_ACTIONS,
         expiryIso, problemWith, toAction, toSubjectType } from "./action";
import type { components } from "../../api/schema";
import type { ActionDraft, ModerationAction } from "./action";
import { categoryLabel, forReview, unattendedCount, waited } from "./queue";
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
    // `POST /admin/moderation-actions` emits no socket frame (see
    // `useSafetyStream`), so this is the only way "Already decided" learns of
    // the action just recorded — and a history that shows it only after a
    // reload is the double-ban the `History` docstring exists to prevent.
    void queries.invalidateQueries({ queryKey: ["moderation-actions"] });
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
          // Remount per report, so the seeded subject follows the row that was
          // clicked rather than sticking from the one before it.
          key={acting}
          reportXid={acting}
          subjectUserXid={
            [...minorRows, ...generalRows]
              .find((row) => row.xid === acting)?.subject_user_xid ?? ""
          }
          onClose={() => setActing(null)}
          onDone={refetching}
        />
      )}

      <History />

      <h2>What this screen cannot tell you</h2>
      <ul className="muted">
        <li>
          Evidence audio is not readable here. A report filed from a call can
          carry a 60-second buffer, and nothing in this console can reach it.
        </li>
        <li>
          There is no report detail. The reporter and their description are
          stored and not exposed, so the queue row and the history below are
          everything this console knows.
        </li>
      </ul>
    </div>
  );
}



/** What has already been done.
 *
 *  `moderation_actions` was written and never read, so a moderator opening a
 *  report could not see that this person had already been warned twice, or that
 *  the report in front of them was actioned an hour ago. The likeliest outcome
 *  of that is the same person banned twice for one incident, with two immutable
 *  rows saying so.
 *
 *  Newest first, unlike the queue above: a queue is worked oldest-first because
 *  the SLA clock started when the report was filed, and a history is read
 *  newest-first because the question is what happened most recently. */
function History() {
  const actions = useQuery({
    queryKey: ["moderation-actions"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/admin/moderation-actions", {
        params: { query: { limit: 50 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  if (actions.isError) {
    return <p className="error">{problemText(actions.error)}</p>;
  }
  const rows = actions.data?.items ?? [];

  return (
    <>
      <h2>Already decided</h2>
      {rows.length === 0 ? (
        <p className="muted">Nothing has been actioned yet.</p>
      ) : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>When</th><th>Action</th><th>Who</th><th>Reason</th><th>By</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.xid}>
                  <td className="muted">{row.created_at?.slice(0, 16).replace("T", " ")}</td>
                  <td>
                    {row.action}
                    {/* A reversal is the one thing that changes what a row means,
                        and it is not visible from the action name alone. */}
                    {row.reversed_at && <span className="muted"> · reversed</span>}
                  </td>
                  <td>{row.target_name || row.target_user_xid?.slice(0, 8)
                       || row.target_subject_type || "—"}</td>
                  <td className="muted">{row.reason}</td>
                  <td className="muted">{row.actor_name ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
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

/** Derived, not restated. This was a hand-written interface listing six of the
 *  fields, so when the server started sending the subject and the evidence flag
 *  the compiler said the properties did not exist — a local copy of a contract
 *  is a copy that goes stale, and the generated types exist so it cannot. */
type Row = components["schemas"]["SafetyReport"];

function Queue({ rows, now, acting, onAct, empty }: {
  rows: Row[];
  now: number;
  acting: string | null;
  onAct: (xid: string | null) => void;
  empty: string;
}) {
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Waiting</th><th>Priority</th><th>Category</th><th>About</th>
            <th>Status</th><th>Report</th><th />
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
              <td>
                {categoryLabel(row.category)}
                {/* The buffer is discarded unless a report is filed, so its
                    presence is itself information: this is a report somebody
                    chose to attach sixty seconds of a minor's conversation to. */}
                {row.has_evidence && <span className="muted"> · audio</span>}
              </td>
              <td>
                {row.subject_user_xid ? (
                  <span title={row.subject_user_xid}>
                    {row.subject_name || row.subject_user_xid.slice(0, 8)}
                  </span>
                ) : (
                  <span className="muted">{row.subject_kind ?? "—"}</span>
                )}
              </td>
              <td><Status value={row.status} /></td>
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
            <tr><td colSpan={7} className="muted">{empty}</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Taking an action, attached to the report that prompted it.
 *
 * `report_xid` is sent on every action from this screen. It is optional on the
 * endpoint, and an action recorded without it is an entry in the audit log with
 * no answer to "what was this about" — which is the question the log exists for.
 */
function ActionForm({ reportXid, subjectUserXid, onClose, onDone }: {
  reportXid: string;
  /** Seeded from the report's own subject. The queue names the person now, and
   *  re-typing a uuid a moderator can see on the row above is exactly the sort
   *  of transcription this screen should not ask for under time pressure. */
  subjectUserXid: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [draft, setDraft] = useState<ActionDraft>(
    subjectUserXid ? { ...EMPTY, targetUserXid: subjectUserXid } : EMPTY);
  const [failed, setFailed] = useState<string | null>(null);
  const [outcome, setOutcome] =
    useState<{ action: ModerationAction; revoked: number } | null>(null);
  const [asking, setAsking] = useState(false);

  const change = (patch: Partial<ActionDraft>) =>
    setDraft((previous) => ({ ...previous, ...patch }));

  const content = draft.action.startsWith("content_");
  const revokes = REVOKES_SESSIONS.includes(draft.action);
  // Everything wrong with the draft EXCEPT the acknowledgement, which the dialog
  // collects rather than the form. `problemWith` still refuses an unacknowledged
  // suspend on the way out — it is the last check before the request — but it
  // must not disable the button whose entire job is to ask for it.
  const wrong = problemWith({ ...draft, acknowledged: true });

  const take = useMutation({
    mutationFn: async (acknowledged: boolean) => {
      const problem = problemWith({ ...draft, acknowledged });
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
      setAsking(false);
      setOutcome({ action: draft.action, revoked: data?.sessions_revoked ?? 0 });
      setDraft(EMPTY);
      onDone();
    },
    // The dialog closes on a refusal too, so the reason — which renders on the
    // form behind it — is not hidden by the thing that caused it.
    onError: (failure) => {
      setAsking(false);
      setFailed(problemText(failure) || String(failure));
    },
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
          // A warning ends no session and goes straight out. Ceremony on every
          // action is how a moderator learns to click through the one that
          // matters, so only the two that end live calls stop here.
          if (revokes) setAsking(true);
          else take.mutate(false);
        }}
      >
        <label htmlFor="mod-action">Action</label>
        <select
          id="mod-action"
          value={draft.action}
          onChange={(event) => {
            const chosen = toAction(event.target.value);
            if (chosen) change({ action: chosen });
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

        <div className="row">
          <button disabled={take.isPending || wrong !== null}>
            {take.isPending ? "Recording…" : "Record this action"}
          </button>
          <button type="button" className="link" onClick={onClose}>Cancel</button>
        </div>
        {wrong && <p className="muted">{wrong}</p>}

        {/* This was the acknowledgement checkbox, and it sat ABOVE the button —
            which is the wrong place for the sentence that matters most on this
            form. A tick is made once and then read past on the way to the
            control; the dialog is the last thing between the decision and a
            live call ending, which is where that sentence belongs.

            Only for the two actions that revoke sessions. A dialog on a warning
            is how a moderator learns to click through the one that counts. */}
        <Confirm
          open={asking}
          title={draft.action === "ban" ? "Ban this person?" : "Suspend this person?"}
          confirmLabel={draft.action === "ban" ? "Ban them" : "Suspend them"}
          busy={take.isPending}
          onCancel={() => setAsking(false)}
          onConfirm={() => take.mutate(true)}
          detail={
            <>
              <p>
                Every session this person has open ends now, on every device —
                including a paired speaking call they are in as you press this.
              </p>
              <p className="muted">
                Recorded against your name in a log that cannot be edited or
                deleted. Somebody reads it months later, possibly a regulator.
                {draft.expiresLocal.trim()
                  ? " It lifts automatically at the time you set."
                  : " It has no end date."}
              </p>
            </>
          }
        />
      </form>
    </div>
  );
}
