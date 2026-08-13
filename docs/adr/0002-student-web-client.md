# ADR-0002: The student client is a website, not a native app

- **Status:** Accepted — ratified by the owner 2026-08-13 (all four §10 decisions answered)
- **Date:** 2026-08-13
- **Author:** Backend/architecture
- **Refines:** ADR-0001 §7 ("Native mobile apps") and Assumption 1 ("Frontend is a web PWA … no native app at MVP")
- **Supersedes:** —

---

## 0. TL;DR

| Decision | Choice | One-line reason |
|---|---|---|
| Student client | **A website, opened in a browser.** Not a native app, not a packaged desktop app. | ADR-0001 already assumed this; the "mobile app" framing was drift, and native was never buying what it was sold as. |
| Design target | **Large-screen-first for the exam simulator; responsive down to a phone for everything else.** | The real IELTS computer-delivered test is a side-by-side desktop layout. A phone turns that into scrolling, which trains a skill the exam does not test. |
| Delivery | **A second SPA behind the same Caddy**, on its own origin, sharing only the API. | The admin console already runs exactly this way. Same-origin ⇒ no CORS. A separate origin keeps the owner's "student UI separated from admin" constraint. |
| Native apps | **Not now.** Reassessed, not merely deferred. | A browser is exactly as untrusted as a phone, and the server is already the sole trust boundary. Native adds cost and stores, buys no security. |
| The one real security change | **Refresh token moves to an httpOnly, Secure, SameSite cookie** (ratified). | It is returned in and read from the JSON body today — a Keychain-shaped design. In a tab that is an XSS-readable long-lived credential; a cookie the JS cannot read is not. |
| Installability (PWA) | **Optional enhancement, later.** Still a website; not an "app." | ADR-0001 §7 already drew the offline line: build the answer outbox, do not cache whole tests offline. |
| The endpoints | **Transfer as-is.** The load-bearing one was already written for a browser. | `POST /answers` specifies an IndexedDB outbox; speaking is WebRTC; both are browser-native. |

**Headline:** the owner's instinct — "a phone doesn't feel like an exam" — is correct, and the fix is *not* "switch to a mobile app." It is to stop treating the small screen as the primary target for the one surface that needs a big one. This ADR does two things: it ratifies ADR-0001's web decision against the "mobile app" drift, and it corrects my own earlier word "desktop-first," which the owner rightly flagged — the target is a **website designed large-screen-first**, not a desktop application.

---

## 1. What actually changed

Nothing in the backend. This is a client-delivery decision that had quietly diverged from the ADR and is being pulled back.

ADR-0001 Assumption 1 reads, verbatim: *"Frontend is a web PWA (React/Next or similar) that you own; no native app at MVP."* §7 lists "Native mobile apps" under **What I am deliberately NOT building**, with the seam *"API is mobile-first: idempotent writes, resumable everything, small payloads."*

Somewhere after that, the student-facing endpoints came to be called "the mobile app endpoints," and the owner evaluated the *product* through that frame — installed it, and found it "doesn't give exam vibe at all." That is a true observation about a phone screen, routed to the wrong conclusion. The endpoints are not mobile-app endpoints; they are **client-agnostic HTTP**, and the ADR always intended a browser to call them.

So this is not a reversal. It is a correction of vocabulary that had started to drive design.

---

## 2. The decision, stated so it cannot be misread

The student client is **a website**. A person opens a URL in Chrome, Safari, or the in-app browser of Telegram, and uses it. There is:

- **no native app** (no Swift, no Kotlin, no React Native), and
- **no desktop application** (no Electron, no packaged binary, no installer).

"Large-screen-first" describes the *viewport we design against first*, not the delivery mechanism. The exam simulator is laid out for a laptop-sized window because that is what it is imitating, and it degrades responsively; the rest of the product is laid out for a phone and scales up. Both are the same website, one React build, served in a browser.

This is the same technology, tooling, and CI as the admin console already in `web/`: React 19 + Vite + TypeScript, the `openapi-fetch` client generated from `openapi/openapi.yaml`, and the `web-codegen-check` gate that fails the build when the client drifts from the contract.

---

## 3. Where "make it a mobile app" was right, and where it was wrong

The owner is right about something real, and it is worth stating precisely so we build for it rather than around it.

