/**
 * Opening a speaking slot.
 *
 * One rule dominates the speaking module and it is not a UI behaviour: **a minor
 * is never matched with an adult.** It is enforced three times server-side — the
 * slot list filters on `age_band` against the caller's own date of birth, booking
 * re-checks it, and the live-queue index leads with it — and nothing on this
 * screen relaxes any of that. What this screen owes the rule is the one thing
 * the server cannot do: make the choice deliberate at the moment it is made.
 *
 * So `age_band` is a set of radio buttons with no default and no submit until
 * one is chosen, and each states who it locks out. A dropdown that starts on a
 * value is a choice nobody made, and this is the field that decides which
 * children may be in a voice call with which adults.
 *
 * **The list below is the bookable list, not a list of what you created.**
 * `GET /speaking/slots` answers "what may I book", so it is filtered for the
 * caller: sessions for their own age group, public ones, their centre's, and
 * class sessions only for members of that class. Measured against the running
 * API: a teacher who opens a class session and is not on that class's roster
 * gets `[]` back. There is no endpoint that lists the slots an account created,
 * so this screen shows what the API can show and says plainly what it cannot,
 * and the panel after a save keeps the new session's details on screen.
 *
 * **The band range is not the filter it looks like.** `band_min`/`band_max` are
 * checked against a student's MEASURED band — the mean of their last few scored
 * mocks — and a student who has sat no mock has no band and is excluded by no
 * range. Measured: a student with no attempts is shown a "band 6.5-7.5" session.
 * Early in a term that is most of a new intake, so a narrow range hides the
 * session from the measured students outside it and hides it from nobody else.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import {
  ageBandLabel,
  appearsInMyList,
  rangeLabel,
  slotProblems,
  type AgeBand,
  type Audience,
  type Creator,
  type SlotDraft,
} from "./slotRules";

/** Each band with the consequence of choosing it. The second half of every one
 *  of these is who it keeps OUT, because that is what the field decides. */
const AGE_CHOICES: readonly { value: AgeBand; label: string; effect: string }[] = [
  {
    value: "minor",
    label: "Under 18 only",
    effect:
      "No adult account can see or book this session. Choose this for a class of " +
      "school-age students.",
  },
  {
    value: "adult",
    label: "18 and over only",
    effect:
      "No account under 18 can see or book this session, whatever it asks for.",
  },
  {
    value: "mixed_supervised",
    label: "Mixed ages, with a teacher present",
    effect:
      "Both age groups in one room. Only allowed for a class session, and only " +
      "because a teacher is supervising it. Do not choose it for a session you " +
      "will not attend.",
  },
];

const AUDIENCES: readonly { value: Audience; label: string; note: string }[] = [
  { value: "public", label: "Anyone on the platform",
    note: "Open to every student whose age group and band match." },
  { value: "org", label: "Your centre",
    note: "Only students who belong to your centre." },
  { value: "cohort", label: "One class",
    note: "Only students on that class's roster." },
];

