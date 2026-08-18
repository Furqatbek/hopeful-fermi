/**
 * What a centre holds — granting it, and taking it back.
 *
 * `entitlements.revoked_at` was read by every access decision in the product and
 * written by nothing: `entitlements` is the one table the whole product asks "is
 * this allowed" against, both readers filter `revoked_at IS NULL`, and the
 * column could never be anything else. A payment reversed at the bank left the
 * feature switched on for ever.
 *
 * **The listing is half the fix and the more easily forgotten half.**
 * `GET /me/entitlements` is scoped to the actor, so before this panel a platform
 * admin could not see what any centre had bought and had no way to reach the id
 * the revoke takes. An action with no way to find its subject is the shape
 * `check_console_coverage.py` exists to catch, and shipping the revoke alone
 * would have been a fresh instance of it.
 *
 * Revoked rows stay on the list, greyed, with their reason. This is the screen a
 * billing dispute is argued from — "we were charged after we cancelled" is
 * answered by the dates and the note, and a row that vanishes answers it with
 * nothing.
 *
 * ── granting ────────────────────────────────────────────────────────────────
 *
 * The other half, and the panel shipped with only the destructive one. Its own
 * empty state said "no seat licence has been granted" and offered no way to
 * grant one: switching a pilot centre on meant an operator running curl against
 * the table the whole product asks "is this allowed" of. That is the shape of a
 * first sale in this market — a centre trials the product for a term before
 * anybody signs anything.
 *
 * **Seats are not a stronger grant, they are a narrower one.** Every other kind
 * covers every member of the centre; a seat covers only the students the centre
 * has seated, and it is the ONLY kind that does. So it is the one choice on this
 * form that changes who is affected rather than where the row came from, and it
 * is the one that asks a follow-up question — how many.
 *
 * The rules a seat must satisfy live on the server (`_seat_grant_rules`) and are
 * not copied here. This form shapes the request so the ordinary path is right
 * and lets the 409 explain the rest: a second copy of "what makes a seat usable"
 * in TypeScript is a thing that can disagree with the first, and the entire
 * defect being fixed underneath this screen is two places that disagreed about
 * what a seat was.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { hasLiveSeatLicence, quantityLabel, shadowedFeatures } from "./seatLicences";

/** The four the endpoint accepts. `order` is absent there and so here: a row
 *  claiming a payment must be able to name one, and nothing on this screen
 *  takes money. */
const KINDS = [
  { value: "trial", label: "Trial — a pilot term, agreed but unsigned" },
  { value: "seat", label: "Seat licence — metered per student" },
  { value: "promo", label: "Promotion — a conference or launch offer" },
  { value: "manual_grant", label: "Manual grant — anything else, say why" },
] as const;

type Kind = (typeof KINDS)[number]["value"];