**Right — exam fidelity is a feature, not a garnish.** The reason a person pays for a mock is that it feels like the day. Three specifics carry that feeling, and all three want a large screen:

- **Reading** is three passages, forty questions, sixty minutes. The real computer-delivered test puts passage and questions **side by side**. On a phone that collapses into scrolling back and forth — a *different cognitive task* from the one being examined. You would be training a skill the test does not measure.
- **Writing** is 150 + 250 words against the clock, on a **physical keyboard**. Thumb-typing 250 words rehearses nothing about the exam.
- **The room.** A full-window, chrome-free, countdown-in-the-corner surface is what produces the pressure that makes a mock worth sitting. A phone notification shade and a browser address bar are the opposite of that.

**Wrong — "nobody prepares on a phone" is too strong, and following it deletes real value.** People absolutely prepare on phones: vocabulary drills on the bus, a listening section with headphones, checking a band score, booking a speaking slot, a five-minute grammar set. The true split is not *mobile vs. web* — it is **rehearsal vs. drill**:

| Surface | Wants | Client |
|---|---|---|
| Full mock: Reading, Listening, Writing | A big screen, a keyboard, a chrome-free room | Large-screen web — the "exam simulator" |
| Drill: vocab, single sections, review of a past attempt | A spare five minutes, one hand | The **same** website, responsive, on a phone |
| Booking, results, band history, speaking queue | Anywhere, any size | The same website, responsive |
| Speaking session itself | A stable connection for ~14 min | Deferred for the pilot — see §8 |

Deleting the phone surface to chase exam-vibe would keep the thing people buy and throw away the thing that makes them open the product on a Tuesday. **One responsive website serves both halves**, which is why "website, large-screen-first" beats both "mobile app" and "desktop-only."

---

## 4. Why the security argument for native dissolves

The original reason the student client was framed as separate-and-native was security — the owner's words, *"student facing UI is mobile app or separated from backend for security concerns."* That reasoning does not survive contact with ADR-0001's own design, and it should be retired explicitly rather than left to quietly justify a native build.

ADR-0001 §5.3 and the constraint list are unambiguous: **the server is the sole authority on time remaining and on scoring; the client is never trusted with either.** The codebase enforces it — `GET /attempts/{xid}` returns `server_now` beside `expires_at` so the countdown is a server delta and never the device clock; section clocks begin server-side at `POST …/sections/{position}/enter`; play-once is enforced there; media is short-TTL, per-user tokenized (§5.4, §9.4); IDs are opaque `xid` so the content library cannot be walked (§5.1).

Given all of that, **a native app is not a security boundary.** An APK or IPA unpacks in a minute; a proxy (mitmproxy, Burp) sits transparently in front of native and browser alike; certificate pinning delays a motivated attacker by an afternoon. Native buys the *feeling* of a sealed client and none of the substance. The substance is server-side, and it is already built.

The correct reading of the owner's constraint is the half that is real: **separation.** The student UI should not share an origin, a deployment, or a session surface with the admin console. That is satisfied by shipping the student site as a **separate SPA on its own subdomain**, sharing only the versioned API — which Caddy already supports (§5) and which is strictly less machinery than a native app. So the constraint is honored; only its delivery changes from "native" to "second website."

**A browser is exactly as untrusted as a phone, and the architecture already assumes an untrusted client.** That is the whole argument.

---

## 5. The endpoints transfer — evidence, not assertion

This is not "they probably work in a browser." Two of the load-bearing pieces are already *specified* for one.

**The most important endpoint in the product was written against a browser API.** `POST /attempts/{xid}/answers` — its own contract description opens *"The load-bearing endpoint of the whole product"* — specifies the client flushing an **IndexedDB** outbox every 5–10 s or on blur, with a required `Idempotency-Key`, per-slot `client_seq` ordering, partial-batch acceptance, and *"the response is the clock sync."* IndexedDB is a browser store. The resilience design that makes a 60-minute exam survivable on bad Tashkent wifi — incremental save decoupled from submit, idempotent replay after a drop — is a browser design already, and ADR-0001 §8.2 and §10 argue for it as such.

**Speaking is WebRTC, which browsers do natively.** `GET /speaking/ice-servers`, `POST /speaking/queue`, `/speaking/pairs/{xid}/…` describe standard WebRTC signaling. A browser needs no plugin for `getUserMedia` and `RTCPeerConnection`; this is *less* work in a browser than in native, not more.

