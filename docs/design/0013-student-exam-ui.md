# 0013 — The student exam UI: the real IELTS computer-delivered interface

**Status:** design spec, implementation started
**Implements:** ADR-0002 (the student client is a website, large-screen-first)
**Supersedes:** `0012-student-app-design-brief.md`, which specified a mobile app

---

## 0. What this is, and the one thing it is not

The owner's brief was *"exactly the same design as the real IELTS exam app."* That
is the right instinct and it is what this document specifies — with one line
drawn deliberately through the middle of it.

**What we copy: the interaction design.** Where the timer sits, which side the
passage is on, what the question palette does, how highlighting is invoked, what
happens at five minutes remaining. This is what produces *transfer* — a student
who has sat twenty of our mocks should walk into a real test centre and recognise
the machine. Every one of these is a functional convention, they are documented
publicly by the test owners, and matching them is the entire product.

**What we never copy: the brand.** No IELTS logo, no British Council or IDP or
Cambridge mark, no lifted stylesheet or image asset, and the product is not
called IELTS. ADR-0001 §9.3 already settled this: *"'IELTS' is a registered
trademark… Descriptive use — 'IELTS preparation platform' — is generally
defensible; using it in your product or company name invites a letter."*

So: **identical behaviour, our own skin.** A student must find the layout
familiar and never mistake the product for the official test. Practically, that
means our own colour tokens, our own type ramp, our own wordmark — and pixel
positions, control names and timing rules that match the real thing.

### Provenance of everything below

Every fact in §2–§6 marked **[verified]** was checked against the test owners'
own published material in August 2026 — sources listed in §9. Anything I could
not confirm from an official page is marked **[unconfirmed]** and says what to do
about it. That distinction is load-bearing: a spec that states an invented detail
with the same confidence as a checked one is worse than one that admits the gap,
because nobody re-checks the parts that sound certain.

Two of my own assumptions were **wrong** and are corrected here — I had the timer
in the top-right (it is top-**centre**) and had carried over the paper test's
10-minute transfer time (computer-delivered gives **2 minutes**, and not for
transferring anything). Both would have shipped as "exam-realistic" and been
neither.

---

## 1. The shell — three fixed regions

