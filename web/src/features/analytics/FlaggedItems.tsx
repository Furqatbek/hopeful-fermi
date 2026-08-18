/**
 * Every question in the centre's bank that is probably broken, across all papers.
 *
 * Item analysis answers "what went wrong with this mock". This answers the
 * question nobody thinks to ask: which questions have been quietly costing
 * students marks for weeks, on papers nobody has looked at since.
 *
 * **This is the ninety-day projection, not live.** A background sweep recomputes
 * it over the last ninety days of sittings, so a mock sat this morning is on the
 * Item analysis screen now and here after the next rebuild. Said on the screen
 * rather than left for a teacher to discover, because "I fixed that yesterday
 * and it is still listed" is how a report loses its reader.
 *
 * **The server's order buries the finding.** It sorts by ascending p-value, so
 * an item nobody could answer outranks an item the strong students got wrong —
 * and the second is the one that is almost always a bad key. `byConcern` puts
 * that back. The list is also capped at fifty rows server-side, which means the
 * cap is applied in the server's order, before ours.
 *
 * The action column is the server's own `suggested_action` rather than ours. It
 * distinguishes four cases from the flag reasons, and a screen that recomputed
 * it would be a second opinion nobody asked for.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api, problemText } from "../../api/client";
import { Discrimination, WrongAnswers } from "./ItemAnalysis";
import {
  type FlaggedItem,
  assess,
  byConcern,
  dedupeByQuestion,
  percentCorrect,
  wrongAnswers,
} from "./items";
import "./items.css";

/** The server's four cases, said the way a teacher would say them. */
const ACTION: Record<string, string> = {
  review_key: "Check the answer key.",
  review_item: "Read the question again.",
  retire_too_easy: "Take it off the paper.",
  none: "—",
};

const VERDICT: Record<string, string> = {
  broken: "Broken",
  "check-key": "Check the key",
  fine: "Probably fine",
  "no-data": "Not sat",
};

/** The server's own `LIMIT`. Reaching it means the list is truncated. */
const SERVER_LIMIT = 50;

export function FlaggedItems() {
  const flagged = useQuery({
    queryKey: ["flagged-items"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/content/flagged-items");
      if (failure) throw failure;
      return data;
    },
  });

  // A bare array, not the `{items, next_cursor}` envelope most listings use, and
  // it takes no query parameters at all — no filter, no page, no scope.
  const sent = flagged.data ?? [];
  // Before ordering: the endpoint sends a question once per statistics scope, so
  // an untouched list shows the same broken question twice.
  const rows = dedupeByQuestion(sent);
  const ordered = byConcern(rows);

  return (
    <div className="page items">
      <h1>Flagged items</h1>
      <p className="muted">
        Questions that look broken, across every paper. Worst first: a question
        the stronger students got wrong is usually a wrong answer key, not a hard
        question.
      </p>

      {flagged.isPending && <p className="muted">Loading…</p>}
      {flagged.isError && <p className="error">{problemText(flagged.error)}</p>}

      {flagged.data && rows.length === 0 && (
        <p className="muted">
          Nothing is flagged. This list is rebuilt in the background over the
          last ninety days of sittings, so a mock sat today appears on{" "}
          <strong>Item analysis</strong> straight away and here after the next
          rebuild.
        </p>
      )}

      {rows.length > 0 && (
        <>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>Question</th>
                  <th>
                    Who got it right
                    <span className="disc-axis">
                      <span>weaker</span>
                      <span>stronger</span>
                    </span>
                  </th>
                  <th>Correct</th>
                  <th>Sat</th>
                  <th>What students wrote</th>
                  <th>What to do</th>
                </tr>
              </thead>
              <tbody>
                {ordered.map((row) => (
                  <Row key={`${row.question_xid}-${row.number}`} row={row} />
                ))}
              </tbody>
            </table>
          </div>

          <p className="muted">
            {sent.length >= SERVER_LIMIT && (
              <>
                The server sends at most {SERVER_LIMIT} rows and picks which by
                difficulty, before this page reorders them — so a bad key on an
                otherwise ordinary question can fall outside the cut.{" "}
              </>
            )}
            {sent.length > rows.length && (
              <>
                {sent.length} rows arrived for {rows.length} questions: a question
                is counted once for your centre and once across the platform, and
                the wider count is the one shown.{" "}
              </>
            )}
            Questions from the shared platform bank appear here alongside your
            centre's own.
          </p>

          <p className="muted">
            Two ways to fix a missing answer. A <strong>different word</strong> —{" "}
            <em>bike</em> where the key says <em>bicycle</em> — is a key
            correction:{" "}
            <Link to="/regrades">correct the answer key</Link>,
            which also offers to rescore everyone who has already sat it. A{" "}
            <strong>different form of the same word</strong> — a spelling, a
            plural, a number written out — belongs in the tolerance list:{" "}
            <Link to="/lexicon">add a tolerance entry</Link>,
            which fixes it on every question at once.
          </p>

          <p className="muted">
            Rebuilt in the background over the last ninety days of sittings. For
            a mock sat today, use <strong>Item analysis</strong>, which is
            computed live.
          </p>
        </>
      )}
    </div>
  );
}

function Row({ row }: { row: FlaggedItem }) {
  const assessment = assess(row);
  const wrongs = wrongAnswers(row);
  const action = ACTION[row.suggested_action ?? "none"] ?? "—";

  return (
    <tr className={assessment.verdict}>
      <td>
        {/* Where to go and what it is called when you get there. An item that
            has never been placed on a paper still belongs on this list — it is
            being answered in practice — and its number would be meaningless. */}
        {row.test_title
          ? <>{row.test_title} · question {row.number ?? "?"}</>
          : <span className="muted">Not on a paper</span>}
        <br />
        <span className="muted">
          {row.type_key} · {VERDICT[assessment.verdict] ?? assessment.verdict}
        </span>
      </td>
      <td><Discrimination value={row.discrimination} /></td>
      <td className="num">{percentCorrect(row.p_value)}</td>
      {/* No mean-time column. The projection that fills this table builds its
          responses without timings, so `mean_time_ms` is null on every row here
          — a column of dashes. Item analysis computes it live and shows it. */}
      <td className="num">{assessment.n}</td>
      <td>
        {wrongs.length > 0
          ? <WrongAnswers item={row} />
          : <span className="muted">—</span>}
      </td>
      <td>{action}</td>
    </tr>
  );
}