**The hosting pattern is already in production.** The Caddyfile serves the admin SPA from a static root (`root * /srv/web`, `try_files {path} /index.html`, hashed assets immutable, shell `no-store`) and reverse-proxies `@backend path /api/* /realtime /realtime/* /internal/* /healthz /metrics/*` to `api:8000`. The student site is a **second static root behind the same Caddy**. Same-origin with its API means the absence of `CORSMiddleware` (confirmed — there is none) is correct, not a gap.

The endpoints, in short, do not need porting. They need a browser to call them, which is what they were for.

---

## 6. What genuinely needs building or deciding

Small, and honest. None of it is a rewrite; three are decisions and one is the actual product work.

| Item | What | Cost | Note |
|---|---|---|---|
| **The exam simulator layouts** | The real work: a large-screen Reading (side-by-side passage/questions), Listening (single-play audio, server-timed), Writing (word-count, keyboard) surface, chrome-free. | The bulk of the effort — it is the product. | This is what "exam vibe" actually is. Everything else here is plumbing. |
| **Origin separation** | Student site as its own subdomain / static root behind Caddy, separate from the admin console. | Caddy config + a second Vite build. Hours, not architecture. | Satisfies the owner's separation constraint (§4) and keeps admin and student session surfaces disjoint. |
| **Refresh-token storage** *(ratified: cookie)* | Today the refresh token is returned in and read from the JSON body. In a browser that means JS-readable storage (`localStorage`), which any XSS drains. | A contained auth change: `/auth/otp/verify`, `/auth/telegram/verify` and `/auth/refresh` set the token via `Set-Cookie` instead of the body, and `/auth/refresh` reads it from the cookie instead of `RefreshRequest`. | **Decided: httpOnly, Secure, SameSite cookie.** Same-origin behind Caddy (decision 3) makes `SameSite=Strict` viable, which closes the CSRF surface the cookie would otherwise open — logout/refresh are the only state-changing GET-adjacent calls and both are POST. The access token stays a short-lived in-memory Bearer (`app/api/limits.py` already reads `Authorization`). The existing rotation-with-reuse-detection in `/auth/refresh` is unchanged. **This applies to the admin console too** (§9) — it is a browser client with the identical exposure, so both adopt the cookie path rather than splitting the auth design. |
| **CORS** | Only if the student origin ever calls the API cross-origin (i.e. not proxied same-origin through Caddy). | A middleware + allow-list, if needed at all. | Prefer same-origin-behind-Caddy and this never appears. If a separate API host is chosen later, add a tight allow-list — never `*` with credentials. |

Everything else the student client needs — attempts, answers, payload, result, review, section enter, speaking, `/me/devices` — exists and is contract-tested.

---

## 7. What we lose by not going native, and why it is affordable

**Push notifications on iPhone.** Web push on iOS requires the user to add the site to the Home Screen first; there is no background push to a plain Safari tab. This is the one genuine capability native would add. Three things make it affordable:

1. **There is no push infrastructure today.** No device-token registration endpoints exist (`/me/devices` is session/device *listing*, not push tokens). We would be building push from zero either way — this is not a capability being given up, it is one not yet built.
2. **The market is Android-dominant** (ADR-0001 Assumption 2), where web push works in a normal browser.
3. **The notification channel is Telegram** (ADR-0001 §8.6): regrade notices, competition reminders, and deadlines are meant to go through the bot, which reaches iOS and Android identically and for free. That is a *better* channel than native push for this audience, not a fallback.

**App-store presence.** Real for consumer trust in some markets; near-irrelevant here, where a prep centre onboards students by pasting a link into a Telegram group. Against it: Apple's $99/year, review latency on every release, and the "install our app" friction that a link does not have. For a solo engineer on near-zero budget, an App Store account to serve a big-screen use case is the expensive path to a worse product.

---

## 8. Speaking — deferred for the pilot

**Ratified 2026-08-13: the speaking *session* is out of scope for the student web pilot.** The owner chose to skip it rather than decide its transport now, which is the right call — it removes the one genuinely hard client question from the pilot's critical path without touching anything else. The booking and queue surfaces (`/speaking/slots`, `/speaking/queue`) are ordinary browser screens and can ship whenever they are wanted; only the live audio call is deferred. Nothing below is decided — it is kept as the analysis for when speaking returns.

