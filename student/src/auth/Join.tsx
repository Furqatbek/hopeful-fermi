/**
 * Joining a centre from an invitation link — the first screen a new student
 * ever sees, and until now there was no way to reach the product at all.
 *
 * The only code path that created an account was Telegram's, which neither
 * client calls. Signing in by code answers "No account exists for this number";
 * accepting an invitation requires already being signed in. So a centre could be
 * set up, a class filled and a paper published, and not one student could get in.
 *
 * ── what the screen asks for, and why ───────────────────────────────────────
 *
 * The link carries a token. That is the centre's authority — issued by an admin,
 * bound to ONE number, expiring in fourteen days — and it is not enough on its
 * own, because links get forwarded. So the student types their own number and
 * proves it with a code, exactly as signing in does. A forwarded link gets
 * `invite_not_yours`.
 *
 * The number is never shown, only its last four digits. The token names it, and
 * revealing a student's phone number to whoever opens a forwarded link is a leak
 * this flow does not need to take.
 *
 * A date of birth is asked for only when registering. It is not decoration:
 * `users.adult_at` is generated from it, and every minor rule in the product —
 * who may be matched with whom, what needs a parent's consent — reads that
 * column.
 */

import { useEffect, useState } from "react";

import { api, problemText } from "../api/client";
import { storeSession } from "../api/session";

type Invite = {
  org: { name?: string };
  role: string;
  phone_hint: string;
  needs_account: boolean;
};

type Stage =
  | { name: "loading" }
  | { name: "dead"; message: string }
  | { name: "phone" }
  | { name: "code"; challengeXid: string; pilotCode?: string };

const ROLE: Record<string, string> = {
  student: "a student",
  teacher: "a teacher",
  centre_admin: "an administrator",
};

export function Join({ token, onJoined }: { token: string; onJoined: () => void }) {
  const [invite, setInvite] = useState<Invite | null>(null);
  const [stage, setStage] = useState<Stage>({ name: "loading" });
  const [phone, setPhone] = useState("+998");
  const [code, setCode] = useState("");
  const [dob, setDob] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const { data, error: failure } = await api.POST("/auth/invite/preview", {
        body: { token },
      });
      if (cancelled) return;
      if (failure || !data) {
        // A dead invite is a dead end, not a form to retry. Say which kind of
        // dead it is and who can fix it.
        setStage({ name: "dead", message: problemText(failure) });
        return;
      }
      setInvite(data);
      setStage({ name: "phone" });
    })();
    return () => { cancelled = true; };
  }, [token]);

  async function requestCode(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const { data, error: failure } = await api.POST("/auth/otp/request", {
      body: { phone, purpose: "login", channel: "sms" },
    });
    setBusy(false);
    if (failure || !data) { setError(problemText(failure)); return; }
    setStage({
      name: "code",
      challengeXid: data.challenge_xid,
      ...(data.pilot_code ? { pilotCode: data.pilot_code } : {}),
    });
  }

  async function redeem(event: React.FormEvent) {
    event.preventDefault();
    if (stage.name !== "code") return;
    setBusy(true);
    setError(null);
    const { data, error: failure } = await api.POST("/auth/invite/redeem", {
      body: {
        token,
        challenge_xid: stage.challengeXid,
        code,
        ...(invite?.needs_account
          ? { date_of_birth: dob, given_name: name || null }
          : {}),
      },
    });
    setBusy(false);
    if (failure || !data) { setError(problemText(failure)); return; }
    storeSession(data.access_token);
    // Take the token out of the address bar. It is a credential, it is spent,
    // and leaving it there puts it in browser history and in the referrer of
    // every link the student clicks next.
    window.history.replaceState({}, "", window.location.pathname);
    onJoined();
  }

  if (stage.name === "loading") {
    return <main className="signin"><p className="muted">Checking your invitation…</p></main>;
  }

  if (stage.name === "dead") {
    return (
      <main className="signin">
        <h1>This invitation cannot be used</h1>
        <p className="error">{stage.message}</p>
        <p className="muted">
          Ask your centre to send a new one — invitations expire after two weeks
          and can only be used once.
        </p>
      </main>
    );
  }

  return (
    <main className="signin">
      <h1>Join {invite?.org.name}</h1>
      <p className="muted">
        You have been invited as {ROLE[invite?.role ?? ""] ?? invite?.role}.
      </p>

      {stage.name === "phone" ? (
        <form onSubmit={(event) => void requestCode(event)}>
          <label htmlFor="phone">Your phone number</label>
          <p className="muted">
            The invitation was sent to the number ending {invite?.phone_hint}.
            Type it in full to prove it is yours.
          </p>
          <input
            id="phone"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="+998901234567"
            pattern="^\+998[0-9]{9}$"
            required
            autoComplete="tel"
          />
          <button disabled={busy}>{busy ? "Sending…" : "Send code"}</button>
        </form>
      ) : (
        <form onSubmit={(event) => void redeem(event)}>
          {stage.pilotCode && (
            <p className="pilot">
              <strong>Pilot mode.</strong> There is no SMS provider yet, so the
              code is shown here instead of being sent:{" "}
              <span className="pilot__code">{stage.pilotCode}</span>
            </p>
          )}

          <label htmlFor="code">Six-digit code</label>
          <input
            id="code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            inputMode="numeric"
            pattern="^[0-9]{6}$"
            maxLength={6}
            required
            autoFocus
            autoComplete="one-time-code"
          />

          {invite?.needs_account && (
            <>
              <label htmlFor="name">Your name</label>
              <input
                id="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoComplete="given-name"
              />

              <label htmlFor="dob">Date of birth</label>
              {/* Asked once, never shown again, and never returned by any
                  endpoint. It decides which speaking pools are open to you and
                  what needs a parent's consent. */}
              <p className="muted">
                We ask because some parts of the app work differently for under-18s.
              </p>
              <input
                id="dob"
                type="date"
                value={dob}
                onChange={(e) => setDob(e.target.value)}
                required
                max={new Date().toISOString().slice(0, 10)}
                autoComplete="bday"
              />
            </>
          )}

          <button disabled={busy}>{busy ? "Joining…" : "Join"}</button>
          <button
            type="button"
            className="link"
            onClick={() => { setStage({ name: "phone" }); setCode(""); setError(null); }}
          >
            Use a different number
          </button>
        </form>
      )}

      {error && <p className="error">{error}</p>}
    </main>
  );
}
