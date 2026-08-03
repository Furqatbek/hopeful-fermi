/**
 * Which items are burning.
 *
 * "Items burn once they circulate, and this is where you watch it happen." So
 * this is a watch list over the question bank, not a lookup form: an author does
 * not open it holding the xid of the item they are worried about — the whole
 * point is that they do not yet know which one that is.
 *
 * **There is no listing endpoint for exposure.** `GET /questions` declares
 * `burn_score` on every row of the bank and the handler hardcodes it to `null`,
 * and `GET /content/flagged-items` is item difficulty, not circulation. The only
 * real number comes from `GET /questions/{xid}/exposure`, one request per item,
 * so this page fetches the bank and then fans out across the page it fetched.
 * That is why the page size is a control the author sets rather than a number
 * hidden in the code — it is the request count, and it is theirs to spend.
 *
 * **`recommendation` comes from the server.** `fresh`/`watch`/`retire` is
 * `stats.exposure_recommendation`, shared with the competition freshness gate on
 * purpose: an author told `watch` here and a contest refused for `retire` there
 * have to be reading one rule. Re-deriving the thresholds in the console would
 * make three.
 *
 * Requires VIEW_EXPOSURE — teacher and above. A student must never see which
 * items have been sat; that is a map of what to revise.
 */

import { useQueries, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, problemText } from "../../api/client";
import { burnPercent, rankByBurn } from "./exposure";
import "./governance.css";

/** How many items to check at once. Each one is a request, so this is a cost the
 *  author is choosing, and 25 is the bank listing's own default. */
const PAGE_SIZES = [10, 25, 50] as const;

export function Exposure() {
  const [skill, setSkill] = useState<"" | "reading" | "listening">("");
  const [pageSize, setPageSize] = useState<number>(25);

  const bank = useQuery({
    queryKey: ["questions", "exposure-page", skill, pageSize],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/questions", {
        params: {
          // No `q` filter offered. The parameter is declared and
          // `list_questions` never applies it, so a search box here would look
          // like it had narrowed the list and would have returned everything.
          query: { limit: pageSize, ...(skill ? { skill } : {}) },
        },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const items = bank.data?.items ?? [];

  const reports = useQueries({
    queries: items.map((question) => ({
      queryKey: ["exposure", question.xid],
      queryFn: async () => {
        const { data, error: failure } = await api.GET("/questions/{xid}/exposure", {
          params: { path: { xid: question.xid } },
        });
        if (failure) throw failure;
        return data;
      },
      // Exposure is a projection refreshed by a worker, not a live counter.
      // Refetching it on every window focus would be one request per item for a
      // number that cannot have moved.
      staleTime: 5 * 60 * 1000,
      refetchOnWindowFocus: false,
    })),
  });

  const rows = rankByBurn(items.map((question, index) => {
    const report = reports[index];
    return {
      xid: question.xid,
      typeKey: question.type_key,
      skill: question.skill,
      versionNo: question.current_version?.version_no ?? null,
      exposure: report?.data ?? null,
      pending: report?.isPending ?? true,
      failure: report?.isError ? problemText(report.error) : null,
    };
  }));

  const burning = rows.filter((row) => row.exposure?.recommendation === "retire");
  const unread = rows.filter((row) => !row.exposure && !row.pending);

  return (
    <div className="page">
      <h1>Exposure</h1>
      <p className="muted">
        How far each question has travelled: how many sittings, how many students,
        and how many centres. An item that has left the building cannot be made
        fresh again.
      </p>

      <div className="row">
        <span>
          <label htmlFor="e-skill">Skill</label>
          <select
            id="e-skill"
            value={skill}
            onChange={(event) =>
              setSkill(event.target.value as "" | "reading" | "listening")}
          >
            <option value="">All</option>
            <option value="reading">Reading</option>
            <option value="listening">Listening</option>
          </select>
        </span>
        <span>
          <label htmlFor="e-size">Items to check</label>
          <select
            id="e-size"
            value={pageSize}
            onChange={(event) => setPageSize(Number(event.target.value))}
          >
            {PAGE_SIZES.map((size) => (
              <option key={size} value={size}>{size}</option>
            ))}
          </select>
        </span>
      </div>

      {bank.isError && <p className="error">{problemText(bank.error)}</p>}

      {burning.length > 0 && (
        <p className="error">
          {burning.length === 1
            ? "One item is spent."
            : `${burning.length} items are spent.`}{" "}
          A contest built on a paper containing them is refused, and a mock set
          from them measures who has met the paper before rather than who can do
          it.
        </p>
      )}

      <table>
        <thead>
          <tr>
            <th>Question</th>
            <th className="burn-cell">Burn</th>
            <th>Sittings</th>
            <th>Students</th>
            <th>Centres</th>
            <th>Last sat</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const recommendation = row.exposure?.recommendation ?? "";
            return (
              <tr key={row.xid}>
                <td>
                  {row.typeKey}
                  <span className="muted">
                    {" "}· {row.skill}
                    {row.versionNo !== null && <> · v{row.versionNo}</>}
                  </span>
                </td>
                <td className="burn-cell">
                  {row.exposure ? (
                    <>
                      <span
                        className={`burn ${recommendation}`}
                        style={{ width: `${burnPercent(row.exposure.burn_score)}%` }}
                      />
                      <span className="muted">
                        {recommendation} · {burnPercent(row.exposure.burn_score)}%
                      </span>
                    </>
                  ) : (
                    <span className="muted">
                      {row.pending ? "Checking…" : (row.failure ?? "Not read")}
                    </span>
                  )}
                </td>
                <td className="num">{row.exposure?.times_sat ?? "—"}</td>
                <td className="num">{row.exposure?.distinct_users ?? "—"}</td>
                <td className="num">{row.exposure?.distinct_orgs ?? "—"}</td>
                <td className="muted">
                  {row.exposure?.last_seen_at
                    ? new Date(row.exposure.last_seen_at).toLocaleDateString()
                    : "Never"}
                </td>
              </tr>
            );
          })}
          {items.length === 0 && !bank.isPending && (
            <tr>
              <td colSpan={6} className="muted">
                No questions in the bank yet. Write some under{" "}
                <Link className="link" to="/questions">Questions</Link>.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {unread.length > 0 && (
        <p className="muted">
          {/* Listed rather than folded into the table's silence. These rows sit
              at the bottom because an item whose exposure could not be read is
              not evidence that it is fresh, and this screen must not imply it. */}
          {unread.length} item{unread.length === 1 ? "" : "s"} could not be
          checked and {unread.length === 1 ? "is" : "are"} at the bottom of the
          list. Their circulation is unknown, not zero.
        </p>
      )}

      <p className="muted">
        Nothing here retires an item: the console has no endpoint that archives a
        question. What a spent item means in practice is do not put it in a new
        paper — and if you already have, compose a fresh version under{" "}
        <Link className="link" to="/tests">Tests</Link>.
      </p>
      <p className="muted">
        A sitting counts once per attempt, and previews are excluded — checking
        your own paper does not burn it.
      </p>
    </div>
  );
}
