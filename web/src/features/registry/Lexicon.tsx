/**
 * The tolerance lexicon: which two spellings count as the same answer.
 *
 * This is the direct follow-on from item analysis. The trigger the contract
 * names is exact: an `item_stats.common_wrong` entry shows 38 students wrote a
 * form the answer key does not accept. Adding that pair is ONE ROW, not a
 * release — so the screen is one row too, and it can be opened with the term
 * already filled in from the item-analysis screen:
 *
 *     /lexicon?term=meters          the form the students wrote  → b
 *     /lexicon?term=meters&a=metres and the form the key accepts → a
 *
 * **Which side is which matters.** Canonicalization runs `b → a`: `a` is the
 * form everything collapses onto — the British spelling, or the digits — and `b`
 * is the variant. A pair entered the wrong way round is not symmetrical and not
 * harmless.
 *
 * **Adding a pair that already exists changes nothing, and the server says 201
 * anyway.** The insert is `ON CONFLICT (kind, a, b) DO NOTHING`, and the
 * response echoes the body that was sent rather than the row that exists — so
 * re-adding `colour`/`color` with a new note answers "created" and keeps the old
 * note. This screen checks the pair against the list before sending, because the
 * alternative is a success message for something that did not happen.
 *
 * **`locale` is listed but not offered.** The column exists and the endpoint
 * returns it, and `LexEntry` — the shape the scorer actually consumes — has no
 * locale field at all, so anything entered there would be stored and ignored.
 * Existing rows still show it, because a value that is in the data belongs on
 * the screen that claims to show the data.
 *
 * **A pair added here does not reach the running scorer.** `default_scorer()`
 * builds its lexicon from `registry/lexicon/*.json` on disk, `StaticLexiconSource`
 * is the only implementation of `LexiconSource`, and nothing in the application
 * reads `lexicon_entries` — so the row is stored, listed, and never consulted
 * when an answer is marked. Measured in
 * `tests/integration/test_console_registry.py`, which adds a pair through this
 * endpoint and then scores against it. The screen says what is true: the entry
 * is recorded, and the registry files still have to be updated. Claiming
 * otherwise would send an admin away believing 38 students had been put right.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api, problemText } from "../../api/client";
import { isPlatformAdmin, loadPrincipal } from "../../api/principal";
import type { components } from "../../api/schema";
import "./registry.css";

type Entry = components["schemas"]["LexiconEntry"];
type Kind = Entry["kind"];

/** The five kinds, checked against the contract's enum at compile time rather
 *  than retyped and left to drift. They also mirror the database CHECK
 *  constraint, which is what makes an unrecognised one a 422 and not a 500. */
const KINDS = [
  "spelling_variant",
  "number_word",
  "contraction",
  "article",
  "unit_form",
] as const satisfies readonly Kind[];

/** What each kind is for, in the words a centre admin would use. */
const ABOUT: Record<Kind, string> = {
  spelling_variant: "British and American spellings — metres / meters.",
  number_word: "Digits and words — 14 / fourteen.",
  contraction: "Short forms — don't / do not.",
  article: "A leading a, an or the.",
  unit_form: "Units written differently — km / kilometres.",
};

