/**
 * Which questions on a paper are broken, and which are merely hard.
 *
 * This is the screen that makes a centre's second term better than its first. A
 * mock produces bands; an item analysis produces a corrected paper.
 *
 * **The finding is discrimination, not difficulty.** A hard question is a
 * legitimate question. A question the strong students get wrong more often than
 * the weak ones is almost never hard — it is a key that does not accept the
 * right answer, and every student who wrote that answer lost a mark they had
 * earned. So difficulty is a number in a cell and discrimination is the one
 * chart on the screen, drawn with a sign in it: the bar for a bad key points the
 * other way and is visible from across the room. The word "discrimination" does
 * not appear in the interface — "who got it right", with weaker students at one
 * end and stronger at the other, says the same thing to a reader who has never
 * met a point-biserial.
 *
 * **The order is by concern, never by question number.** Forty rows in paper
 * order buries the one row that matters. `byConcern` puts it first and the two
 * or three items worth acting on are lifted out of the table entirely.
 *
 * **`common_wrong` is how a broken key is found without anybody suspecting it.**
 * Ten students writing *bicycle* against a key that only accepts *bike* is not
 * ten careless students. Spelling and number variants never reach this list
 * because the tolerance lexicon absorbs them first, so what surfaces is a
 * genuine alternative — which is why each line here is written to answer one
 * question and not to be studied.
 *
 * Computed live from current score runs rather than from the ninety-day
 * projection, so a mock sat this morning appears this morning. The cross-test
 * dashboard on the Flagged items screen is the projection, and lags.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api, problemText } from "../../api/client";
import {
  type Assessment,
  type Item,
  MIN_RESPONSES,
  assess,
  byConcern,
  discriminationBar,
  meanTime,
  percentCorrect,
  preferredVersion,
  summarise,
  wrongAnswers,
} from "./items";
import "./items.css";

const LABEL: Record<Assessment["verdict"], string> = {
  broken: "Broken",
  "check-key": "Check the key",
  fine: "Probably fine",
  "no-data": "Not sat",
};

export function ItemAnalysis() {
  const [testXid, setTestXid] = useState("");
  const [picked, setPicked] = useState("");
  const [scope, setScope] = useState<"mine" | "global">("mine");

  // The limit is part of the key: five other screens also read ["tests"]
  // under a different limit, and a bare shared key serves whichever one
  // last populated the cache to all of them (see tests/query-keys.test.ts).
  const tests = useQuery({
    queryKey: ["tests", 50],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/tests", {
        params: { query: { limit: 50 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const versions = useQuery({
    queryKey: ["test-versions", testXid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/tests/{xid}/versions", {
        params: { path: { xid: testXid } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(testXid),
  });

  // Derived rather than held in an effect: when the test changes, the previously
  // picked version is no longer in the list and the fallback takes over on the
  // same render. An effect would leave one render analysing the old paper's
  // version against the new paper's title.
  const versionList = versions.data ?? [];
  const versionXid =
    picked && versionList.some((v) => v.xid === picked)
      ? picked
      : preferredVersion(versionList)?.xid ?? "";

  const analysis = useQuery({
    queryKey: ["item-analysis", versionXid, scope],
    queryFn: async () => {
      const { data, error: failure } = await api.GET(
        "/test-versions/{xid}/item-analysis",
        { params: { path: { xid: versionXid }, query: { org_scope: scope } } },
      );
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(versionXid),
  });

  const items = analysis.data?.items ?? [];
  const ordered = byConcern(items);
  const tally = summarise(items);
  // Lifted out of the table because three findings among forty rows are three
  // rows, and the reason this screen exists is that nobody scrolls forty rows
  // looking for a minus sign.
  const findings = ordered.filter((item) => {
    const { verdict } = assess(item);
    return verdict === "broken" || verdict === "check-key";
  });

  return (
    <div className="page items">
      <h1>Item analysis</h1>
      <p className="muted">
        How each question on a paper behaved when students sat it. Read the bars
        first: a question the stronger students got wrong is usually a wrong
        answer key, not a hard question.
      </p>

      <label htmlFor="ia-test">Test</label>
      <select
        id="ia-test"
        value={testXid}
        onChange={(event) => {
          setTestXid(event.target.value);
          setPicked("");
        }}
      >
        <option value="">— choose a test —</option>
        {tests.data?.items?.map((test) => (
          <option key={test.xid} value={test.xid}>{test.title}</option>
        ))}
      </select>
      {tests.isError && <p className="error">{problemText(tests.error)}</p>}
      {tests.data?.items?.length === 0 && (
        <p className="muted">There are no tests yet.</p>
      )}

      {testXid && (
        <>
          <label htmlFor="ia-version">Version</label>
          <select
            id="ia-version"
            value={versionXid}
            onChange={(event) => setPicked(event.target.value)}
          >
            {versionList.map((version) => (
              <option key={version.xid} value={version.xid}>
                v{version.version_no} · {version.status}
              </option>
            ))}
          </select>
          <p className="muted">
            {/* A draft has never been sat, so it analyses to an empty screen —
                which reads as a paper with nothing wrong with it rather than as
                a paper nobody has taken. */}
            Opens on the published version. A draft has never been sat, so it has
            nothing to show.
          </p>
          {versions.data && versionList.length === 0 && (
            <p className="muted">This test has no versions yet.</p>
          )}
          {versions.isError && <p className="error">{problemText(versions.error)}</p>}
        </>
      )}

      {versionXid && (
        <div className="row">
          <label className="choice">
            <input
              type="radio"
              name="ia-scope"
              checked={scope === "mine"}
              onChange={() => setScope("mine")}
            />
            My centre
          </label>
          <label className="choice">
            <input
              type="radio"
              name="ia-scope"
              checked={scope === "global"}
              onChange={() => setScope("global")}
            />
            Every centre
          </label>
        </div>
      )}
      {versionXid && (
        <p className="muted">
          My centre counts only sittings your centre set — an assignment or a
          contest. Every centre adds practice students did on their own account,
          which is the platform average and a different question.
        </p>
      )}

      {analysis.isError && <p className="error">{problemText(analysis.error)}</p>}
      {analysis.isPending && versionXid && <p className="muted">Working it out…</p>}

      {analysis.data && items.length === 0 && (
        <p className="muted">
          Nobody has sat this version yet, so there is nothing to analyse.
          {scope === "mine" && (
            <> Practice a student did on their own account is not counted here.
            Switch to <strong>Every centre</strong> to include it.</>
          )}
        </p>
      )}

      {analysis.data && items.length > 0 && (
        <>
          <p>
            <strong>{analysis.data.n_attempts ?? 0}</strong> sat this version ·{" "}
            {tally.broken} broken · {tally.checkKey} to check ·{" "}
            {tally.fine} probably fine
          </p>

          <h2>Worth acting on</h2>
          {findings.length === 0 ? (
            <p className="muted">Nothing on this paper looks broken.</p>
          ) : (
            <>
              {findings.map((item) => (
                <Finding key={`${item.question_xid}-${item.number}`} item={item} />
              ))}
              <p className="muted">
                There are two ways to fix a missing answer and they are not the
                same. A <strong>different word</strong> — <em>bike</em> where the
                key says <em>bicycle</em> — is a key correction:{" "}
                <Link to="/regrades">correct the answer key</Link>,
                which also offers to rescore everyone who has already sat it. A{" "}
                <strong>different form of the same word</strong> — a spelling, a
                plural, a number written out — belongs in the tolerance list:{" "}
                <Link to="/lexicon">add a tolerance entry</Link>,
                which fixes it on every question at once. Forms already in that
                list never reach this screen, so one appearing here is one the
                list does not have.
              </p>
            </>
          )}

          <h2>The whole paper</h2>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th />
                  <th>
                    Who got it right
                    <span className="disc-axis">
                      <span>weaker</span>
                      <span>stronger</span>
                    </span>
                  </th>
                  <th>Correct</th>
                  <th>Sat</th>
                  <th>Most common wrong answer</th>
                </tr>
              </thead>
              <tbody>
                {ordered.map((item) => {
                  const assessment = assess(item);
                  const [top] = wrongAnswers(item);
                  return (
                    <tr
                      key={`${item.question_xid}-${item.number}`}
                      className={assessment.verdict}
                    >
                      <td className="num">{item.number ?? "—"}</td>
                      <td>
                        <span className={`verdict ${assessment.verdict}`}>
                          {LABEL[assessment.verdict]}
                        </span>
                      </td>
                      <td><Discrimination value={item.discrimination} /></td>
                      <td className="num">{percentCorrect(item.p_value)}</td>
                      <td className="num">
                        {assessment.n}
                        {assessment.thin && assessment.n > 0 && (
                          <span
                            className="muted"
                            title={`Below ${MIN_RESPONSES} responses the server raises no flag of its own`}
                          >
                            {" "}· thin
                          </span>
                        )}
                      </td>
                      <td>
                        {top ? (
                          <>
                            <span className="wrong-value">{top.value}</span>{" "}
                            <span className="wrong-count">
                              {Math.round(top.share * 100)}%
                            </span>
                          </>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="muted">
            Ordered by what needs attention, not by question number. Sat counts
            students, not blanks — a three-blank question one student answered is
            one response, right only if every blank was right.
          </p>
        </>
      )}
    </div>
  );
}

/** One item that needs a person, with the reason and the numbers behind it. */
function Finding({ item }: { item: Item }) {
  const assessment = assess(item);
  const wrongs = wrongAnswers(item);
  const time = meanTime(item.mean_time_ms);

  return (
    <section className={`finding ${assessment.verdict}`}>
      <h3>
        Question {item.number ?? "?"}
        <span className={`verdict ${assessment.verdict}`}>
          {LABEL[assessment.verdict]}
        </span>
        <span className="muted">{item.type_key}</span>
      </h3>

      {assessment.reasons.includes("negative_discrimination") && (
        <p>
          The students who scored highest on this paper got this question wrong
          more often than the students who scored lowest. That is what a wrong
          answer key looks like from the outside: the people who knew the
          material wrote an answer the key does not accept.
        </p>
      )}
      {assessment.reasons.includes("near_zero_p") && (
        <p>
          Only {percentCorrect(item.p_value)} answered it correctly. Either the
          key does not accept the right answer, or the question cannot be
          answered as it is written.
        </p>
      )}
      {assessment.reasons.includes("common_wrong_answer")
        && !assessment.reasons.includes("negative_discrimination") && (
        <p>
          One wrong answer is far more common than the rest. If it is an answer,
          the key is missing it.
        </p>
      )}

      <div className="row">
        <Discrimination value={item.discrimination} />
        <span className="muted">
          {percentCorrect(item.p_value)} correct · {assessment.n} sat
          {time && <> · {time} each</>}
        </span>
      </div>

      {wrongs.length > 0 && (
        <>
          <p className="muted">What students wrote and had marked wrong</p>
          <WrongAnswers item={item} />
        </>
      )}

      {assessment.thin && (
        <p className="muted">
          {/* Said out loud because the row carries `flagged: false` and an author
              who checks would otherwise find the server disagreeing with this
              screen. It is the sample size, not a difference of opinion. */}
          Only {assessment.n} students have sat it. Below {MIN_RESPONSES} the
          server does not raise its own flag, because the numbers move too much.
          Treat this as a lead.
        </p>
      )}
    </section>
  );
}

/**
 * What students typed, each line answering one question: should the key accept
 * this.
 *
 * The count alone cannot answer it — ten of twenty-five is a missing alternative
 * and ten of four hundred is not — so the share is written beside it and the
 * call is written out in words rather than encoded in a colour. Shared with the
 * flagged-items screen so the same string never gets two different readings.
 */
export function WrongAnswers({ item }: { item: Item }) {
  const n = item.n_responses ?? 0;
  return (
    <ul className="wrongs">
      {wrongAnswers(item).map((wrong) => (
        <li key={wrong.value}>
          <span className="wrong-value">{wrong.value}</span>
          <span className="wrong-count">
            {wrong.count} of {n} · {Math.round(wrong.share * 100)}%
          </span>
          <span
            className={`wrong-call ${wrong.likelyMissingAnswer ? "missing" : "plain"}`}
          >
            {wrong.likelyMissingAnswer
              ? "probably an answer the key should accept"
              : "students were simply wrong"}
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * The signed bar. Left of the line means the weaker students got it right.
 *
 * Rendered as two halves of one track so the zero line stays in the same place
 * on every row: a chart whose axis moves cannot be compared down a column.
 */
export function Discrimination({ value }: { value: number | null | undefined }) {
  const bar = discriminationBar(value);
  if (!bar) {
    // Not a zero-length bar. "Cannot be computed" — everyone right, everyone
    // wrong, or no spread in total scores — is a different statement from "no
    // relationship", and an author acts on them differently.
    return <span className="disc-unknown">not enough spread</span>;
  }
  return (
    <span
      className="disc"
      title={typeof value === "number" ? `Point-biserial ${value.toFixed(2)}` : ""}
    >
      <span className="disc-half low">
        {bar.side === "negative" && (
          <span className="disc-fill low" style={{ width: `${bar.percent}%` }} />
        )}
      </span>
      <span className="disc-zero" />
      <span className="disc-half">
        {bar.side === "positive" && (
          <span className="disc-fill" style={{ width: `${bar.percent}%` }} />
        )}
      </span>
    </span>
  );
}