export function OrgEntitlements({ orgXid, name }: { orgXid: string; name: string }) {
  const queries = useQueryClient();
  const [revoking, setRevoking] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const [granting, setGranting] = useState(false);
  const [kind, setKind] = useState<Kind>("trial");
  const [feature, setFeature] = useState("mock.unlimited");
  const [seats, setSeats] = useState("");
  const [why, setWhy] = useState("");
  const [until, setUntil] = useState("");
  const [grantError, setGrantError] = useState<string | null>(null);

  const grant = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/admin/entitlements", {
        body: {
          subject_kind: "org",
          subject_xid: orgXid,
          feature: feature.trim(),
          source_kind: kind,
          reason: why.trim(),
          // A seat count is the seat licence's whole size. Sent for a seat and
          // omitted otherwise, rather than sent as 0 or 1: on any other kind a
          // number here means "a consumable balance", and one that nobody
          // typed would quietly cap a trial at a single use.
          ...(kind === "seat" ? { quantity: Number(seats) } : {}),
          // Midnight local, at the end of the named day — a term that "runs to
          // the 30th" includes the 30th, and `<input type="date">` gives the
          // start of it.
          ...(until ? { expires_at: new Date(`${until}T23:59:59`).toISOString() } : {}),
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setGranting(false);
      setGrantError(null);
      setSeats("");
      setWhy("");
      setUntil("");
      void queries.invalidateQueries({ queryKey: ["admin-entitlements", orgXid] });
      // And the centre's own view of what it holds, and the seats screen, which
      // reads the licence this may have just created.
      void queries.invalidateQueries({ queryKey: ["entitlements"] });
      void queries.invalidateQueries({ queryKey: ["seats"] });
    },
    onError: (failure) => setGrantError(problemText(failure) || String(failure)),
  });

  const seatCount = Number(seats);
  const incomplete =
    feature.trim() === "" ||
    why.trim() === "" ||
    (kind === "seat" && !(Number.isInteger(seatCount) && seatCount >= 1));

  const held = useQuery({
    queryKey: ["admin-entitlements", orgXid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET(
        "/admin/orgs/{xid}/entitlements", { params: { path: { xid: orgXid } } });
      if (failure) throw failure;
      return data;
    },
  });

  const revoke = useMutation({
    mutationFn: async (xid: string) => {
      const { error: failure } = await api.POST("/admin/entitlements/{xid}/revoke", {
        params: { path: { xid } },
        body: { reason },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setRevoking(null);
      setReason("");
      setError(null);
      void queries.invalidateQueries({ queryKey: ["admin-entitlements", orgXid] });
      // The centre's own view of what it holds is now wrong.
      void queries.invalidateQueries({ queryKey: ["entitlements"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const rows = held.data ?? [];
  const shadowed = shadowedFeatures(rows);

  return (
    <div className="panel">
      <h3>{name} — entitlements</h3>
      {held.isError && <p className="error">{problemText(held.error)}</p>}
      {held.data?.length === 0 && (
        <p className="muted">This centre holds nothing. Nothing has been bought
          for it, and no seat licence has been granted.</p>
      )}
      {hasLiveSeatLicence(rows) && (
        <p className="muted">
          A seat licence covers only the students this centre has seated against
          it. Who holds a seat is the centre&rsquo;s own decision, on their seats
          screen — this panel decides how many there are.
        </p>
      )}
      {shadowed.length > 0 && (
        // Found by rendering it: granting twice is easy from this form and the
        // list showed two live licences as though a centre had both. It has one
        // — the seat lookup takes a single row, newest live first, because a
        // centre that renews has two and last year's must not be the answer. So
        // the older one is not extra seats, it is nothing at all, and a panel
        // that lets an operator read it as capacity will produce a centre told
        // it has four seats and refused on the second.
        <p className="error">
          More than one live seat licence for {shadowed.join(", ")}. Only the
          most recent is in force — the others grant nothing at all. Revoke
          them, or grant a single licence for the total.
        </p>
      )}
      {held.data && held.data.length > 0 && (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>What</th><th>How</th><th>Left</th><th>Runs to</th><th>State</th><th />
              </tr>
            </thead>
            <tbody>
              {held.data.map((row) => (
                <tr key={row.xid} className={row.revoked_at ? "muted" : undefined}>
                  <td>{row.feature}</td>
                  <td className="muted">{row.source_kind}</td>
                  <td className="num">{quantityLabel(row)}</td>
                  <td className="muted">
                    {row.expires_at
                      ? new Date(row.expires_at).toLocaleDateString()
                      : "no end date"}
                  </td>
                  <td className="muted">
                    {row.revoked_at
                      ? `revoked ${new Date(row.revoked_at).toLocaleDateString()}` +
                        (row.revoked_reason ? ` — ${row.revoked_reason}` : "")
                      : "live"}
                  </td>
                  <td>
                    {row.revoked_at ? null : revoking === row.xid ? (
                      <span className="row">
                        <input value={reason} autoFocus
                               placeholder="Why — a chargeback, a refund, a mistake"
                               onChange={(event) => setReason(event.target.value)} />
                        {/* Required by the endpoint and required here, rather
                            than letting the 422 teach it. This note is what a
                            centre is shown when it asks why access stopped. */}
                        <button className="link"
                                disabled={revoke.isPending || reason.trim() === ""}
                                onClick={() => revoke.mutate(row.xid)}>
                          {revoke.isPending ? "Revoking…" : "Confirm"}
                        </button>
                        <button className="link" onClick={() => {
                          setRevoking(null);
                          setReason("");
                        }}>Cancel</button>
                      </span>
                    ) : (
                      <button className="link" onClick={() => setRevoking(row.xid)}>
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {error && <p className="error">{error}</p>}

      {granting ? (
        <form onSubmit={(event) => {
          event.preventDefault();
          grant.mutate();
        }}>
          <h4>Grant {name} something, without a payment</h4>

          <label htmlFor="grant-kind">What kind</label>
          <select id="grant-kind" value={kind}
                  onChange={(event) => setKind(event.target.value as Kind)}>
            {KINDS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>

          <label htmlFor="grant-feature">Feature</label>
          <input id="grant-feature" value={feature} required
                 onChange={(event) => setFeature(event.target.value)} />
          <p className="muted">
            {kind === "seat"
              ? "Seats are sold against mock sittings. Any other feature is " +
                "refused: a seat nobody can be assigned to covers nobody, and " +
                "would read as granted."
              : "org.assignments lets the centre set work; mock.unlimited " +
                "covers the students sitting it. Setting an assignment needs " +
                "both, so a trial usually means granting this twice."}
          </p>

          {kind === "seat" && (
            <>
              <label htmlFor="grant-seats">How many seats</label>
              <input id="grant-seats" type="number" min={1} step={1} required
                     value={seats} inputMode="numeric"
                     onChange={(event) => setSeats(event.target.value)} />
            </>
          )}

          <label htmlFor="grant-why">Why</label>
          {/* Required by the endpoint and required here. It is stored on the row
              as well as in the audit log, because whoever later asks "why does
              this centre have this" is reading the entitlement. */}
          <input id="grant-why" value={why} required
                 placeholder="Pilot, one term, agreed by phone with the director"
                 onChange={(event) => setWhy(event.target.value)} />

          <label htmlFor="grant-until">Runs to (optional)</label>
          <input id="grant-until" type="date" value={until}
                 min={new Date().toISOString().slice(0, 10)}
                 onChange={(event) => setUntil(event.target.value)} />
          <p className="muted">
            Leave empty for no end date. A trial with no end date is one somebody
            has to remember to revoke.
          </p>

          {grantError && <p className="error">{grantError}</p>}
          <span className="row">
            <button disabled={grant.isPending || incomplete}>
              {grant.isPending ? "Granting…" : "Grant"}
            </button>
            <button type="button" className="link" onClick={() => {
              setGranting(false);
              setGrantError(null);
            }}>Cancel</button>
          </span>
        </form>
      ) : (
        <button className="link" onClick={() => setGranting(true)}>
          Grant something
        </button>
      )}

      <p className="muted">
        Revoking takes effect on the next request — access is resolved per call,
        not cached in a token. Revoked rows stay listed with their reason,
        because this is what a billing dispute is settled against.
      </p>
    </div>
  );
}
