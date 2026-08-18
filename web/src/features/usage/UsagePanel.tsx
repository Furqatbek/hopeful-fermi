/**
 * Where a shared asset is used, shown before it is edited.
 *
 * The contract states the purpose of these two endpoints in one sentence:
 * "shown before an author edits a shared asset, so 'I changed one passage and
 * broke four published mocks' cannot happen by surprise". Passages, questions
 * and groups are REFERENCED by tests, never copied into them, which is what
 * makes a bank a bank — and it is also what makes one edit reach material the
 * author is not looking at.
 *
 * **What an edit can and cannot reach, checked against the code rather than
 * assumed.** Publishing a TEST materializes a snapshot: `content_repo.publish`
 * writes the whole document — passage text, paragraph letters, question payloads
 * — onto `test_versions.snapshot`, and `exam.session` serves exactly that row to
 * a student. So a published paper is frozen, and editing this asset afterwards
 * cannot change what somebody sits. A draft version holds no such copy: it is
 * built from live content at the moment it is published, so an edit today is in
 * the paper that goes out next week. That asymmetry is the whole message of this
 * panel, and it is why draft references are not the harmless half of the list.
 *
 * Rendered inline and unfolded rather than behind a disclosure. A warning one
 * click away is a warning read after the edit.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api, problemText } from "../../api/client";
import { byRisk, tally } from "./usage";

/** Which endpoint answers for this asset. A question is addressed by its own
 *  xid; a passage by the xid of the VERSION being edited, because usage is
 *  per-version — a section points at one passage version, and a new version of
 *  the same passage starts used by nothing. */
export type UsageSubject = "passage-version" | "question";

export function UsagePanel({ subject, xid }: { subject: UsageSubject; xid: string }) {
  const usage = useQuery({
    queryKey: ["usage", subject, xid],
    queryFn: async () => {
      // Two calls rather than one templated path: the generated client types the
      // path literal, and a variable there loses every parameter check.
      const { data, error: failure } = subject === "passage-version"
        ? await api.GET("/passage-versions/{xid}/usage", { params: { path: { xid } } })
        : await api.GET("/questions/{xid}/usage", { params: { path: { xid } } });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(xid),
  });

  if (usage.isError) {
    return <p className="error">{problemText(usage.error)}</p>;
  }
  if (!usage.data) {
    // Deliberately not "nothing uses this". An unanswered question renders the
    // same as a safe answer, and this panel exists to be believed.
    return <p className="muted">Checking where this is used…</p>;
  }

  const counted = tally(usage.data);
  const rows = byRisk(usage.data.references);

  if (counted.total === 0) {
    return (
      <p className="muted">
        No test uses this yet, so an edit here changes nothing else.
      </p>
    );
  }

  return (
    <div className="issued">
      <h3>Where this is used</h3>
      <p className="muted">
        {counted.published > 0 && (
          <>
            {counted.published} published test
            {counted.published === 1 ? "" : "s"}.{" "}
          </>
        )}
        {counted.draft > 0 && (
          <>
            {counted.draft} draft{counted.draft === 1 ? "" : "s"}.{" "}
          </>
        )}
        {counted.archived > 0 && (
          <>
            {counted.archived} archived.{" "}
          </>
        )}
        {/* A status this console has no word for is still counted, so the
            sentence and the table below can never disagree about how many. */}
        {counted.other > 0 && (
          <>
            {counted.other} in another state.{" "}
          </>
        )}
      </p>

      {/* Only when there are rows to draw. The counts above can come from the
          server's two numbers alone, and an empty table under "2 published
          tests" reads as a listing that failed rather than one that was never
          sent. */}
      {rows.length > 0 && (
      <div className="scroll">
        <table>
          <thead>
            <tr><th>Test version</th><th>Status</th><th /></tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.xid}>
                <td>{row.title ?? "Untitled"}</td>
                <td className="muted">{row.status}</td>
                <td>
                  {/* Linked only for a test version. The contract's `kind` also
                      allows `question_group_version`, which nothing in the API
                      currently returns — and a route built for one would be a link
                      to a page that does not exist the day it starts arriving. */}
                  {row.kind === "test_version" && row.xid
                    ? <Link to={`/versions/${row.xid}`}>Open</Link>
                    : <span className="muted">{row.kind}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      )}

      <p className="muted">
        {counted.published > 0 && (
          <>
            A published test was frozen when it was published: it serves a copy of
            this material taken at that moment, so an edit here does not change a
            paper anyone has already sat.{" "}
          </>
        )}
        {counted.draft > 0 && (
          <>
            A draft has not been frozen. It takes this material as it stands on the
            day it is published, so an edit now is in the paper that goes out.{" "}
          </>
        )}
        Check the answer keys of anything listed here after a change.
      </p>
    </div>
  );
}
