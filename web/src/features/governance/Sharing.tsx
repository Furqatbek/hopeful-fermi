/**
 * Who can see this.
 *
 * "Centre-created content defaults to organization-private. A centre's material
 * must never leak to a competitor centre" is a contractual promise, and a
 * promise nobody can check is not one. Until `GET /content-grants` existed the
 * only way to hold a grant's xid was to have created it, so the answer to *who
 * can see this* lived in a database console — and revoking a grant meant having
 * kept the response from the day it was made.
 *
 * **Two directions, because they are two questions.** `granted` is what we let
 * out; `received` is what we may use. A centre needs both and they are not each
 * other's mirror: the receiving centre sees a grant as received and never as
 * granted, because it was not the party that let the material out.
 *
 * **Public is not a choice a centre gets to make.** `grantee_kind: public` is
 * refused for anyone but a platform admin, and that single rule is most of the
 * copyright containment in this product — content cannot become world-visible
 * without a platform review. So the option is absent with its reason written
 * next to it, rather than present and answered with `public_share_not_permitted`
 * after the admin has filled in the form.
 *
 * **Revoke is offered only on what we granted.** Revoking checks `Action.SHARE`
 * on the SUBJECT, which the grantee does not have — they are precisely the party
 * that holds the xid and must not be able to end the arrangement. And a revoke
 * can answer 404: the grant may already be gone, from another tab or another
 * admin, so a failure here refreshes the listing rather than insisting.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { isPlatformAdmin, loadPrincipal } from "../../api/principal";
import { SUBJECT_TYPES, type SubjectType, subjectLabel } from "./subjects";

type Direction = "granted" | "received";
type GranteeKind = "org" | "user" | "public";
type Permission = "view" | "assign" | "copy";

const PERMISSIONS: { value: Permission; label: string }[] = [
  { value: "view", label: "View — read it" },
  { value: "assign", label: "Assign — set it as work" },
  { value: "copy", label: "Copy — take a copy into their own library" },
];

/**
 * The candidate subjects of one type, as `{ xid, label }`.
 *
 * A switch rather than one path built from a string, because the seven listings
 * are genuinely not one endpoint: five are paged and two are bare arrays, the
 * label is `title` for most, `name` for a band map and `type_key` for a question
 * — questions have no title column at all. The generated client types each of
 * those separately, which is what makes writing them out safer than clever.
 */
async function loadSubjects(
  type: SubjectType,
): Promise<{ xid: string; label: string }[]> {
  const named = <T extends { xid?: string }>(
    rows: T[] | undefined,
    label: (row: T) => string,
  ) =>
    (rows ?? [])
      .filter((row): row is T & { xid: string } => Boolean(row.xid))
      .map((row) => ({ xid: row.xid, label: label(row) }));

  switch (type) {
    case "test": {
      const { data, error } = await api.GET("/tests", {
        params: { query: { limit: 100 } },
      });
      if (error) throw error;
      return named(data.items, (row) => row.title);
    }
    case "passage": {
      const { data, error } = await api.GET("/passages", {
        params: { query: { limit: 100 } },
      });
      if (error) throw error;
      return named(data.items, (row) => row.title);
    }
    case "audio_track": {
      const { data, error } = await api.GET("/audio-tracks", {
        params: { query: { limit: 100 } },
      });
      if (error) throw error;
      return named(data.items, (row) => row.title);
    }
    case "question_group": {
      const { data, error } = await api.GET("/question-groups", {
        params: { query: { limit: 100 } },
      });
      if (error) throw error;
      return named(data.items, (row) => row.title ?? "Untitled group");
    }
    case "question": {
      const { data, error } = await api.GET("/questions", {
        params: { query: { limit: 100 } },
      });
      if (error) throw error;
      return named(data.items, (row) =>
        `${row.type_key} · v${row.current_version?.version_no ?? "?"}`);
    }
    case "cue_card_set": {
      const { data, error } = await api.GET("/cue-card-sets");
      if (error) throw error;
      return named(data, (row) => row.title ?? "Untitled set");
    }
    case "band_map": {
      const { data, error } = await api.GET("/band-maps");
      if (error) throw error;
      return named(data, (row) => row.name ?? "Untitled band map");
    }
  }
}

