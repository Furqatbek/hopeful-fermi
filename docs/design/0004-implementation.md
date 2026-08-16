# Deliverable 4 — Implementation (phases 0–4)

> **Read this as a record, not as the current state.** It describes the system on
> the date below, and its counts are frozen there. What the platform does *today*
> is `docs/design/0014-platform-flow.md`; what is known to be wrong today is
> `docs/known-issues.md`. Rewriting a design record would erase why a thing was
> built, so this is annotated rather than edited.

- **Status:** Delivered. A record of phases 0–4; the suite and the contract have
  both grown well past the numbers below.
- **Date:** 2026-07-29
- **Depends on:** ADR-0001, `0002-data-model.md`, `0003-api-contract.md`
- **Build order:** ADR-0001 §12, as agreed. Each phase complete and green before the next.

---

## 0. What is delivered, and what is not

**Delivered — the four named test targets, tests first, all green:**

| Phase | Module | Tests | State |
|---|---|---|---|
| 0 | `app/platform` — injectable clock, error hierarchy, findings | (exercised throughout) | ✅ |
| 1 | `app/modules/qtypes` — registry, tolerance, 3 primitives, scorer | **124** | ✅ |
| 2 | `app/modules/content` — publish gate (19 checks) | **40** | ✅ |
| 3 | `app/modules/exam` — scoring + regrade | **18** | ✅ |
| 4 | `app/modules/billing` — entitlements | **30** | ✅ |
| | **Total** | **212 passed in 0.28 s** | |

Plus, re-verified after this phase's registry changes:

```
migrations applied: 17        types seeded: 17
import contracts:   5 kept, 0 broken
openapi:            PASS  paths=113 operations=149 schemas=144
acceptance test:    PASS — new question type end to end, DDL changed: False
```

**Not delivered in this phase, and I want to be exact about it rather than let a
green test count imply more than it should:**

* No HTTP layer. `app/api/` is an empty package with a docstring. The domain core
  is complete and tested; the routers that expose it over the contract in
  Deliverable 3 are not written.
* No repositories. Every module here takes its data as value objects
  (`TestComposition`, `AttemptInput`, `EntitlementStore`), so the persistence
  layer is a mechanical mapping from the migrations — but it is not written.
* `identity`, `authz`, `safety`, `competitions`, `speaking`, `analytics` are
  package stubs. They exist now only so the import-linter contracts are live from
  the first commit that adds code to them, rather than being switched on later
  when they are already violated.

That is phases 0–4 of an eight-phase plan. The remaining phases are ordinary work
against a core that is now pinned by tests; these four were the ones where being
wrong is expensive and invisible.

---

## 1. Phase 1 — the scoring engine

### 1.1 Shape

```
qtypes/
  schemas.py       value types + NORMALIZER_ORDER (the canonical pipeline order)
  lexicon.py       spelling / number / contraction tables, built once, held in process
  wordlimit.py     "NO MORE THAN TWO WORDS AND/OR A NUMBER", enforced
  normalizers.py   ten normalizers + the Pipeline that runs them in canonical order
  primitives.py    choice_per_slot · text_per_slot · set_selection
  registry.py      Registry, Scorer, ScoreRequest
```

Plain dataclasses, not Pydantic. The scoring path runs ~40 slots × 50 k attempts a
year and should stay dependency-light; Pydantic earns its keep at the API
boundary, where JSON Schema generation is the point.

### 1.2 Two decisions worth challenging

**Normalizer order is code, not data.** A definition *selects* from
`NORMALIZER_ORDER`; it cannot reorder it. `ScoringSpec.from_dict` sorts whatever
the JSON says into canonical order. Applying `casefold` after `spelling_uk_us`
would silently break every lexicon lookup, and no author should be able to cause
that by writing their JSON in a different sequence. The cost is one axis of
flexibility that nobody has asked for; adding a normalizer needs a deploy anyway
(ADR-0001 §8.3), so its position is decided at the same time as its code.

