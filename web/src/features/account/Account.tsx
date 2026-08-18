/**
 * The signed-in member of staff: their profile, their consents, their devices.
 *
 * Six endpoints the console had never called, and the reason to put them on one
 * screen is that they are one question — "what does this system hold about me,
 * and where am I signed in" — asked by the person it holds it about.
 *
 * **`date_of_birth` is collected and never shown, and cannot be changed here.**
 * It is read for one thing, the 18 boundary, which decides which speaking pools
 * an account may enter; `user_dto` leaves it out of every response and
 * `UserUpdate` has no field for it. So this screen surfaces the DERIVED fact
 * (`is_minor`) and says where a correction has to go instead of offering an
 * input that would silently do nothing.
 *
 * **The phone number is not editable either**, and for a sharper reason: it is
 * what an organization invite is bound to and what an SMS code proves. No
 * endpoint changes it, and one that did would need the same proof again.
 *
 * **Consent is a ledger.** Each grant appends a row with the document version
 * and a hash of it; nothing here overwrites a previous answer. `currentConsents`
 * is what turns that log into "held / not held", because a privacy notice
 * accepted twice is two rows and only the newer one is the answer.
 *
 * **Devices are sessions.** Forgetting one revokes its refresh token
 * immediately. The server cannot tell this console which row is the session it
 * is talking to — the access token carries only `sub` — so this screen does not
 * pretend to know, and warns instead.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import {
  CONSENT_KINDS,
  type ConsentKind,
  consentProblem,
  currentConsents,
} from "./consents";

const LOCALES = ["uz-Latn", "uz-Cyrl", "ru", "en"] as const;
const GRANTED_BY = ["self", "parent", "centre_admin"] as const;
const CHANNELS = ["web", "telegram", "sms", "paper"] as const;

/** Plain words for the five consent kinds. The enum values are stored and shown
 *  as-is elsewhere; these are for staff reading a list in a second language. */
const KIND_LABEL: Record<ConsentKind, string> = {
  terms: "Terms of use",
  privacy: "Privacy notice",
  parental: "Parental permission",
  stranger_matching: "Speaking practice with strangers",
  marketing: "Marketing messages",
};

