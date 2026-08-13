/**
 * Sign in by phone number and a one-time code.
 *
 * The same two-step exchange the console uses, with student-facing copy and one
 * addition the console does not need: the **pilot code**.
 *
 * ── why the code is on screen ────────────────────────────────────────────────
 *
 * There is no SMS contract yet, so `PILOT_OPEN_SIGNIN` makes
 * `POST /auth/otp/request` return the code in its own response body. That is
 * account takeover by design — anyone who can guess a phone number can sign in
 * as that person — and the server logs a warning about it at every boot.
 *
 * So this screen shows it, loudly and labelled as temporary, rather than
 * pretending a code is in flight that will never arrive. The alternative is a
 * student staring at an empty inbox, which is a support call the owner cannot
 * afford to take. The banner is deliberately ugly: it should be embarrassing to
 * still be there on the day an SMS contract is signed.
 *
 * `pilot_code` is absent from the response the moment the flag is off, so this
 * degrades to an ordinary "we sent you a code" screen with no code change.
 *
 * ── why the code usually arrives on Telegram ────────────────────────────────
 *
 * `notify._channel` picks Telegram whenever the account has a linked Telegram
 * id and only falls back to SMS when it does not, because Telegram is free and
 * SMS is the one line on the infra bill that grows with the user count. Hence
 * "check Telegram first" rather than "we sent an SMS", which would be wrong for
 * most students and would have them waiting on the wrong medium.
 */

import { useState } from "react";

import { api, problemText } from "../api/client";
import { storeSession } from "../api/session";

type Stage =
  | { name: "phone" }
  | { name: "code"; challengeXid: string; pilotCode?: string };

export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [stage, setStage] = useState<Stage>({ name: "phone" });
  const [phone, setPhone] = useState("+998");
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function requestCode(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const { data, error: failure } = await api.POST("/auth/otp/request", {
      // `channel` is a REQUEST, not a decision: the server overrides it with
      // Telegram whenever the account is linked, precisely so a client cannot
      // spend the SMS budget.
      body: { phone, purpose: "login", channel: "sms" },
    });
    setBusy(false);
    if (failure || !data) {
      setError(problemText(failure));
      return;
    }
    setStage({
      name: "code",
      challengeXid: data.challenge_xid,
      ...(data.pilot_code ? { pilotCode: data.pilot_code } : {}),
    });
  }

  async function submitCode(event: React.FormEvent) {
    event.preventDefault();
    if (stage.name !== "code") return;
    setBusy(true);
    setError(null);
    const { data, error: failure } = await api.POST("/auth/otp/verify", {
      body: { challenge_xid: stage.challengeXid, code },
    });
    setBusy(false);
    if (failure || !data) {
      setError(problemText(failure));
      return;
    }
    // Only the access token is in the body. The refresh token arrived as a
    // `Set-Cookie` the browser has already stored and this code cannot read.
    storeSession(data.access_token);
    onSignedIn();
  }

  return (
    <main className="signin">
      <h1>IELTS Hub</h1>
      <p className="muted">Practice and mock exams</p>

      {stage.name === "phone" ? (
        <form onSubmit={(event) => void requestCode(event)}>
          <label htmlFor="phone">Phone number</label>
          <input
            id="phone"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="+998901234567"
            // The same pattern the server pins (`PHONE_PATTERN`). Here so the
            // form refuses before spending a request — a convenience, never
            // INSTEAD of the server check.
            pattern="^\+998[0-9]{9}$"
            required
            autoComplete="tel"
          />
          <button disabled={busy}>{busy ? "Sending…" : "Send code"}</button>
        </form>
      ) : (
        <form onSubmit={(event) => void submitCode(event)}>
          {stage.pilotCode && (
            <p className="pilot">
              <strong>Pilot mode.</strong> There is no SMS provider yet, so the
              code is shown here instead of being sent:{" "}
              <span className="pilot__code">{stage.pilotCode}</span>
              <br />
              Anyone who knows your number could sign in as you until this is
              switched off.
            </p>
          )}

          <label htmlFor="code">Six-digit code</label>
          <p className="muted">
            Sent to {phone} — check Telegram first; it goes there when your
            account is linked.
          </p>
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
          <button disabled={busy}>{busy ? "Checking…" : "Sign in"}</button>
          <button
            type="button"
            className="link"
            onClick={() => {
              setStage({ name: "phone" });
              setCode("");
              setError(null);
            }}
          >
            Use a different number
          </button>
        </form>
      )}

      {error && <p className="error">{error}</p>}
    </main>
  );
}