export function Sharing() {
  const queries = useQueryClient();
  const [direction, setDirection] = useState<Direction>("granted");
  const [subjectType, setSubjectType] = useState<SubjectType>("test");
  const [subjectXid, setSubjectXid] = useState("");
  const [granteeKind, setGranteeKind] = useState<GranteeKind>("org");
  const [granteeXid, setGranteeXid] = useState("");
  const [permission, setPermission] = useState<Permission>("view");
  const [expires, setExpires] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  const principal = useQuery({
    queryKey: ["principal"],
    queryFn: loadPrincipal,
    staleTime: Infinity,
  });
  const admin = isPlatformAdmin(principal.data ?? null);
  // `Action.SHARE` is `{CENTRE_ADMIN, PLATFORM_ADMIN}`. A teacher authors
  // content but does not decide who outside the centre may have it, and the
  // server would answer `share_not_permitted` — so the form is absent with the
  // reason given rather than present and refused.
  const mayShare = admin || (principal.data?.memberships ?? []).some(
    (m) => m.role === "centre_admin" && m.status === "active");

  const grants = useQuery({
    queryKey: ["content-grants", direction],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/content-grants", {
        params: { query: { direction, limit: 200 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const subjects = useQuery({
    queryKey: ["governance-subjects", subjectType],
    queryFn: () => loadSubjects(subjectType),
  });

  const share = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/content-grants", {
        body: {
          subject_type: subjectType,
          subject_xid: subjectXid,
          grantee_kind: granteeKind,
          // Absent for `public`, which names no grantee at all. Sending an empty
          // string would fail uuid parsing with a message about a field the
          // admin was never shown.
          ...(granteeKind === "public" ? {} : { grantee_xid: granteeXid.trim() }),
          permission,
          // A datetime-local input is in the browser's zone; the server stores
          // and compares in UTC.
          ...(expires ? { expires_at: new Date(expires).toISOString() } : {}),
          ...(note.trim() ? { note: note.trim() } : {}),
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      setSubjectXid("");
      setGranteeXid("");
      setNote("");
      setExpires("");
      void queries.invalidateQueries({ queryKey: ["content-grants"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const revoke = useMutation({
    mutationFn: async (xid: string) => {
      const { error: failure } = await api.DELETE("/content-grants/{xid}", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      void queries.invalidateQueries({ queryKey: ["content-grants"] });
    },
    onError: (failure) => {
      setError(problemText(failure));
      // A 204 is not the only outcome. The grant may already have been revoked
      // — another admin, another tab — and the server answers 404 for a row it
      // cannot find, so the row on screen is stale either way and refetching is
      // what makes the listing agree with the server again.
      void queries.invalidateQueries({ queryKey: ["content-grants"] });
    },
  });

  const rows = grants.data?.items ?? [];

  return (
    <div className="page">
      <h1>Sharing</h1>
      <p className="muted">
        Everything a centre authors stays inside that centre until somebody here
        shares it. This is the whole list, in both directions.
      </p>

      <div className="row">
        <label className="choice">
          <input
            type="radio"
            name="direction"
            checked={direction === "granted"}
            onChange={() => setDirection("granted")}
          />
          What we shared
        </label>
        <label className="choice">
          <input
            type="radio"
            name="direction"
            checked={direction === "received"}
            onChange={() => setDirection("received")}
          />
          Shared with us
        </label>
      </div>

      {error && <p className="error">{error}</p>}
      {grants.isError && <p className="error">{problemText(grants.error)}</p>}

      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th>Material</th>
              <th>{direction === "granted" ? "Shared with" : "Shared by"}</th>
              <th>Permission</th>
              <th>From</th>
              <th>Until</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              // Bound to a const so the `xid &&` guard below narrows inside the
              // revoke handler. A property read would widen back to
              // `string | undefined` in the closure.
              const xid = row.xid;
              return (
                <tr key={xid}>
                  <td>
                    {row.subject_title || <span className="muted">Untitled</span>}
                    <span className="muted"> · {subjectLabel(row.subject_type)}</span>
                  </td>
                  <td>
                    {row.grantee_name ?? <span className="muted">Not named</span>}
                    {row.grantee_kind === "public" && (
                      <span className="muted"> · anyone</span>
                    )}
                    {row.note && <div className="muted">{row.note}</div>}
                  </td>
                  <td>{row.permission}</td>
                  <td className="muted">
                    {row.granted_at
                      ? new Date(row.granted_at).toLocaleDateString()
                      : "—"}
                  </td>
                  <td className="muted">
                    {row.expires_at
                      ? new Date(row.expires_at).toLocaleDateString()
                      : "No end date"}
                  </td>
                  <td>
                    {/* Only what we granted. Revoking needs SHARE on the subject
                        and the grantee does not have it — offering the control on
                        a received grant is a button that can only answer 403. */}
                    {direction === "granted" && mayShare && xid && (
                      <button
                        className="link"
                        disabled={revoke.isPending}
                        onClick={() => revoke.mutate(xid)}
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  {direction === "granted"
                    ? mayShare
                      ? "Nothing has been shared outside this centre."
                      : "Only a centre admin can see what this centre has shared."
                    : "No other centre has shared anything with this one."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <h2>Share something</h2>
      {!mayShare ? (
        <p className="muted">
          You do not have access to this. Letting material out of the centre is a
          centre admin decision, not an author's.
        </p>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            setError(null);
            share.mutate();
          }}
        >
          <label htmlFor="g-type">What</label>
          <select
            id="g-type"
            value={subjectType}
            onChange={(event) => {
              setSubjectType(event.target.value as SubjectType);
              // The xid belongs to the old type's list. Keeping it would send a
              // passage's identifier as a test and get "Subject not found."
              setSubjectXid("");
            }}
          >
            {SUBJECT_TYPES.map((type) => (
              <option key={type} value={type}>{subjectLabel(type)}</option>
            ))}
          </select>

          <label htmlFor="g-subject">Which one</label>
          <select
            id="g-subject"
            value={subjectXid}
            onChange={(event) => setSubjectXid(event.target.value)}
            required
          >
            <option value="">— choose —</option>
            {(subjects.data ?? []).map((subject) => (
              <option key={subject.xid} value={subject.xid}>{subject.label}</option>
            ))}
          </select>
          {subjects.isError && (
            <p className="error">{problemText(subjects.error)}</p>
          )}
          {subjects.data?.length === 0 && (
            <p className="muted">
              This centre has no {subjectLabel(subjectType).toLowerCase()} to share.
            </p>
          )}

          <label htmlFor="g-kind">With</label>
          <select
            id="g-kind"
            value={granteeKind}
            onChange={(event) => {
              setGranteeKind(event.target.value as GranteeKind);
              // An organization identifier is not a person's.
              setGranteeXid("");
            }}
          >
            <option value="org">Another centre</option>
            <option value="user">One person</option>
            {/* Platform admin only, and absent rather than disabled for
                everybody else: `public` is refused with
                `public_share_not_permitted`, and a control that can only fail
                teaches an admin to distrust the screen. */}
            {admin && <option value="public">Everyone — published openly</option>}
          </select>
          {!admin && (
            <p className="muted">
              Sharing with everyone is not on this list. Material can only be made
              openly visible after a platform review, and that rule is most of what
              keeps a copyright claim from becoming a public one.
            </p>
          )}
          {granteeKind === "public" && (
            <p className="muted">
              This puts the material in front of anyone, including people with no
              account. It cannot be taken back from whoever has already seen it.
            </p>
          )}

          {granteeKind !== "public" && (
            <>
              <label htmlFor="g-grantee">
                {granteeKind === "org" ? "Their centre's identifier" : "Their identifier"}
              </label>
              <input
                id="g-grantee"
                value={granteeXid}
                onChange={(event) => setGranteeXid(event.target.value)}
                placeholder="00000000-0000-0000-0000-000000000000"
                required
              />
              <p className="muted">
                {/* `GET /orgs` is scoped to the actor's own memberships, so this
                    console has no directory of other centres to pick from — for
                    everybody except a platform admin it would list only their own
                    centre. Asking for the identifier is honest; a picker that
                    could only offer sharing with yourself is not. */}
                There is no directory of other centres here — ask them for their
                identifier. Nothing is shared until you press the button below.
              </p>
            </>
          )}

          <label htmlFor="g-permission">They may</label>
          <select
            id="g-permission"
            value={permission}
            onChange={(event) => setPermission(event.target.value as Permission)}
          >
            {PERMISSIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
          <p className="muted">
            {/* This used to say view and assign were recorded and unread, and
                it was true: `policy.filter_content` took a `grant_ids` argument
                no caller passed, so only `copy` did anything. `authz.grants` is
                the reader now, wired into the one place every listing is
                scoped, and the permissions are a hierarchy — so the copy below
                describes what the server actually does. */}
            The three build on each other. View lets the other centre open the
            material. Assign adds setting it as work for their own students.
            Copy adds taking their own editable copy, which is then theirs to
            change. Revoking takes the access away again.
          </p>

          <label htmlFor="g-expires">Until (optional)</label>
          <input
            id="g-expires"
            type="datetime-local"
            value={expires}
            onChange={(event) => setExpires(event.target.value)}
          />

          <label htmlFor="g-note">Why (optional)</label>
          <input
            id="g-note"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Pilot term, agreed with their director"
          />

          <button
            disabled={
              share.isPending
              || !subjectXid
              || (granteeKind !== "public" && !granteeXid.trim())
            }
          >
            {share.isPending ? "Sharing…" : "Share"}
          </button>
        </form>
      )}
    </div>
  );
}