**`unique_options` is an authoring constraint, not a scoring rule.** A student who
uses the same heading for two paragraphs gets at most one of them right, marked
independently — which is how the real exam marks. Penalising the duplicate would
diverge from it. The publish gate enforces uniqueness on the *key*
(`KEY_OPTION_REUSED`); the scorer stays neutral.

### 1.3 Where the tests changed the implementation

**The word-limit rule was under-specified, and my first version was too generous.**
`NO MORE THAN TWO WORDS AND/OR A NUMBER` with two numbers: my original code let
surplus numbers spend the word allowance, so `14 September 1999` passed a
two-word limit. That leniency was invented, not inherited from the exam. It now
enforces two independent limits — `words <= max_words` **and** `numbers <= 1` —
so that answer is correctly rejected. Being more generous than real marking is
the specific failure this module exists to prevent, and the parametrised case is
what surfaced it.

**`ordinal_digit` was missing from five of the eight text types.** `table_completion`,
`form_completion` and `note_completion` had it; `sentence_completion`,
`short_answer`, `summary_completion`, `flowchart_completion` and
`diagram_completion` did not. There is no principled reason a table accepts `19th`
for `nineteenth` and a sentence does not. Fixed in the registry JSON — a data
change with no code change, which is the design working.

### 1.4 The example I had been using is wrong

I have used "38 students wrote *car park*, your key only accepts *carpark*"
throughout Deliverables 2 and 3 as the canonical broken key. Writing the regrade
tests proved it is not one: `car park` ⇄ `carpark` is in the spelling lexicon, so
the scorer already accepts it and it never reaches a regrade.

That distinction is worth having explicitly, so it is now a test class of its own
(`TestToleranceCatchesItFirst`):

* **Variants** — spelling, number form, articles, hyphens, case, punctuation — are
  absorbed at scoring time and are invisible. They never generate a regrade.
* **Missing synonyms** — `bike` for `bicycle`, `physician` for `doctor` — are
  genuinely wrong keys that no normalizer can rescue. These are what the regrade
  path is for.

I have corrected the illustration in the migration comment, both design docs and
the OpenAPI description. It was a small factual error in shipped documentation and
it would have set the wrong expectation about what `common_wrong` surfaces.

---

## 2. Phase 2 — the publish gate

19 checks, and the two properties that matter more than any of them individually:

1. **A sound test publishes cleanly.** A gate that cries wolf gets bypassed, and a
   bypassed gate is worse than none. Two tests assert clean passes for a complete
   Reading and a complete Listening test.
2. **Every problem in one pass.** `test_multiple_independent_faults_all_surface`
   builds a deliberately broken test and asserts eight distinct codes come back
   together. Returning the first failure would mean an author fixes one thing,
   republishes, waits, and finds the next — nineteen times.

Two supporting guarantees are also tested:

* **Errors and warnings are separated.** A missing section time limit is a warning
  and does not block publication; a missing key is an error and does.
* **Every finding carries a `path` and a `fix_hint`.** Asserted for all findings,
  not spot-checked. A finding the UI cannot deep-link to, or that tells the author
  nothing actionable, is close to useless.

The gate is a pure function of `(TestComposition, Registry)` — no database, no
clock. That is what lets it run on a draft the author is still editing, which is
exactly when they want it.

---

## 3. Phase 3 — the regrade path

Scoring is a pure function of `(responses, key_versions, band_map_version,
engine_version)`, and the tests pin each part of that:

* the same inputs always reproduce the same run,
* a run records exactly which key version and engine version produced it,
* an item with no key is **void**, not incorrect — the student did nothing wrong.

The flow, as tested end to end at realistic scale (`TestTheRealisticScenario`, 50
attempts):