**The reason it was worth deferring — speaking, on a browser tab.** A backgrounded or locked-screen browser tab can be suspended by the OS, and a 14-minute speaking call is exactly where that hurts — mid-conversation, on a phone, is the worst moment to lose the media session. This is the one place native genuinely is more reliable than a browser.

But native-for-the-whole-app is the wrong response to a problem isolated to one 14-minute surface. The candidates, to be decided separately:

- **A Telegram bot or Telegram voice** for the speaking session specifically. ADR-0001 §8.6 already puts Telegram at the centre of this stack; a Telegram-mediated call may beat *both* a browser tab and a native app on reliability and on the safety surface (§8.7 minors constraints), because the identity and the reporting path are already there.
- **Keep speaking in the browser**, accept the suspension risk for the pilot, and measure drop rates before spending anything.
- **A thin native shell later, for speaking only** — the highest-cost option, justified only if the data demands it.

This does not block the decision in this ADR. The exam simulator, drills, booking, results, and the speaking *queue/booking* are all browser surfaces regardless of how the *call itself* is ultimately carried. Flagged, not resolved.

---

## 9. Consequences

**Positive**

- One codebase, one toolchain, one contract-checked client for both admin and student — the same `openapi-fetch` + `web-codegen-check` machinery, so a schema change is a compile error on both sides in the same CI run.
- No app-store review, no $99/year, no release latency, no "install our app" step between a centre's Telegram group and a student sitting a mock.
- The separation the owner asked for is preserved: student and admin are distinct origins over a shared API, and the trust boundary stays on the server where it already is.
- Ships on infrastructure that already exists and is proven by the admin console — a second static root behind the same Caddy.
- The resilience design (IndexedDB outbox, idempotent replay, server-authoritative clock) is already specified and already the right design for a browser on a bad network.

**Negative, accepted**

- No iOS background push until (and unless) the user installs the PWA; mitigated by Telegram as the notification channel and by the fact that no push exists today (§7).
- Speaking is not in the pilot (§8). The pilot ships Reading, Listening and Writing plus booking and results; the live call and its transport are decided later, which is a deliberate scope cut, not an oversight.
- A contained auth change is now owed (§6): three `/auth` endpoints move the refresh token from the JSON body to a `Set-Cookie`, and `/auth/refresh` reads it from the cookie. **The admin console adopts the same cookie path** — it shares the exposure, and one auth design across both browser clients is worth more than isolating the change to the new one.
- "Exam vibe" is now our job to build in CSS and layout rather than something a native chrome supplies — but it was always going to be, on any client.

---

## 10. Decisions (ratified 2026-08-13)

All four were put to the owner and answered. This section is the record.

1. **A website, large-screen-first, no native and no desktop app** (§2). **Confirmed.**
2. **Refresh token in the browser** (§6): **httpOnly, Secure, SameSite cookie.** Chosen over accepting the `localStorage` risk. Applies to the admin console as well, since it shares the exposure (§9).
3. **Origin** (§5–6): **same-origin behind Caddy.** No CORS; the student site is a second static root reverse-proxied to the same API, exactly as the admin console already is.
4. **Speaking session** (§8): **deferred for the pilot.** Booking and queue remain ordinary browser screens; the live call and its transport are decided later.

### What these unlock, in build order

Not part of the decision, but the sequencing the decisions imply, so the next step is not a blank page:

| # | Work | Depends on |
|---|---|---|
| 1 | **Auth: move the refresh token to an httpOnly cookie** across `/auth/otp/verify`, `/auth/telegram/verify`, `/auth/refresh`; migrate the admin console to it. | Decision 2. Do this first — it is a backend change, and both clients want it settled before they store a token. |
| 2 | **Caddy + a second Vite build**: student SPA on its own origin, same-origin-proxied to the API. | Decision 3. Config, not architecture. |
| 3 | **The exam simulator**: large-screen Reading (side-by-side), Listening (server-timed single play), Writing (word-count, keyboard), chrome-free. Wired to the endpoints that already exist. | The product itself. |
| 4 | **The drill/responsive surface**: booking, results, band history, review, vocab — the same SPA, phone-friendly. | — |

Speaking (§8) and iOS push (§7) are explicitly not on this list.
