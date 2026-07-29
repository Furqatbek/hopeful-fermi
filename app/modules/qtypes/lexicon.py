"""Tolerance lexicon.

Spelling variants and number-word forms are DATA (`lexicon_entries`), so a
platform admin can add a missing pair without a deploy — typically prompted by
`item_stats.common_wrong` showing 38 students wrote a form the key rejects.

The whole table is small (a few hundred rows) and is held in process, refreshed
on a Redis pub/sub invalidation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


@dataclass(frozen=True, slots=True)
class LexEntry:
    kind: str
    a: str
    b: str
    bidirectional: bool = True


class LexiconSource(Protocol):
    def entries(self) -> Iterable[LexEntry]: ...


class Lexicon:
    """Canonicalizing lookup tables built once from the entry list.

    Canonical direction is deliberately `b -> a`: for spelling variants `a` holds
    the British form, for number words `a` holds the digit form. Both sides of a
    pair therefore collapse onto one representation, which is all equality needs.
    """

    __slots__ = ("_token", "_phrase", "_max_phrase_len", "_contractions")

    def __init__(self, entries: Iterable[LexEntry] = ()) -> None:
        self._token: dict[str, dict[str, str]] = {}
        self._phrase: dict[str, dict[tuple[str, ...], tuple[str, ...]]] = {}
        self._contractions: dict[str, str] = {}
        self._max_phrase_len = 1
        for e in entries:
            self.add(e)

    def add(self, e: LexEntry) -> None:
        a_tokens, b_tokens = tuple(e.a.split()), tuple(e.b.split())
        if e.kind == "contraction":
            self._contractions[e.a] = e.b
            if e.bidirectional:
                self._contractions.setdefault(e.b, e.b)
            return
        if len(a_tokens) > 1 or len(b_tokens) > 1:
            table = self._phrase.setdefault(e.kind, {})
            table[b_tokens] = a_tokens
            if e.bidirectional:
                table.setdefault(a_tokens, a_tokens)
            self._max_phrase_len = max(self._max_phrase_len, len(a_tokens), len(b_tokens))
            return
        table_t = self._token.setdefault(e.kind, {})
        # `b -> a`. setdefault, not assignment: the first entry wins, so a later
        # one-way pair cannot quietly redirect an existing canonical form.
        table_t.setdefault(e.b, e.a)
        if e.bidirectional:
            table_t.setdefault(e.a, e.a)

    # ── lookups ──────────────────────────────────────────────────────
    def canonical_token(self, kind: str, token: str) -> str:
        return self._token.get(kind, {}).get(token, token)

    def expand_contraction(self, token: str) -> str:
        return self._contractions.get(token, token)

    def apply_phrases(self, kind: str, tokens: list[str]) -> list[str]:
        """Longest-match n-gram replacement, e.g. `car park` -> `carpark`."""
        table = self._phrase.get(kind)
        if not table:
            return tokens
        out: list[str] = []
        i = 0
        n = len(tokens)
        while i < n:
            for size in range(min(self._max_phrase_len, n - i), 0, -1):
                gram = tuple(tokens[i : i + size])
                if gram in table:
                    out.extend(table[gram])
                    i += size
                    break
            else:
                out.append(tokens[i])
                i += 1
        return out

    def has_kind(self, kind: str) -> bool:
        return kind in self._token or kind in self._phrase


class StaticLexiconSource:
    """Loads the JSON files under `registry/lexicon/`. Used in dev and in tests."""

    def __init__(self, rows: Iterable[dict]) -> None:
        self._rows = list(rows)

    def entries(self) -> Iterable[LexEntry]:
        for r in self._rows:
            yield LexEntry(
                kind=r["kind"],
                a=r["a"],
                b=r["b"],
                bidirectional=bool(r.get("bidirectional", True)),
            )