```
plan()   dry run: 38 of 50 attempts change, 38 bands move, 0 students lose marks
apply()  38 new score runs; previous runs retained, so score history survives
notify() 38 notices — band changes only, each with a dedupe key
```

`impact.worsened == 0` is asserted, because a key *fix* must never cost a student
marks; if it does, the fix is wrong.

**The competition governance rule is structural, not documentary.** When any
affected attempt belongs to a competition whose ranking would move, `apply()`
raises `Conflict` (409) listing the competitions, until a decision is recorded. A
leaderboard that changes by itself looks like fraud, so refusing is the schema and
the code, not a convention someone has to remember. Practice attempts regrade with
no ceremony at all, and a key fix that moves nobody needs no decision either.

---

## 4. Phase 4 — entitlements

One call site: `Entitlements.check()`. Nothing reads `orders` or `payments`, and
there is no second implementation of "has this student paid" in feature code.

Resolution order, tested: personal grant → seat against an org licence → org-wide
grant. A student who paid privately keeps their own plan inside a centre, so
leaving the centre does not silently revoke what they bought.

Three rules worth calling out:

* **A seat licence requires an assigned seat.** Without it, buying 10 seats would
  entitle a 400-student centre. Released seats stop covering immediately.
* **Revocation beats expiry.** A refund or a ban must bite now, regardless of the
  paid-through date.
* **The most informative denial wins.** The client says "your plan expired" rather
  than the useless "you have no plan". Easy to lose in a refactor, so it is tested.

`consume()` checks and decrements in one call rather than exposing a
check-then-consume pair. Every caller that forgets the second half is a consumable
the student keeps for free.

---

## 5. Module boundaries are now enforced

`import-linter`, five contracts, all kept, running in CI:

| Contract | What it prevents |
|---|---|
| Modules never import `api` or `workers` | Domain code reaching up into transport |
| Layers: `api` → `modules` → `platform` | Any upward dependency |
| `qtypes` depends on `platform` only | The registry acquiring domain knowledge |
| `analytics` reads no other module | The seam that lets it move to a replica later |
| `billing` depends on neither `content` nor `exam` | Entitlements becoming feature-aware |

This is the mechanism that makes "modular monolith" a fact rather than an
aspiration (ADR-0001 §4.3). It fails the build, not a code review.

---

## 6. Things to push back on

1. **Coverage is deep but narrow.** 212 tests over four modules is thorough for
   what they cover, and covers roughly a third of the system. The count should not
   read as "the backend is a third built" — the untested two-thirds is mostly
   mechanical (repositories, routers), which is precisely why I front-loaded these.

2. **No integration test spanning HTTP → DB yet.** The database invariants are
   proven separately (`scripts/verify_invariants.sql`) and the domain logic is
   proven here, but nothing yet exercises both at once. That belongs in phase 5,
   with `testcontainers`.

3. **`_parse_number` handles compounds up to millions and declines beyond.** It
   parses `twenty five`, `three hundred and fifty`, `two thousand`. Anything it
   cannot parse is left untouched rather than guessed at. If a centre writes keys
   with `one and a half`, that is a lexicon row, not a parser change.

4. **`text_per_slot` re-normalizes accepted alternatives on every score.** For a
   40-item test that is ~120 normalizations, microseconds total. It could be
   precomputed at publish, but then a lexicon fix would not apply to already
   published content — which is the wrong trade for a tolerance system that exists
   to be corrected.

5. **The word-limit strictness in §1.3 is a judgment call.** I chose the reading
   that never awards more than the real exam. If your centres tell you that
   `14 September 1999` should pass a TWO WORDS AND/OR A NUMBER rule, it is a
   one-line change and one test.

---

## Deliverable 4 (phases 0–4) ends here.

Next, on your word: phases 5–8 — repositories, the HTTP layer against the D3
contract, the import pipeline, and the exam session lifecycle. Or Deliverable 5
(scaling triggers) first, if you would rather have the whole design settled before
more code.
