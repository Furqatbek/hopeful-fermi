/**
 * Copyright claims, and the record of what was decided about them.
 *
 * "Assume some centres will try to upload published Cambridge papers, and design
 * so that liability and evidence are handled." This queue is that evidence. It
 * had no reader: filing is unauthenticated and hands the xid to the CLAIMANT, so
 * `PATCH /admin/takedowns/{xid}` took an identifier nobody on this side of the
 * system had ever seen, and the trail ended at an INSERT.
 *
 * **Platform admin only, and not as a matter of seniority.** A takedown is an
 * allegation against a centre's material. A queue readable by that centre's own
 * staff is a notification service for "we are about to be caught", which is why
 * `Action.TAKEDOWN` is `{PLATFORM_ADMIN}` alone and why this screen refuses
 * rather than showing an empty table.
 *
 * **Oldest first, and this screen does not re-sort.** The order is the server's,
 * from a partial index built for it. The clock a rights holder cares about
 * started when they filed, and newest-first is how the request that has been
 * sitting for three weeks stays at the bottom.
 *
 * **A decision is made from the material, never from the status column.** Every
 * claim opens with the claimant, the rights basis, the description and the
 * subject on screen together, and the decision control stays inert until the
 * admin says they have read it. Four of the five decisions are final in
 * practice, and the outcome note is the only durable record of the reason —
 * `decide_takedown` overwrites `actioned_by` and `actioned_at` if it is called
 * twice and writes no audit row, so a decision recorded without a note is a
 * decision nobody can account for later.
 *
 * `POST /takedowns` is deliberately unauthenticated — a rights holder must not
 * need an account to file — so the filing form belongs on a public page. It is
 * not built here and must not be: an admin console form would put the one
 * account-free route in the product behind a sign-in.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { Status } from "../../app/Icon";
import { api, problemText } from "../../api/client";
import { isPlatformAdmin, loadPrincipal } from "../../api/principal";
import type { components } from "../../api/schema";
import { subjectLabel } from "./subjects";
import { DECISION_LIST, type Decision, isOpen, waitingDays } from "./decisions";

/** A queue row that has an xid — which every stored row does, but the contract
 *  types it optional and a decision cannot be addressed without one. */
type Claimed = components["schemas"]["TakedownQueueItem"] & { xid: string };

type StatusFilter =
  | "open" | "all" | "received" | "reviewing" | "upheld" | "rejected"
  | "counter_noticed" | "withdrawn";

const FILTERS: { value: StatusFilter; label: string }[] = [
  { value: "open", label: "Open — received and reviewing" },
  { value: "all", label: "Everything" },
  { value: "upheld", label: "Upheld" },
  { value: "rejected", label: "Rejected" },
  { value: "counter_noticed", label: "Counter-noticed" },
  { value: "withdrawn", label: "Withdrawn" },
];