export function Lexicon() {
  const queries = useQueryClient();
  const [params] = useSearchParams();
  const principal = useQuery({
    queryKey: ["principal"],
    queryFn: loadPrincipal,
    staleTime: Infinity,
  });

  const [filter, setFilter] = useState<Kind | "">("");
  const [kind, setKind] = useState<Kind>("spelling_variant");
  // Lazy initial state, so arriving from item analysis pre-fills the box once
  // and never overwrites what is being typed on a later render.
  const [a, setA] = useState(() => params.get("a") ?? "");
  const [b, setB] = useState(() => params.get("term") ?? params.get("b") ?? "");
  const [bidirectional, setBidirectional] = useState(true);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [added, setAdded] = useState<string | null>(null);

  // Not fetched for a teacher. Both calls are platform-admin only and would be
  // two guaranteed 403s behind a page that has already said so.
  const admin = isPlatformAdmin(principal.data ?? null);
  const shown = useLexicon(filter, admin);
  // Unfiltered, and only for the duplicate check below: a pair that already
  // exists under a kind the table is not currently showing is still a pair that
  // will not be written. When no filter is set these two share a query key and
  // there is one request, not two.
  const all = useLexicon("", admin);

  const add = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/admin/lexicon", {
        body: {
          kind,
          a: a.trim(),
          b: b.trim(),
          bidirectional,
          // Omitted rather than sent empty: `note` is what a later reader uses
          // to tell a deliberate pair from a typo, and "" says nothing.
          ...(note.trim() ? { note: note.trim() } : {}),
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      setAdded(`${a.trim()} / ${b.trim()}`);
      setA("");
      setB("");
      setNote("");
      void queries.invalidateQueries({ queryKey: ["lexicon"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  if (principal.isPending) {
    return <div className="page"><p className="muted">Loading…</p></div>;
  }
  if (!admin) {
    return (
      <div className="page">
        <h1>Tolerance lexicon</h1>
        <p className="muted">
          You do not have access to this. The lexicon decides which spellings are
          treated as the same answer across every centre on the platform, so only
          a platform admin can read or change it.
        </p>
      </div>
    );
  }

  const trimmedA = a.trim();
  const trimmedB = b.trim();
  const duplicate = (all.data ?? []).find(
    (entry) => entry.kind === kind && entry.a === trimmedA && entry.b === trimmedB,
  );
  const ready = trimmedA !== "" && trimmedB !== "" && trimmedA !== trimmedB;

  return (
    <div className="page">
      <h1>Tolerance lexicon</h1>
      <p className="muted">
        Which two forms of an answer are marked the same — <code>metres</code> and{" "}
        <code>meters</code>, <code>14</code> and <code>fourteen</code>. Adding a
        pair is one row.
      </p>
      <p className="muted">
        One thing it does not do: it does not change anything already scored.
        To move marks that have already been given, correct the answer key and
        run a regrade from the Keys screen. New work is marked with this pair
        within a few seconds, on every server.
      </p>

      {error && <p className="error">{error}</p>}
      {shown.isError && <p className="error">{problemText(shown.error)}</p>}

      <h2>Add a pair</h2>
      <form
        className="row lexicon-add"
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          setAdded(null);
          add.mutate();
        }}
      >
        <span>
          <label htmlFor="lx-kind">Kind</label>
          <select
            id="lx-kind"
            value={kind}
            onChange={(event) => setKind(event.target.value as Kind)}
          >
            {KINDS.map((option) => (
              <option key={option} value={option}>{option.replaceAll("_", " ")}</option>
            ))}
          </select>
        </span>
        <span>
          <label htmlFor="lx-a">a — the form the key accepts</label>
          <input
            id="lx-a"
            value={a}
            onChange={(event) => setA(event.target.value)}
            placeholder="metres"
            required
          />
        </span>
        <span>
          <label htmlFor="lx-b">b — the form students wrote</label>
          <input
            id="lx-b"
            value={b}
            onChange={(event) => setB(event.target.value)}
            placeholder="meters"
            required
          />
        </span>
        <span>
          <label htmlFor="lx-note">Note</label>
          <input
            id="lx-note"
            className="wide"
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="38 students wrote this on Mock 1 Q7"
          />
        </span>
        <label className="choice">
          <input
            type="checkbox"
            checked={bidirectional}
            onChange={(event) => setBidirectional(event.target.checked)}
          />
          Both ways
        </label>
        <button disabled={add.isPending || !ready || duplicate !== undefined}>
          {add.isPending ? "Adding…" : "Add"}
        </button>
      </form>

      <p className="muted">
        {ABOUT[kind]} Everything collapses onto <strong>a</strong>, so put the
        form the answer key already accepts there and the form students wrote in{" "}
        <strong>b</strong>. Entered the other way round the pair still works in
        one direction only, which is not what you want.
      </p>

      {trimmedA !== "" && trimmedA === trimmedB && (
        <p className="error">The two forms are the same, so there is nothing to add.</p>
      )}
      {duplicate !== undefined && (
        <p className="error">
          This pair is already in the lexicon
          {duplicate.note ? ` — noted as "${duplicate.note}"` : ""}. Adding it
          again would report success and change nothing, including the note: an
          existing pair is never overwritten. Change one of the two forms, or
          leave it as it is.
        </p>
      )}
      {added && (
        <p className="muted">
          Added <strong>{added}</strong>. It is in the list below, and still has
          to reach the servers' lexicon folder before it marks anything.
        </p>
      )}

      <h2>Entries</h2>
      <div className="row">
        <span>
          <label htmlFor="lx-filter">Show</label>
          <select
            id="lx-filter"
            value={filter}
            onChange={(event) => setFilter(event.target.value as Kind | "")}
          >
            <option value="">every kind</option>
            {KINDS.map((option) => (
              <option key={option} value={option}>{option.replaceAll("_", " ")}</option>
            ))}
          </select>
        </span>
        <span className="muted">
          {shown.data?.length ?? 0} shown of {all.data?.length ?? 0}
        </span>
      </div>

      <table>
        <thead>
          <tr><th>Kind</th><th>a</th><th>b</th><th>Both ways</th><th>Locale</th><th>Note</th></tr>
        </thead>
        <tbody>
          {shown.data?.map((entry) => (
            /* `(kind, a, b)` is the table's unique index, so it is a stable row
               identity. The listing carries no id of its own. */
            <tr key={`${entry.kind}|${entry.a}|${entry.b}`}>
              <td className="muted">{entry.kind.replaceAll("_", " ")}</td>
              <td>{entry.a}</td>
              <td>{entry.b}</td>
              <td className="muted">{entry.bidirectional ? "yes" : "one way"}</td>
              <td className="muted">{entry.locale ?? "—"}</td>
              <td className="muted">{entry.note ?? "—"}</td>
            </tr>
          ))}
          {shown.data?.length === 0 && (
            <tr>
              <td colSpan={6} className="muted">
                {filter
                  ? "No entries of this kind yet."
                  : "The lexicon is empty."}
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <p className="muted">
        There is no way to remove a pair from here — the API has no delete. A pair
        that turns out to be wrong has to be taken out by whoever maintains the
        registry.
      </p>
    </div>
  );
}

/** One kind's entries, or all of them when `kind` is empty. Shared so the table
 *  and the duplicate check ask the same question the same way. */
function useLexicon(kind: Kind | "", enabled: boolean) {
  return useQuery({
    queryKey: ["lexicon", kind],
    queryFn: async () => {
      const { data, error: failure } = await api.GET(
        "/admin/lexicon",
        kind ? { params: { query: { kind } } } : {},
      );
      if (failure) throw failure;
      return data;
    },
    enabled,
  });
}
