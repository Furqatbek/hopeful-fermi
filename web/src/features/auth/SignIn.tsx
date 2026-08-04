/**
 * Sign in by phone number and a one-time code.
 *
 * Worth stating because it is not obvious from the endpoint names: **the code
 * usually arrives on Telegram, not by SMS.** `notify._channel` picks Telegram
 * whenever the account has a linked Telegram id, and only falls back to SMS when
 * it does not — because Telegram is free and SMS is the one line on the infra
 * bill that grows with the user count.
 *
 * That matters here for two reasons. It is what makes a DESKTOP admin sign-in
 * work at all: the Mini App is a mobile surface, but an OTP delivered over
 * Telegram can be typed into a laptop. And it is why this screen says "we sent a
 * code" rather than "we sent an SMS" — the second sentence would be wrong for
 * most staff, and a person waiting on the wrong medium is a support call.
 *
 * No SMS provider is contracted yet, so an account with no Telegram link cannot
 * currently receive a code at all. The server does not pretend otherwise — the
 * notification ends `failed` with a reason naming the missing provider — but the
 * REQUEST still answers 202, deliberately, because answering anything else would
 * turn this form into a phone-number oracle. So a code that never arrives is a
 * real outcome, and the copy below has to prepare somebody for it.
 */

import { useState } from "react";

import { api, problemText } from "../../api/client";
import { storeSession } from "../../api/session";

type Stage = { name: "phone" } | { name: "code"; challengeXid: string };

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
      // `channel` is required by the contract, and is a REQUEST not a
      // decision: `notify._channel` overrides it with Telegram whenever the
      // account is linked, precisely so a client cannot spend the SMS budget.
      body: { phone, purpose: "login", channel: "sms" },
    });
    setBusy(false);
    if (failure || !data) {
      setError(problemText(failure));
      return;
    }
    setStage({ name: "code", challengeXid: data.challenge_xid });
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
    storeSession(data.access_token, data.refresh_token);
    onSignedIn();
  }

  return (
    <main className="signin">
      <h1>IELTS Hub</h1>
      <p className="muted">Centre administration</p>

      {stage.name === "phone" ? (
        <form onSubmit={(event) => void requestCode(event)}>
          <label htmlFor="phone">Phone number</label>
          <input
            id="phone"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="+998901234567"
            // Same pattern the server pins (`PHONE_PATTERN`). Stated here so the
            // form refuses before spending a request, never INSTEAD of the
            // server check — this is a convenience, not a control.
            pattern="^\+998[0-9]{9}$"
            required
            autoComplete="tel"
          />
          <button disabled={busy}>{busy ? "Sending…" : "Send code"}</button>
        </form>
      ) : (
        <form onSubmit={(event) => void submitCode(event)}>
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