export function Slots() {
  const queries = useQueryClient();
  const [startsAt, setStartsAt] = useState("");
  const [duration, setDuration] = useState(15);
  const [capacity, setCapacity] = useState(20);
  const [audience, setAudience] = useState<Audience>("public");
  const [cohortXid, setCohortXid] = useState("");
  const [ageBand, setAgeBand] = useState<AgeBand | "">("");
  const [bandMin, setBandMin] = useState("");
  const [bandMax, setBandMax] = useState("");
  const [cueCards, setCueCards] = useState("");
  const [filter, setFilter] = useState<Audience | "">("");
  const [made, setMade] = useState<
    { xid: string; ageBand: string; audience: string; startsAt: string;
      shown: boolean; reason: string } | null
  >(null);
  const [error, setError] = useState<string | null>(null);

  // `/auth/session` directly rather than `api/principal.ts`, which narrows the
  // response to `user`, `memberships` and `platform_roles`. This screen needs
  // `is_minor`: the slot list is filtered by the CALLER's age band, so whether a
  // session will come back is a question about the person looking, not about the
  // session.
  const session = useQuery({
    queryKey: ["auth-session"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/auth/session");
      if (failure) throw failure;
      return data;
    },
    staleTime: Infinity,
  });

  const orgs = useQuery({
    queryKey: ["orgs"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/orgs", {
        params: { query: { limit: 25 } },
      });
      if (failure) throw failure;
      return data;
    },
  });
  const orgXid = orgs.data?.items?.[0]?.xid;

  const cohorts = useQuery({
    queryKey: ["cohorts", orgXid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/orgs/{xid}/cohorts", {
        params: { path: { xid: orgXid! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(orgXid),
  });

  const sets = useQuery({
    queryKey: ["cue-card-sets"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/cue-card-sets");
      if (failure) throw failure;
      return data;
    },
  });

  // No `from`: the contract declares that query parameter and the handler's is
  // named `from_`, so sending `from` is accepted and ignored. Measured — with a
  // slot two days in the past, `?from=` returned nothing and `?from_=` returned
  // it. A control that silently does nothing is worse than no control.
  const slots = useQuery({
    queryKey: ["speaking-slots", filter],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/speaking/slots", {
        params: { query: filter ? { audience: filter } : {} },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const creator: Creator = {
    isPlatformAdmin: (session.data?.platform_roles ?? []).includes("platform_admin"),
    hasOrg: (session.data?.memberships ?? []).some((m) => m.status === "active"),
    isMinor: session.data?.is_minor ?? false,
  };
  const draft: SlotDraft = { startsAt, ageBand, audience, cohortXid, bandMin, bandMax };
  const problems = slotProblems(draft, creator);

  const create = useMutation({
    mutationFn: async () => {
      if (ageBand === "") throw new Error("Choose who this session is for.");
      // Built up rather than declared with undefined members: the project runs
      // `exactOptionalPropertyTypes`, so an absent field and a field set to
      // undefined are different things, and the server treats a null
      // `cohort_xid` on a cohort slot as a 404.
      const payload: {
        starts_at: string; duration_minutes: number; capacity: number;
        audience: Audience; age_band: AgeBand; cohort_xid?: string;
        band_min?: number; band_max?: number; cue_card_set_version_xid?: string;
      } = {
        // The server is the authority on time. A datetime-local value is in the
        // browser's zone, so it is sent as an instant.
        starts_at: new Date(startsAt).toISOString(),
        duration_minutes: duration,
        capacity,
        audience,
        age_band: ageBand,
      };
      if (audience === "cohort" && cohortXid) payload.cohort_xid = cohortXid;
      if (bandMin.trim() !== "") payload.band_min = Number(bandMin);
      if (bandMax.trim() !== "") payload.band_max = Number(bandMax);
      if (cueCards) payload.cue_card_set_version_xid = cueCards;

      const { data, error: failure } = await api.POST("/speaking/slots", {
        body: payload,
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      const reach = appearsInMyList(
        { audience: data?.audience, age_band: data?.age_band }, creator);
      setMade({
        xid: data?.xid ?? "", ageBand: data?.age_band ?? "",
        audience: data?.audience ?? "", startsAt: data?.starts_at ?? "",
        shown: reach.shown, reason: reach.reason,
      });
      setStartsAt("");
      setAgeBand("");
      setCueCards("");
      void queries.invalidateQueries({ queryKey: ["speaking-slots"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const attachable = (sets.data ?? []).filter((set) => set.current_version_xid);

  return (
    <div className="page">
      <h1>Speaking slots</h1>
      <p className="muted">
        A scheduled speaking session. Students book a place, and pairs are matched
        when the slot opens.
      </p>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          setMade(null);
          if (problems.length === 0) create.mutate();
        }}
      >
        <div className="row">
          <span>
            <label htmlFor="sl-start">Starts</label>
            <input
              id="sl-start"
              type="datetime-local"
              value={startsAt}
              onChange={(event) => setStartsAt(event.target.value)}
              required
            />
          </span>
          <span>
            <label htmlFor="sl-duration">Minutes</label>
            <input
              id="sl-duration"
              type="number"
              min={5}
              max={120}
              value={duration}
              onChange={(event) => setDuration(Number(event.target.value))}
            />
          </span>
          <span>
            <label htmlFor="sl-capacity">Places</label>
            <input
              id="sl-capacity"
              type="number"
              min={2}
              max={200}
              value={capacity}
              onChange={(event) => setCapacity(Number(event.target.value))}
            />
          </span>
        </div>

        <label htmlFor="sl-audience">Who can book</label>
        <select
          id="sl-audience"
          value={audience}
          onChange={(event) => setAudience(event.target.value as Audience)}
        >
          {AUDIENCES.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        <p className="muted">
          {AUDIENCES.find((option) => option.value === audience)?.note}
        </p>

        {audience === "cohort" && (
          <>
            <label htmlFor="sl-cohort">Class</label>
            <select
              id="sl-cohort"
              value={cohortXid}
              onChange={(event) => setCohortXid(event.target.value)}
            >
              <option value="">— choose a class —</option>
              {cohorts.data?.map((cohort) => (
                <option key={cohort.xid} value={cohort.xid}>
                  {cohort.name}
                  {cohort.member_count == null ? "" : ` · ${cohort.member_count}`}
                </option>
              ))}
            </select>
            {cohorts.data?.length === 0 && (
              <p className="muted">This centre has no classes yet.</p>
            )}
          </>
        )}

        <fieldset>
          <legend>Who is this session for?</legend>
          <p className="muted">
            This decides who may ever enter the session. Minors and adults are
            never paired for speaking practice, and the rule is applied when a
            student books, not by this screen. It cannot be changed afterwards.
          </p>
          {AGE_CHOICES.map((choice) => (
            <label key={choice.value} className="choice">
              <input
                type="radio"
                name="sl-age"
                value={choice.value}
                checked={ageBand === choice.value}
                onChange={() => setAgeBand(choice.value)}
              />
              <span>
                <strong>{choice.label}</strong>
                <br />
                <span className="muted">{choice.effect}</span>
              </span>
            </label>
          ))}
        </fieldset>

        <div className="row">
          <span>
            <label htmlFor="sl-bmin">Lowest band (optional)</label>
            <input
              id="sl-bmin"
              type="number"
              min={0}
              max={9}
              step={0.5}
              value={bandMin}
              onChange={(event) => setBandMin(event.target.value)}
            />
          </span>
          <span>
            <label htmlFor="sl-bmax">Highest band (optional)</label>
            <input
              id="sl-bmax"
              type="number"
              min={0}
              max={9}
              step={0.5}
              value={bandMax}
              onChange={(event) => setBandMax(event.target.value)}
            />
          </span>
        </div>
        <p className="muted">
          Checked against a student's measured band — the mean of their last few
          scored mocks — not the band they say they are aiming for. A student who
          has sat no mock has no measured band and is shown the session whatever
          you set here. Early in a term that is most of a new class, so a narrow
          range hides the session from your returning students and from nobody
          else. Leave both empty to invite everybody.
        </p>

        <label htmlFor="sl-cards">Cue card set (optional)</label>
        <select
          id="sl-cards"
          value={cueCards}
          onChange={(event) => setCueCards(event.target.value)}
        >
          <option value="">— no prompts —</option>
          {attachable.map((set) => (
            <option key={set.xid} value={set.current_version_xid}>{set.title}</option>
          ))}
        </select>
        <p className="muted">
          The prompts the session runs from, authored on the Cue cards screen. A
          session with none opens with nothing to talk about.
        </p>

        {problems.length > 0 && (startsAt || ageBand || cohortXid) && (
          <p className="error">{problems.join("\n")}</p>
        )}

        <button disabled={create.isPending || problems.length > 0}>
          {create.isPending ? "Opening…" : "Open slot"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}

      {made && (
        <div className="issued">
          <h2>Slot open</h2>
          <p>
            {new Date(made.startsAt).toLocaleString()} ·{" "}
            {ageBandLabel(made.ageBand)} ·{" "}
            {AUDIENCES.find((a) => a.value === made.audience)?.label ?? made.audience}
          </p>
          <p className="token">{made.xid}</p>
          {!made.shown && <p className="muted">{made.reason}</p>}
        </div>
      )}

      <h2>Slots you can book</h2>
      <p className="muted">
        This is the bookable list for your own account, which is the only listing
        the API offers. It shows sessions for your age group: public ones, your
        centre's, and class sessions for classes you are on the roster of. A
        session you opened for another age group, or for a class you are not in,
        will not be here.
      </p>

      <label htmlFor="sl-filter">Show</label>
      <select
        id="sl-filter"
        value={filter}
        onChange={(event) => setFilter(event.target.value as Audience | "")}
      >
        <option value="">All</option>
        {AUDIENCES.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>

      {slots.isError && <p className="error">{problemText(slots.error)}</p>}

      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th>Starts</th><th>For</th><th>Who</th><th>Band</th>
              <th>Booked</th><th>Prompts</th>
            </tr>
          </thead>
          <tbody>
            {slots.data?.map((slot) => (
              <tr key={slot.xid}>
                <td>{new Date(slot.starts_at).toLocaleString()}</td>
                <td>{ageBandLabel(slot.age_band)}</td>
                <td className="muted">
                  {AUDIENCES.find((a) => a.value === slot.audience)?.label
                    ?? slot.audience}
                </td>
                <td className="muted">{rangeLabel(slot.band_min, slot.band_max)}</td>
                <td className="num">
                  {slot.booked_count ?? 0} / {slot.capacity ?? 0}
                </td>
                <td className="muted">
                  {slot.cue_card_set_version_xid ? "Attached" : "None"}
                </td>
              </tr>
            ))}
            {slots.data?.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  Nothing bookable by this account. That is not the same as nothing
                  scheduled.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