export function Takedowns() {
  const queries = useQueryClient();
  const [filter, setFilter] = useState<StatusFilter>("open");
  const [opened, setOpened] = useState<string | null>(null);

  const principal = useQuery({
    queryKey: ["principal"],
    queryFn: loadPrincipal,
    staleTime: Infinity,
  });
  const admin = isPlatformAdmin(principal.data ?? null);

  const queue = useQuery({
    queryKey: ["takedowns", filter],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/admin/takedowns", {
        params: { query: { status: filter, limit: 200 } },
      });
      if (failure) throw failure;
      return data;
    },
    // Not fetched at all for anyone else. The refusal is the point of the
    // screen, and a 403 in the console's error line reads like a fault.
    enabled: admin,
  });

  if (principal.isPending) return <div className="page"><p className="muted">…</p></div>;

  if (!admin) {
    return (
      <div className="page">
        <h1>Takedowns</h1>
        <p className="muted">
          You do not have access to this. Copyright claims are read and decided by
          the platform, not by centres — a claim names a centre's material, and
          the centre it names must not be the one reading it.
        </p>
      </div>
    );
  }

  const items = queue.data?.items ?? [];
  const now = new Date();

  return (
    <div className="page">
      <h1>Takedowns</h1>
      <p className="muted">
        Copyright claims, oldest first. The order is the queue's own and this page
        keeps it: the wait is the thing a rights holder measures.
      </p>

      <label htmlFor="t-filter">Show</label>
      <select
        id="t-filter"
        value={filter}
        onChange={(event) => {
          setFilter(event.target.value as StatusFilter);
          setOpened(null);
        }}
      >
        {FILTERS.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>

      {queue.isError && <p className="error">{problemText(queue.error)}</p>}

      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th>Claimant</th>
              <th>Material</th>
              <th>Waiting</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((row) => {
              const waiting = waitingDays(row.received_at, now);
              return (
                <tr key={row.xid}>
                  <td>
                    {row.claimant_name}
                    {row.claimant_org && (
                      <span className="muted"> · {row.claimant_org}</span>
                    )}
                  </td>
                  <td>
                    {row.subject_title || <span className="muted">Not resolved</span>}
                    <span className="muted"> · {subjectLabel(row.subject_type)}</span>
                  </td>
                  <td className="num">
                    {waiting === null
                      ? "—"
                      : waiting === 0
                        ? "Today"
                        : `${waiting} day${waiting === 1 ? "" : "s"}`}
                  </td>
                  <td><Status value={row.status} /></td>
                  <td>
                    <button
                      className="link"
                      onClick={() =>
                        setOpened(opened === row.xid ? null : (row.xid ?? null))}
                    >
                      {opened === row.xid ? "Close" : "Read"}
                    </button>
                  </td>
                </tr>
              );
            })}
            {items.length === 0 && !queue.isPending && (
              <tr>
                <td colSpan={5} className="muted">
                  {filter === "open"
                    ? "No open claims."
                    : "Nothing matches that status."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {items
        .filter((row): row is Claimed => Boolean(row.xid) && row.xid === opened)
        .map((row) => (
          <Claim
            key={row.xid}
            claim={row}
            onDecided={() => {
              void queries.invalidateQueries({ queryKey: ["takedowns"] });
            }}
          />
        ))}
    </div>
  );
}

/**
 * One claim, whole, and the decision.
 *
 * Everything the queue carries is shown at once rather than behind tabs, because
 * the failure this screen exists to prevent is a decision made from a status
 * column. The description is the claimant's own words about what was copied and
 * it is the part that actually decides most of these.
 */
function Claim({ claim, onDecided }: {
  claim: Claimed;
  onDecided: () => void;
}) {
  const [decision, setDecision] = useState<Decision>("reviewing");
  const [note, setNote] = useState("");
  const [read, setRead] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  const chosen = DECISION_LIST.find((option) => option.value === decision);
  // `reviewing` leaves the request in the queue and can be revisited; the other
  // four end it, and `decide_takedown` writes no audit row, so this note is the
  // only lasting record of why.
  const closes = !isOpen(decision);

  const decide = useMutation({
    mutationFn: async () => {
      if (closes && !note.trim()) {
        throw new Error("Say why. This note is the only record of the reason "
                        + "that outlives the decision.");
      }
      const { error: failure } = await api.PATCH("/admin/takedowns/{xid}", {
        params: { path: { xid: claim.xid } },
        body: {
          status: decision,
          ...(note.trim() ? { outcome_note: note.trim() } : {}),
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setFailed(null);
      onDecided();
    },
    onError: (failure) => setFailed(problemText(failure) || String(failure)),
  });

  return (
    <div className="issued">
      <h2>The claim</h2>
      {failed && <p className="error">{failed}</p>}

      <div className="scroll">
        <table>
          <tbody>
            <tr>
              <td>Claimant</td>
              <td>
                {claim.claimant_name}
                {claim.claimant_org && <> · {claim.claimant_org}</>}
                <div className="muted">{claim.claimant_email}</div>
              </td>
            </tr>
            <tr>
              <td>Rights claimed</td>
              <td>{claim.rights_basis}</td>
            </tr>
            <tr>
              <td>Material</td>
              <td>
                {claim.subject_title || <span className="muted">Not resolved</span>}
                <span className="muted"> · {subjectLabel(claim.subject_type)}</span>
                {/* A per-item route exists for tests and for nothing else, so this
                    is offered where it works and the identifier is given plainly
                    where it does not. A link that lands on a library listing of
                    four hundred rows is not a way to look at the material. */}
                {claim.subject_type === "test" && claim.subject_xid && (
                  <div>
                    <Link className="link" to={`/tests/${claim.subject_xid}`}>
                      Open it
                    </Link>
                  </div>
                )}
                {!claim.subject_xid && (
                  <div className="muted">
                    The subject named at filing does not exist. Decide on the
                    description alone, or ask the claimant.
                  </div>
                )}
                {claim.subject_xid && claim.subject_type !== "test" && (
                  <div className="muted">{claim.subject_xid}</div>
                )}
              </td>
            </tr>
            <tr>
              <td>Filed</td>
              <td className="muted">
                {claim.received_at
                  ? new Date(claim.received_at).toLocaleString()
                  : "—"}
              </td>
            </tr>
            <tr>
              <td>Status</td>
              <td>
                {claim.status}
                {claim.outcome_note && (
                  <div className="muted">{claim.outcome_note}</div>
                )}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <blockquote>{claim.description}</blockquote>

      <p className="muted">
        {/* Not a per-claim field: `file_takedown` refuses a request whose
            `sworn_statement` is false, so every row that exists here has one.
            The queue does not return the column, and inventing a tick from its
            absence would be showing a guess as a fact. */}
        Filing required a sworn statement — a request without one is refused
        before it reaches this queue.
      </p>
      <p className="muted">
        {claim.hidden_at
          ? `A hide was recorded against this request on ${
            new Date(claim.hidden_at).toLocaleDateString()}. `
          : "No hide was recorded against this request. "}
        Nothing in the platform acts on that timestamp yet, so treat the material
        as still reachable. It is never deleted while a claim is open — destroying
        it would destroy the evidence with it.
      </p>

      <h2>Decide</h2>
      <label htmlFor={`d-${claim.xid}`}>Decision</label>
      <select
        id={`d-${claim.xid}`}
        value={decision}
        onChange={(event) => setDecision(event.target.value as Decision)}
      >
        {DECISION_LIST.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
      {chosen && <p className="muted">{chosen.help}</p>}

      <label htmlFor={`n-${claim.xid}`}>
        Outcome note{closes ? " (required)" : " (optional)"}
      </label>
      <textarea
        id={`n-${claim.xid}`}
        rows={3}
        value={note}
        onChange={(event) => setNote(event.target.value)}
        placeholder="Compared against Cambridge IELTS 17 Test 2; the passage is reproduced word for word."
      />

      <label className="choice">
        <input
          type="checkbox"
          checked={read}
          onChange={(event) => setRead(event.target.checked)}
        />
        I have read this claim and looked at the material
      </label>
      <button
        onClick={() => decide.mutate()}
        /* The gate, not ceremony. Upholding a claim says a centre infringed
           copyright and rejecting one tells a rights holder they were wrong;
           both are answers somebody has to be willing to give in writing. */
        disabled={decide.isPending || !read}
      >
        {decide.isPending ? "Recording…" : "Record this decision"}
      </button>
    </div>
  );
}
