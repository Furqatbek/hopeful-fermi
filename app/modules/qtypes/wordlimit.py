"""Word-limit enforcement.

`NO MORE THAN TWO WORDS AND/OR A NUMBER` is a scoring rule, not decoration on the
instruction line. The real IELTS marking behaviour encoded here:

  * The count runs on the RAW trimmed response, BEFORE normalization, because the
    rule counts the words the student actually wrote. Normalizing first would let
    `strip_articles` turn a three-word over-limit answer into a legal two-word one.
  * A hyphenated compound counts as ONE word (`well-known`).
  * A contraction counts as ONE word (`don't`) — it is one whitespace token, and
    contraction expansion happens later in the pipeline.
  * `AND/OR A NUMBER` grants exactly ONE number on top of the word allowance.
    Additional numbers fall back to counting as words.
  * Exceeding the limit marks the answer WRONG OUTRIGHT. It is never truncated
    and re-compared, which would silently award marks the real exam would not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .schemas import WordLimit

# Digits with optional thousands/decimal separators, ranges, times and simple
# units: 14, 3.5, 1,200, 9:30, 12/05, 20%, $15, 3km
_NUMBER_RE = re.compile(r"^[£$€]?\d+(?:[.,:/-]\d+)*\s*%?[a-z]{0,3}$")
_EDGE_PUNCT = "\"'“”‘’.,;:!?()[]{}…"


@dataclass(frozen=True, slots=True)
class WordCount:
    words: int
    numbers: int
    tokens: tuple[str, ...]

    @property
    def total(self) -> int:
        return self.words + self.numbers


def count(raw: str, rule: WordLimit) -> WordCount:
    words = 0
    numbers = 0
    tokens = tuple(t for t in raw.strip().split() if t.strip(_EDGE_PUNCT))
    for token in tokens:
        core = token.strip(_EDGE_PUNCT).casefold()
        if not core:
            continue
        if _NUMBER_RE.match(core):
            numbers += 1
        elif rule.hyphen_counts_as_one:
            words += 1
        else:
            words += core.count("-") + 1
    return WordCount(words=words, numbers=numbers, tokens=tokens)


def violates(raw: str, rule: WordLimit | None) -> tuple[bool, dict]:
    """Returns (violated, explain). Explain is always populated, so support can
    show a student exactly how their answer was counted.

    Two independent limits, both of which must hold:

        words   <= max_words
        numbers <= 1            (only when `allow_number`)

    `AND/OR A NUMBER` is singular, so two numbers violates even when the word
    count is comfortable — `14 September 1999` is out under a TWO WORDS AND/OR A
    NUMBER rule. An earlier draft let surplus numbers spend the word allowance
    instead, which accepted that answer; that leniency was invented, not
    inherited from the exam, and being more generous than the real marking is
    the specific failure this module exists to avoid.

    When numbers are not allowed at all they simply count as words, because the
    rule then reads `NO MORE THAN TWO WORDS` and a digit is just a token.
    """
    if rule is None:
        return False, {}
    c = count(raw, rule)

    if rule.allow_number:
        effective_words = c.words
        numbers_ok = c.numbers <= 1
    else:
        effective_words = c.words + c.numbers
        numbers_ok = True

    words_ok = effective_words <= rule.max_words
    violated = not (words_ok and numbers_ok)
    return violated, {
        "max_words": rule.max_words,
        "allow_number": rule.allow_number,
        "hyphen_counts_as_one": rule.hyphen_counts_as_one,
        "counted_words": c.words,
        "counted_numbers": c.numbers,
        "effective_words": effective_words,
        "words_ok": words_ok,
        "numbers_ok": numbers_ok,
        "violated": violated,
    }
