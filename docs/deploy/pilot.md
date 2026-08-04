# Running a pilot

One prep centre, reading and listening. Everything in
[production.md](production.md) applies — this document is what is *different*,
and the difference is one setting that turns off authentication.

## Scope it out loud

Tell the centre, before they sign anything:

- **Reading and Listening** are scored by the engine.
- **Speaking is not in this pilot**, because there is no TURN deployment in this
  repository. The safety checks themselves are in place: a minor can only book a
  minor-banded slot, and a minor booking a PUBLIC — stranger-matched — slot is
  refused without a `stranger_matching` consent granted by a parent. Both are
  enforced server-side against the account's own date of birth, and both are
  proven by sabotage.
- **Writing is modelled, not scored.**
- **Do not take money through it.** A paid order grants nothing: no code inserts
  `entitlements` rows, so the centre would pay and receive no access. Comp them.

## Sign-in, and what it costs

`app/modules/identity/transport.py` implements Telegram and refuses to pretend
about SMS. A login code for an account with no Telegram link is routed to `sms`,
fails closed, and leaves a `failed` notification naming the missing provider —
rather than a `sent` row that delivered nothing. That is the right design and it
means forty students with no Telegram link cannot get in on day one.

Two ways out. Pick one deliberately.

### Option A — Telegram only

Set `TELEGRAM_BOT_TOKEN`, and have every student start a chat with the bot
before the first session. `users.telegram_user_id` is recorded by the Mini App
sign-in path and *is* the chat id; a bot cannot open a conversation with
somebody who has not started one, so this step cannot be skipped or automated.

Nothing is weakened. It costs one instruction per student and it fails visibly —
a student who has not done it simply cannot sign in.

### Option B — open sign-in

```
PILOT_OPEN_SIGNIN=true
```

`POST /auth/otp/request` returns the login code in its own response.

**This is authentication switched off.** Anyone who knows a phone number can
sign in as that person: a student, a teacher, a centre admin, you. There is no
narrower version of it — narrowing it while calling it open would be the
dangerous kind of half-measure.

It is survivable for exactly one situation: a single centre, a roster you
control, for as long as it takes an SMS contract to sign. It is not survivable
for a second centre, and it is not survivable being forgotten.

Three things make it recoverable rather than merely reckless:

- it is **off by default** and must be set explicitly;
- it **logs a warning at every boot**, so it cannot be discovered only by
  reading a `.env` six months later;
- every issuance writes an **`audit_log` row**, so what it did is a query:

```sql
SELECT count(*), min(at), max(at) FROM audit_log
 WHERE action = 'auth.pilot_code_issued';
```

Turn it off the day SMS lands. If it outlives the pilot, replace it with an
org-scoped lookup a centre admin runs against their own roster — that keeps the
code away from anonymous callers, which is the property this trades away.

Uzbek SMS gateways worth pricing: Eskiz and Play Mobile. Neither needs a code
change beyond an implementation of `Transport._send_sms`, which already exists
as a seam with the failure path written.

## The order to launch in

1. **Deploy** ([production.md](production.md)) and confirm TLS issued.
2. **Do the restore drill.** Dump, destroy, restore, sit a mock on the restored
   box. Not because the backups are suspect — because nobody has done it, and
   the moment you find out is otherwise the moment you needed it. The nightly
   verification proves the dump restores; it does not prove you know the steps.
3. **Create the centre.** `POST /orgs` as a platform admin, then invite its
   admin from the console's Centre screen.
4. **One paper first.** Have the centre author or import a single reading paper
   and publish it. The publish gate will refuse a passage with no copyright
   attestation — that is the point, not a bug. Assume some centres will try to
   upload published Cambridge papers.
5. **Two or three students, not forty.** Assign it to a small cohort and watch
   one attempt from start to marked.
6. **Then the rest of the roster.**

## What to watch in the first week

| | |
|---|---|
| `outbox_lag_seconds` | `/metrics/workers`. Green under 5 s. Over 60 s sustained means scoring has stopped. |
| `[verify] OK:` | in `docker compose logs backup`, nightly. Its **absence** is the alert. |
| `/safety` | The moderation queue orders critical first and can be emptied. One that only grows means nobody is working it. |
| `/flagged-items` | After the first mock. Negative discrimination is almost always a bad key; the fix is on `/regrades`. |
| `df -h` | `pgdata`, `media` and `backups` share one disk. A full disk takes down uploads and exams together. |

## When the pilot ends

```bash
# 1. remove PILOT_OPEN_SIGNIN from .env
docker compose up -d api

# 2. confirm it is gone — this must return nothing
docker compose logs api | grep PILOT_OPEN_SIGNIN

# 3. and that nothing used it after the cutoff
docker compose exec postgres psql -U ielts ielts -c \
  "SELECT max(at) FROM audit_log WHERE action = 'auth.pilot_code_issued'"
```

Then read the audit log for the whole pilot window and decide whether any of it
looks like somebody signing in as somebody else. That is the bill for Option B,
and it is payable at the end whether or not anyone asks for it.