One full-viewport, **non-scrolling** application shell. The page itself never
scrolls; only the panes inside it do. This is the single most important
structural fact, because a page that scrolls as a whole destroys the fixed-timer,
fixed-palette layout that makes the exam feel like the exam.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  TOP BAR (fixed)                                                          │
│   left: candidate identity   centre: TIMER   right: volume · settings ·  │
│                                                              help        │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  BODY — the only region that scrolls, and it scrolls per pane            │
│                                                                          │
│   Reading:   passage (left)  │  questions (right)   ← draggable divider  │
│   Listening: questions, full width, audio playing                        │
│   Writing:   prompt + image (left) │ response editor (right)             │
│                                                                          │
├──────────────────────────────────────────────────────────────────────────┤
│  BOTTOM BAR (fixed)                                                      │
│   left: [Review]   centre: 1 2 3 4 … 40 palette   right: ◀ ▶            │
└──────────────────────────────────────────────────────────────────────────┘
```

### 1.1 Top bar

| Element | Position | Behaviour |
|---|---|---|
| Candidate identity | left | Name and candidate number in the real test. Ours: name and a mock reference. |
| **Timer** | **top centre** [verified] | Counts down. **Flashes at 10 minutes and again at 5 minutes remaining** [verified], for Reading and Writing. |
| Volume | **upper right** [verified] | Listening only. A control bar, adjustable throughout. |
| **Settings** | **upper right** [verified] | Opens text-size and background-colour options. §5. |
| Help | upper right | Instructions for the current question type. |

The timer is top-centre and not tucked in a corner because it is meant to be
unavoidable. Ours renders from a **server** delta — never the device clock; see
§7.

### 1.2 Bottom bar — the question palette

| Element | Position | Behaviour |
|---|---|---|
| **Review** button | **lower left** [verified] | Flags the current question. **The question's marker changes from a square to a circle** [verified] — that is the real visual language, and we use it. |
| Palette | centre | **All 40 questions of the section** [verified], grouped by part. Click any number to jump. |
| Prev / Next | right | Step one question. |

Palette marker states, all now **[verified]**:

| State | Marker |
|---|---|
| Unanswered | square, no underline |
| Answered | **underlined** — "a line appears beneath the question once it is answered" |
| Flagged for review | **circle**, carrying its own answered underline |
| Current | square/circle with a heavy focus ring |

The answered state is an **underline, not a fill**, and this document said "filled"
until it was checked. It matters: a filled marker and a circled marker compete for
the same glance, whereas an underline sits under the shape and reads independently
of it. Both signals are legible at once, which is the whole job of the bar.

---

## 2. Listening [verified]

| | |
|---|---|
| Structure | 4 parts, 10 questions each, **40 total** |
| Duration | **30–34 minutes** of audio |
| Replay | **Each recording is heard ONCE only.** No pause, no rewind, no replay. |
| Volume | Adjustable throughout, upper-right control bar |
| At the end | **2 minutes to check answers** |
| Transfer time | **None.** Answers are typed straight in. |

**The 2-minutes point is where most clones get it wrong, including my own first
draft.** The paper test gives 10 minutes to transfer answers to an answer sheet.
Computer-delivered has no answer sheet, so there is nothing to transfer: you get
**2 minutes** to review, and that is all. A practice product that grants 10
minutes is training a habit the real test will punish.

Questions are on screen while the audio plays, so the candidate reads ahead and
answers in flight. The audio is not a media player with a scrubber — it is a
transport the candidate cannot touch beyond volume.

**Play-once is enforced on the server**, not by hiding a button. See §7.

---

## 3. Reading [verified]

| | |
|---|---|
| Structure | 3 passages, **40 questions**, free navigation across all of them |
| Duration | **60 minutes** |
| Transfer time | **None** |
| Layout | **Passage on the LEFT, questions on the RIGHT** |
| Divider | Draggable, so the candidate rebalances the split |

### 3.1 Highlighting and notes [verified]

The exact interaction, in the test owner's own words:

> "Simply left click and drag your cursor over the section of text you wish to
> highlight, then right click and select the 'highlight' option. To remove a
> highlight, simply right click on the highlighted area and select **'clear
> all'**."

Notes work the same way, and the wording is the test owner's own:

> "To make notes on a section of the test, left click and drag the cursor over the
> selection of text or question you want to make notes on, then right click and
> select the **'Notes'** option."

So: **select → right-click → Highlight / Notes / Clear all.** Not a toolbar
button, not a floating bubble on selection. The menu item is called **Notes** —
not "Add note", which is what this document guessed before it was checked.

What a note LOOKS like, and how it is edited or deleted, is **[unconfirmed]** in
official prose — no test owner documents it. The familiarisation application's own
`notes.js` builds a `div.note` with a `div.close` handler, which is evidence for a
closable floating box rather than a confirmed specification.

This matters more than it looks. Right-click is the muscle memory; a product that
puts a highlighter button in a toolbar teaches the wrong reflex, and the student
loses seconds in the real test hunting for a menu that is not there.

**Highlights and notes are client-side only.** They are scratch, they never leave
the browser, and they are never sent to `/answers` — they are not answers and
storing them would be recording a student's private thinking for no purpose.

---

## 4. Writing [verified]

| | |
|---|---|
| Structure | Task 1 and Task 2, one continuous session |
| Duration | **60 minutes total** |
| Task 1 | **≥150 words**, ~**20 minutes** recommended. Academic: describe a supplied graph, table, chart or diagram. |
| Task 2 | **≥250 words**, ~**40 minutes** recommended. Discuss a point of view, argument or problem. Worth **twice** Task 1. |
| Word count | **Automatic and live on screen** |
| Editing | **Cut / copy / paste**, and drag to move a paragraph |
| **Spell check** | **ABSENT, deliberately.** "the IELTS on Computer test does not have a spell-check function." |

The absent spell-check is a *feature to implement*, not an omission to fix. The
response editor must therefore set `spellcheck="false"` and suppress autocorrect,
autocapitalise and autocomplete — a browser textarea does all four by default, and
every one of them trains a habit the real test does not permit and the marking
does penalise.

Task 1's visual sits beside the editor, and must be zoomable: a candidate reading
a chart at 1280px needs it larger without losing the response pane.

The recommended 20/40 split is **advice, not enforcement** — the real test does
not stop you writing Task 1 at minute 45, and neither do we. Show elapsed time
per task; never lock a task.

---

## 5. Settings and accessibility [verified]

Reached from the **Settings button, upper right**:

- **Text size** [verified] — exactly **three** steps, labelled **Standard**,
  **Large**, **Extra large**. They are whole-interface **zoom multipliers** —
  1.0, 1.2, 1.4 — not a body-copy font change. Ours scales the root font-size by
  the same factors, with every length in the stylesheet in `rem`, so the effect
  matches.
- **Colours** [verified] — **four** options, under a heading spelled "Colours".
  Note that the real **"Standard" is NOT black-on-white**: the engine ships an
  empty override stylesheet, so Standard is its own chrome — black on a pale
  blue-grey. Ours is our own colour, but the *tinted* default is kept, because a
  full-white page at exam brightness is fatiguing over three hours.
- The real panel also carries a **Screen Resolution** group, which is stripped out
  in live test-centre delivery. We do not ship it.
- **Persistence across sections** is **[unconfirmed]** — the real engine holds
  these in module-scoped variables that reset on page load, which suggests it does
  not persist them. **We deliberately do.** A student who needs extra-large needs
  it in every section, and making them set it four times would be a worse product
  than the one we are imitating. This is a place where matching the real client
  exactly would be the wrong call.

These are not decoration. A student who has practised at extra-large will look
for that control under pressure, and the real test has it.

**Keyboard**: every control reachable by Tab, the palette navigable by arrow
keys, and a visible focus ring that survives the colour themes. A candidate whose
mouse dies mid-exam must be able to finish.

---

## 6. What a practice product must add — and why the real exam lacks it

The real test is a measuring instrument. We are a teaching instrument, so the
difference is deliberate and belongs in **practice mode only**. Exam mode is the
real thing.

| | Exam mode | Practice mode |
|---|---|---|
| Listening audio | **Once**, server-enforced | Replay freely, scrub, pause |
| Timer | Server-authoritative, hard stop | Pausable; can be turned off |
| Answers | Revealed only after submit | "Show answer" and explanation per question |
| Navigation | Within the section only | Jump anywhere, leave and resume |
| Marking | On submit, server | Instant per-question feedback |

The failure mode to avoid is letting practice affordances leak into exam mode.
Exam mode must feel like the exam **including the parts that are unpleasant** —
one hearing, a clock that does not stop, no confirmation that you got it right.
A mock that is gentler than the test is not a mock.

---

## 7. Where we necessarily differ from the real client

These are not aesthetic choices; they fall out of constraints already decided.

1. **The server owns the clock.** `GET /attempts/{xid}` returns `server_now`
   beside `expires_at`; the countdown renders from that delta and re-syncs on
   every `POST /answers` response, which is documented as *"the response is the
   clock sync."* A cheap Android handset can be minutes out, and the device clock
   is never trusted. Section clocks start server-side at
   `POST /attempts/{xid}/sections/{position}/enter`.

2. **Play-once is a server grant, not a hidden button.** Exam-mode audio requires
   `POST /attempts/{xid}/sections/{position}/audio-grant`, which issues one
   short-TTL, per-user token. Hiding the replay control in the UI would be
   theatre — anyone with devtools replays at will. Practice mode simply asks for
   grants freely.

3. **Answers save continuously, not on submit.** An IndexedDB outbox flushes to
   `POST /attempts/{xid}/answers` every 5–10 s and on blur, batched, with a
   required `Idempotency-Key` and per-slot `client_seq` ordering. This is the
   documented contract of *"the load-bearing endpoint of the whole product"*, and
   it is what makes a 60-minute exam survivable on a Tashkent connection: a
   dropped request costs one round trip, not one answer.

4. **No proctoring.** ADR-0001 §7 refuses webcam and lockdown-browser proctoring,
   and specifically refuses recording minors' webcams. Focus-loss can be recorded
   as a *signal for the teacher*; it is never presented as an accusation and
   never blocks the attempt.

5. **At zero the test stops by itself** [verified] — "The tests will
   automatically stop when the time finishes." There is no candidate submit
   action and no confirmation dialog; answers already saved are kept. Ours is the
   same, and it is safe precisely because answers were never held for a final
   submit: the outbox has been flushing them every 5–10 seconds all along.

6. **Two languages in the chrome.** Interface in Uzbek (Latin) and Russian, exam
   content in English. The real client is English-only; a student in Tashkent
   should not have to parse English navigation to sit an English exam.

---

## 8. Screens, and the endpoints behind each

Every screen names its endpoint. A screen needing data no endpoint returns cannot
be built, and this is the list that keeps that honest.

| Screen | Endpoints |
|---|---|
| Sign in | `POST /auth/otp/request`, `POST /auth/otp/verify`, `POST /auth/telegram/verify` |
| Cold start | `POST /auth/refresh` (no body — httpOnly cookie), `GET /auth/session` |
| Home / assigned work | `GET /assignments`, `GET /me/progress`, `GET /me/entitlements` |
| Start an attempt | `POST /attempts` (`Idempotency-Key`) |
| **Exam runner** | `GET /attempts/{xid}/payload`, `POST …/sections/{position}/enter`, `POST …/sections/{position}/audio-grant`, `POST /attempts/{xid}/answers`, `GET /attempts/{xid}` |
| Submit | `POST /attempts/{xid}/submit` (`Idempotency-Key`) |
| Result | `GET /attempts/{xid}/result` |
| Review answers | `GET /attempts/{xid}/review` |
| Competitions | `GET /competitions`, `…/lobby`, `…/register`, `…/leaderboard`, `…/key` |
| Account | `GET /me/consents`, `GET /me/devices`, `POST /auth/logout` |

Speaking is **not in the pilot** (ADR-0002 §8).

### 8.1 Responsive behaviour

Large-screen-first, and honest about the small screen:

- **≥1024px** — the exam runner as specified. Split panes, full palette.
- **<1024px** — the exam runner shows an interstitial: *"Reading and Writing need
  a larger screen. Use a laptop or tablet."* This is a **feature**. Letting a
  student sit a Reading mock on a phone trains scrolling, which the real test
  does not test.
- **All sizes** — home, results, band history, review, booking, account. These
  are the drill surface and they belong on a phone.

Listening is the arguable case: audio plus short answers genuinely works on a
phone. Allow it in practice mode, gate exam mode behind the same width rule as
the rest, so a mock is always sat under mock conditions.

---

## 9. Sources

Checked August 2026. Everything marked [verified] traces to one of these.

- British Council — [IELTS on computer: how it works](https://takeielts.britishcouncil.org/take-ielts/prepare/free-ielts-english-practice-tests/ielts-on-computer/how-it-works)
- British Council — [Highlighting text](https://takeielts.britishcouncil.org/take-ielts/prepare/free-ielts-english-practice-tests/ielts-on-computer/about/highlighting-text) (the highlight / "clear all" wording)
- British Council — [Reading section](https://takeielts.britishcouncil.org/take-ielts/prepare/free-ielts-english-practice-tests/ielts-on-computer/about/reading)
- British Council — [Listening section](https://takeielts.britishcouncil.org/take-ielts/prepare/free-ielts-english-practice-tests/ielts-on-computer/about/listening)
- IDP — [How IELTS on computer works](https://ielts.idp.com/canada/prepare/article-how-computer-delivered-ielts-works) (Review button lower-left, square→circle; navigation bar; Settings upper-right)
- IDP — [Using IELTS on Computer features to your advantage](https://ielts.idp.com/srilanka/about/news-and-articles/article-how-to-use-ielts-on-computer-features-to-your-advantage) (timer upper-middle, flashes at 10 and 5 minutes; volume upper-right)
- IELTS.org — [Academic Writing format](https://ielts.org/take-a-test/test-types/ielts-academic-test/ielts-academic-format-writing) (60 min, 150/250 words, 20/40 split, Task 2 worth double)
- British Council — [What you need to know about IELTS on computer](https://takeielts.britishcouncil.org/blog/ielts-on-computer-changes-updates) (no spell-check; automatic word count)

### What has since been settled, and what has not

The four items this document originally left **[unconfirmed]** have been checked
against the test owners' pages and against the official familiarisation
application's own source. Three are now verified and two of them **corrected a
mistake in this document**: answered questions are underlined rather than filled,
and the note menu item is called "Notes". The text-size steps and the four-option
colour list are confirmed, along with the fact that "Standard" is not white.

Still genuinely unknown, and marked as such above:

- what a note looks like once created, and how it is edited or deleted;
- whether the real client keeps display settings across sections (we keep them,
  deliberately — §5);
- whether there is an end-of-section review/summary screen. No official source
  describes one; reviewing appears to be done in place through the bottom bar for
  the whole of the section.

Sitting the official free familiarisation test once would settle all three. It is
an hour and it is free.