export function Account() {
  const queries = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const me = useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/me");
      if (failure) throw failure;
      return data;
    },
  });

  const consents = useQuery({
    queryKey: ["consents"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/me/consents");
      if (failure) throw failure;
      return data;
    },
  });

  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/me/devices");
      if (failure) throw failure;
      return data;
    },
  });

  const forget = useMutation({
    mutationFn: async (xid: string) => {
      const { error: failure } = await api.DELETE("/me/devices/{xid}", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      void queries.invalidateQueries({ queryKey: ["devices"] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  if (me.isPending) return <div className="page muted">Loading…</div>;
  if (me.isError) {
    return (
      <div className="page">
        <h1>Your account</h1>
        <p className="error">{problemText(me.error)}</p>
      </div>
    );
  }

  const user = me.data;
  const states = currentConsents(consents.data ?? []);

  return (
    <div className="page">
      <h1>Your account</h1>
      <p className="muted">
        {user?.phone} · joined{" "}
        {user?.created_at ? new Date(user.created_at).toLocaleDateString() : "—"}
      </p>
      {error && <p className="error">{error}</p>}

      {user && <Profile user={user} onFailed={setError} />}

      <h2>Consents</h2>
      {consents.isError && <p className="error">{problemText(consents.error)}</p>}
      <div className="scroll">
        <table>
          <thead>
            <tr><th>What</th><th>Held</th><th>Document</th><th>Given by</th><th>When</th></tr>
          </thead>
          <tbody>
            {states.map((state) => (
              <tr key={state.kind}>
                <td>{KIND_LABEL[state.kind]}</td>
                <td>
                  {state.held
                    ? <strong>yes</strong>
                    : <span className="muted">{state.latest ? "withdrawn" : "not on file"}</span>}
                </td>
                <td className="muted">
                  {state.latest?.doc_version ?? "—"}
                  {state.superseded > 0 && (
                    <span> · {state.superseded} earlier</span>
                  )}
                </td>
                <td className="muted">
                  {state.latest?.granted_by_kind.replaceAll("_", " ") ?? "—"}
                  {state.latest?.channel ? ` · ${state.latest.channel}` : ""}
                </td>
                <td className="muted">
                  {state.latest
                    ? new Date(state.latest.granted_at).toLocaleDateString()
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted">
        Nothing here can be withdrawn from this console. The record has a place
        for a withdrawal date and no endpoint writes it, so withdrawing a consent
        is a support action for now. Recording a new version of a document below
        is the supported way to bring a consent up to date: the old row stays,
        which is the point of keeping consent as evidence rather than as a
        setting.
      </p>

      {user && (
        <RecordConsent
          isMinor={user.is_minor === true}
          onFailed={setError}
          onRecorded={() => {
            setError(null);
            void queries.invalidateQueries({ queryKey: ["consents"] });
          }}
        />
      )}

      <h2>Where you are signed in</h2>
      {devices.isError && <p className="error">{problemText(devices.error)}</p>}
      <div className="scroll">
        <table>
          <thead>
            <tr><th>Device</th><th>Last used</th><th /></tr>
          </thead>
          <tbody>
            {devices.data?.map((device) => (
              <tr key={device.xid}>
                <td>
                  {/* `auth_sessions.device_label` is returned by this endpoint and
                      written by no code path, so it is null for every session that
                      has ever been opened. Rendered as an honest placeholder
                      rather than a blank cell, which would read as a rendering
                      fault rather than as a fact about the data. */}
                  {device.label ?? <span className="muted">Unnamed session</span>}
                </td>
                <td className="muted">
                  {device.last_seen_at
                    ? new Date(device.last_seen_at).toLocaleString()
                    : "—"}
                </td>
                <td>
                  {device.xid && (
                    <button
                      className="link"
                      onClick={() => forget.mutate(device.xid!)}
                      disabled={forget.isPending}
                    >
                      Forget
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {devices.data?.length === 0 && (
              <tr><td colSpan={3} className="muted">No open sessions.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="muted">
        {/* `current` comes back false on every row: the access token carries only
            the user, never the session it was minted from, so the server has
            nothing to match this request against. Saying so is the only safe
            option — a guess would be a button labelled "not this one" that signs
            the teacher out mid-upload. */}
        This list cannot show which of these is the computer you are using now.
        Forgetting a session ends it at once, and if it is this one you will be
        asked to sign in again. Signing out closes every session on this list, on
        every device.
      </p>
    </div>
  );
}


/**
 * The four fields `UserUpdate` accepts, and an explanation for the ones it does
 * not.
 *
 * Seeded from the server's copy rather than kept in sync with it: this form is
 * opened rarely and edited once, so the simplest correct thing is to hold a
 * draft from the moment the screen loads and send the whole of it.
 */
function Profile({ user, onFailed }: {
  user: {
    given_name: string;
    family_name?: string | null;
    locale: "uz-Latn" | "uz-Cyrl" | "ru" | "en";
    timezone: string;
    target_band?: number | null;
    is_minor?: boolean;
    telegram_username?: string | null;
  };
  onFailed: (message: string | null) => void;
}) {
  const queries = useQueryClient();
  const [givenName, setGivenName] = useState(user.given_name);
  const [familyName, setFamilyName] = useState(user.family_name ?? "");
  const [locale, setLocale] = useState<(typeof LOCALES)[number]>(user.locale);
  const [targetBand, setTargetBand] = useState(
    user.target_band === null || user.target_band === undefined
      ? ""
      : String(user.target_band),
  );
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: async () => {
      const band = targetBand.trim() === "" ? null : Number(targetBand);
      if (band !== null && (Number.isNaN(band) || band < 1 || band > 9)) {
        // The same bounds the server pins (`Field(ge=1, le=9)`). Checked here so
        // the form refuses before spending a request, never instead of the
        // server check.
        throw new Error("A target band is between 1 and 9.");
      }
      const { error: failure } = await api.PATCH("/me", {
        body: {
          given_name: givenName.trim(),
          family_name: familyName.trim(),
          locale,
          // Omitted rather than sent as null when blank. The handler applies
          // `exclude_none`, so a null would be dropped anyway — but the contract
          // types the field as a number, and sending null to satisfy "clear it"
          // would be writing against an implementation detail. Clearing a target
          // band is not something this endpoint offers.
          ...(band === null ? {} : { target_band: band }),
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      onFailed(null);
      setSaved(true);
      void queries.invalidateQueries({ queryKey: ["me"] });
    },
    onError: (failure) => {
      setSaved(false);
      onFailed(problemText(failure) || String(failure));
    },
  });

  return (
    <>
      <h2>Profile</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          onFailed(null);
          setSaved(false);
          save.mutate();
        }}
      >
        <label htmlFor="a-given">First name</label>
        <input
          id="a-given"
          value={givenName}
          onChange={(event) => setGivenName(event.target.value)}
          required
        />

        <label htmlFor="a-family">Family name</label>
        <input
          id="a-family"
          value={familyName}
          onChange={(event) => setFamilyName(event.target.value)}
        />

        <label htmlFor="a-locale">Language</label>
        <select
          id="a-locale"
          value={locale}
          onChange={(event) =>
            setLocale(event.target.value as (typeof LOCALES)[number])
          }
        >
          {LOCALES.map((code) => (
            <option key={code} value={code}>{code}</option>
          ))}
        </select>

        <label htmlFor="a-band">Target band</label>
        <input
          id="a-band"
          type="number"
          min={1}
          max={9}
          step={0.5}
          value={targetBand}
          onChange={(event) => setTargetBand(event.target.value)}
        />

        <button disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save profile"}
        </button>
        {saved && <p className="muted">Saved.</p>}
      </form>

      <p className="muted">
        Time zone is <strong>{user.timezone}</strong> and your Telegram account
        is{" "}
        {user.telegram_username
          ? <>linked as <strong>@{user.telegram_username}</strong></>
          : "not linked"}
        . Neither is editable here.
      </p>
      <p className="muted">
        {/* Named explicitly rather than left as an absent field. A date of birth
            that is wrong by a year moves an account across the 18 boundary, and
            the boundary decides who may be matched for speaking practice with
            strangers — so the change needs a support action with an audit record,
            not a self-service form. */}
        Your date of birth is not shown and cannot be changed here. It is held for
        one purpose — whether this account is under 18, which decides who can be
        matched for speaking practice — and this account is recorded as{" "}
        <strong>{user.is_minor ? "under 18" : "18 or over"}</strong>. If that is
        wrong, ask support to correct it; changing it moves the account across
        that boundary and is recorded.
      </p>
    </>
  );
}


/**
 * Recording a consent.
 *
 * Append-only, and shaped so it cannot send a request the server will refuse.
 * The one refusal that is not obvious from the form is parental: `POST
 * /me/consents` answers 403 `parental_consent_required` for `stranger_matching`
 * on an account under 18 unless the grant names a parent and carries a phone
 * number. `consentProblem` holds that rule and this component renders it as a
 * sentence before the button rather than as a refusal after it.
 */
function RecordConsent({ isMinor, onRecorded, onFailed }: {
  isMinor: boolean;
  onRecorded: () => void;
  onFailed: (message: string | null) => void;
}) {
  const [kind, setKind] = useState<ConsentKind>("terms");
  const [docVersion, setDocVersion] = useState("");
  const [grantedByKind, setGrantedByKind] =
    useState<(typeof GRANTED_BY)[number]>("self");
  const [parentName, setParentName] = useState("");
  const [parentPhone, setParentPhone] = useState("+998");
  const [channel, setChannel] = useState<(typeof CHANNELS)[number]>("web");

  const draft = {
    kind, docVersion, grantedByKind, parentName, parentPhone, isMinor,
  };
  const blocked = consentProblem(draft);
  const wantsParent = grantedByKind === "parent";

  const record = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/me/consents", {
        body: {
          kind,
          doc_version: docVersion.trim(),
          granted_by_kind: grantedByKind,
          channel,
          ...(wantsParent
            ? {
                parent_name: parentName.trim(),
                parent_phone: parentPhone.trim(),
              }
            : {}),
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setDocVersion("");
      onRecorded();
    },
    onError: (failure) => onFailed(problemText(failure)),
  });

  return (
    <>
      <h2>Record a consent</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          onFailed(null);
          if (!blocked) record.mutate();
        }}
      >
        <label htmlFor="c-kind">What is being consented to</label>
        <select
          id="c-kind"
          value={kind}
          onChange={(event) => setKind(event.target.value as ConsentKind)}
        >
          {CONSENT_KINDS.map((value) => (
            <option key={value} value={value}>{KIND_LABEL[value]}</option>
          ))}
        </select>

        <label htmlFor="c-doc">Document version</label>
        <input
          id="c-doc"
          value={docVersion}
          onChange={(event) => setDocVersion(event.target.value)}
          placeholder="privacy-2026-03"
          required
        />
        <p className="muted">
          Stored with a hash of this exact string, which is what makes the record
          evidence rather than a checkbox. Use the version printed on the document
          the person actually read.
        </p>

        <label htmlFor="c-by">Given by</label>
        <select
          id="c-by"
          value={grantedByKind}
          onChange={(event) =>
            setGrantedByKind(event.target.value as (typeof GRANTED_BY)[number])
          }
        >
          {GRANTED_BY.map((value) => (
            <option key={value} value={value}>{value.replaceAll("_", " ")}</option>
          ))}
        </select>

        {wantsParent && (
          <>
            <label htmlFor="c-pname">Parent's name</label>
            <input
              id="c-pname"
              value={parentName}
              onChange={(event) => setParentName(event.target.value)}
            />
            <label htmlFor="c-pphone">Parent's phone number</label>
            <input
              id="c-pphone"
              value={parentPhone}
              onChange={(event) => setParentPhone(event.target.value)}
              pattern="^\+998[0-9]{9}$"
            />
          </>
        )}

        <label htmlFor="c-channel">How it was given</label>
        <select
          id="c-channel"
          value={channel}
          onChange={(event) =>
            setChannel(event.target.value as (typeof CHANNELS)[number])
          }
        >
          {CHANNELS.map((value) => (
            <option key={value} value={value}>{value}</option>
          ))}
        </select>

        {blocked && <p className="error">{blocked}</p>}
        <button disabled={record.isPending || blocked !== null}>
          {record.isPending ? "Recording…" : "Record this consent"}
        </button>
      </form>
    </>
  );
}
