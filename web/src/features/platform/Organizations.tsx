/**
 * Creating an organization. Platform admin only.
 *
 * This is the one act in the product that nobody inside a centre can perform:
 * `POST /orgs` refuses anyone without the `platform_admin` grant with 403
 * `create_not_permitted`, and there is no self-serve path. The Centre screen
 * already tells a teacher with no membership that "only a platform admin can
 * create one" and left them with no idea where that happens; this is where.
 *
 * **A teacher opening this page is shown a sentence, not a disabled form.** The
 * refusal is not about a missing field or a permission they might earn by
 * asking — a centre cannot create the entity that governs it — so a form with a
 * greyed-out button would be an invitation to keep trying. `isPlatformAdmin`
 * decides what is DRAWN; the server decides what is allowed, and still refuses
 * regardless.
 *
 * **The slug is permanent.** `OrgUpdate` carries name, contact phone and
 * settings, so the only chance to get the web name right is this form. It is
 * suggested from the organization's name and always editable, and never
 * submitted when the suggestion came back empty — which is what happens for a
 * Cyrillic centre name.
 *
 * The listing reuses the shape `features/roster/Roster.tsx` already reads:
 * `{ items, next_cursor }` scoped by membership, and a platform admin sees
 * every organization on the platform.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Pager, usePaged } from "../../app/paging";
import { api, problemText } from "../../api/client";
import { OrgEntitlements } from "./OrgEntitlements";
import { isPlatformAdmin, loadPrincipal } from "../../api/principal";
import { slugProblem, suggestSlug } from "./slug";

const KINDS = ["prep_centre", "school", "university", "internal"] as const;

export function Organizations() {
  const queries = useQueryClient();
  const [name, setName] = useState("");
  // `null` means "follow the name"; a string means the admin has taken over.
  // Two states rather than comparing the box against the suggestion, because an
  // admin who deliberately types the suggestion back in has still taken over.
  const [slugEdit, setSlugEdit] = useState<string | null>(null);
  const [kind, setKind] = useState<(typeof KINDS)[number]>("prep_centre");
  const [legalName, setLegalName] = useState("");
  const [contactPhone, setContactPhone] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Which centre's billing is expanded. One at a time: the panel is a table and
  // two of them stacked reads as one list with a hidden boundary.
  const [open, setOpen] = useState<{ xid: string; name: string } | null>(null);
  const [created, setCreated] = useState<string | null>(null);

  const principal = useQuery({
    queryKey: ["principal"],
    queryFn: loadPrincipal,
    staleTime: Infinity,
  });
  const admin = isPlatformAdmin(principal.data ?? null);

  const orgs = usePaged(["orgs"], async (cursor) => {
    const { data, error: failure } = await api.GET("/orgs", {
      params: { query: { limit: 25, ...(cursor ? { cursor } : {}) } },
    });
    if (failure) throw failure;
    return data ?? {};
  });

  const slug = slugEdit ?? suggestSlug(name);
  // Not on an untouched form. `suggestSlug("")` is `""` and `slugProblem("")`
  // is "Give a short name for the web address", so this screen opened with a
  // red error block against a field nobody had typed in yet — telling a
  // platform admin they had got something wrong before they had done anything.
  // The message is right once they are typing and the slug is genuinely
  // unusable; it is only the pristine case that was wrong.
  const touched = name !== "" || slugEdit !== null;
  const badSlug = touched ? slugProblem(slug) : null;

  const create = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST("/orgs", {
        body: {
          name: name.trim(),
          slug,
          kind,
          ...(legalName.trim() ? { legal_name: legalName.trim() } : {}),
          ...(contactPhone.trim() ? { contact_phone: contactPhone.trim() } : {}),
        },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      setCreated(data?.name ?? name.trim());
      setName("");
      setSlugEdit(null);
      setLegalName("");
      setContactPhone("");
      void queries.invalidateQueries({ queryKey: ["orgs"] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  if (principal.isPending) return <div className="page muted">Loading…</div>;

  if (!admin) {
    return (
      <div className="page">
        <h1>Organizations</h1>
        <p className="muted">
          You do not have access to this. Creating an organization is a platform
          administrator's action, not something a centre does for itself. If a
          new centre needs setting up, ask the platform administrator to create
          it and invite you into it.
        </p>
      </div>
    );
  }

  return (
    <div className="page">
      <h1>Organizations</h1>
      {error && <p className="error">{error}</p>}
      {created && (
        <div className="issued">
          <p>
            <strong>{created}</strong> was created.
          </p>
          <p className="muted">
            It has no members yet. Invite its first centre admin from the Centre
            screen — an organization nobody belongs to cannot be administered by
            anybody but you.
          </p>
        </div>
      )}

      <h2>Create one</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          setCreated(null);
          if (!badSlug && name.trim()) create.mutate();
        }}
      >
        <label htmlFor="o-name">Name</label>
        <input
          id="o-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Tashkent Prep Centre"
          required
        />

        <label htmlFor="o-slug">Web name</label>
        <input
          id="o-slug"
          value={slug}
          onChange={(event) => setSlugEdit(event.target.value)}
          placeholder="tashkent-prep-centre"
          required
        />
        {badSlug ? (
          <p className="error">{badSlug}</p>
        ) : (
          <p className="muted">
            Permanent. Nothing changes it after this form — the endpoint that
            edits an organization does not carry it.
          </p>
        )}

        <label htmlFor="o-kind">Kind</label>
        <select
          id="o-kind"
          value={kind}
          onChange={(event) =>
            setKind(event.target.value as (typeof KINDS)[number])
          }
        >
          {KINDS.map((value) => (
            <option key={value} value={value}>{value.replaceAll("_", " ")}</option>
          ))}
        </select>

        <label htmlFor="o-legal">Legal name (optional)</label>
        <input
          id="o-legal"
          value={legalName}
          onChange={(event) => setLegalName(event.target.value)}
          placeholder="MChJ Tashkent Prep"
        />

        <label htmlFor="o-phone">Contact phone (optional)</label>
        <input
          id="o-phone"
          value={contactPhone}
          onChange={(event) => setContactPhone(event.target.value)}
          placeholder="+998901234567"
        />

        <button disabled={create.isPending || badSlug !== null || !name.trim()}>
          {create.isPending ? "Creating…" : "Create organization"}
        </button>
      </form>
      <p className="muted">
        {/* `create_org` writes `status="active"`, not the model default of
            "pending" — so there is no approval step to wait for and no control
            here for one. Said plainly because a platform admin would otherwise
            look for the step that activates it. */}
        A new organization is active immediately. There is no approval step.
      </p>

      <h2>On the platform</h2>
      {orgs.isError && <p className="error">{problemText(orgs.error)}</p>}
      <div className="scroll">
        <table>
          <thead>
            <tr><th>Name</th><th>Web name</th><th>Kind</th><th>Status</th><th /></tr>
          </thead>
          <tbody>
            {orgs.items.map((org) => (
              <tr key={org.xid}>
                <td>{org.name}</td>
                <td className="muted">{org.slug}</td>
                <td className="muted">{org.kind.replaceAll("_", " ")}</td>
                <td className="muted">{org.status}</td>
                <td>
                  <button className="link" onClick={() => setOpen(
                    open?.xid === org.xid ? null : { xid: org.xid, name: org.name })}>
                    {open?.xid === org.xid ? "Hide billing" : "Billing"}
                  </button>
                </td>
              </tr>
            ))}
            {orgs.items.length === 0 && (
              <tr><td colSpan={5} className="muted">No organizations yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <Pager shown={orgs.items.length} hasMore={orgs.hasMore}
             onMore={orgs.more} loading={orgs.loadingMore}
             noun="organizations" />
      <p className="muted">
        {/* The listing takes `limit` and returns `next_cursor`, which the handler
            hardcodes to null — there is no second page to fetch even when there
            are more than 25 rows. Stated rather than paginated against a cursor
            that is never issued. */}
        The first 25. This listing does not page.
      </p>

      {open && <OrgEntitlements orgXid={open.xid} name={open.name} />}
    </div>
  );
}
