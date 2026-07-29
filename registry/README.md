# Question-type registry

Each file in `question_types/` is one question type, versioned. These files are the
reviewable source of truth; the runtime registry lives in the `question_type_defs`
table and is loaded from here by `ielts qtypes sync` (and once, for a fresh database,
by migration `0017`).

**Adding a type does not require a migration and does not require a redeploy of the
exam engine.** Drop a new JSON file here, run `ielts qtypes sync`, done. The full
end-to-end walkthrough is in `docs/design/0002-data-model.md` section 4.

## Anatomy of a definition

| Field | Purpose |
|---|---|
| `key` + `version` | Content binds to this exact pair. Changing schemas means a new `version`, never an edit in place. |
| `skills` | Which section skills may use the type. The publish gate enforces it. |
| `payload_schema` | JSON Schema for what the **author** writes. |
| `key_schema` | JSON Schema for the **answer key** (versioned separately from the question). |
| `response_schema` | JSON Schema for what the **student** submits. The exam engine validates against this with zero per-type code. |
| `scoring` | A composition over the three closed scoring primitives, plus a normalizer pipeline. |
| `validation` | Cross-field rules JSON Schema cannot express. |
| `authoring` | UI hints. The teacher-facing form is **generated** from this — without it, "no redeploy" would still be false on the frontend. |

## The three scoring primitives

All 19 question types required at MVP decompose into these. This is the closed set;
adding a genuinely new primitive is ~30–50 lines of code plus a deploy, and is
expected to be rare (ADR-0001 §8.3).

| Primitive | Shape | Used by |
|---|---|---|
| `choice_per_slot` | N slots, each answered with an option id | MCQ single, T/F/NG, Y/N/NG, all matching types, summary completion with a word bank, map labelling |
| `text_per_slot` | N slots, each answered with free text, normalized then compared | all completion types, short answer |
| `set_selection` | choose K of N, unordered, partial credit | MCQ multiple answers |

## Normalizer pipeline

Applied in this order by `text_per_slot`. Order matters: the **word-limit check runs
on the raw trimmed response, before normalization**, because the rule counts the
words the student actually wrote.

```
trim → collapse_space → casefold → strip_punctuation
     → hyphen_flexible → strip_articles → contraction_expand
     → spelling_uk_us → number_word → ordinal_digit
```

`spelling_uk_us` and `number_word` resolve against the `lexicon_entries` table, so a
platform admin can add a missing variant without a deploy.

## Word-limit semantics

`{"max_words": 2, "allow_number": true, "hyphen_counts_as_one": true,
  "on_violation": "mark_incorrect"}`

Real IELTS marking rules encoded here:

* A hyphenated compound counts as **one** word.
* A contraction counts as **one** word.
* A token that is entirely digits counts against the `allow_number` allowance, not the
  word count.
* Exceeding the limit marks the answer **wrong outright** — it is never truncated and
  re-compared.
