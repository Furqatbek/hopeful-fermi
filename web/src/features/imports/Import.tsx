/**
 * Bringing a centre's existing material in.
 *
 * This is how a prep centre arrives with forty papers already written, and it is
 * the difference between "we will retype our bank into your product" and "we
 * will try your product". The whole pipeline existed — three adapters, a
 * canonical document, a dry run, a diff, a commit — and no screen reached it.
 *
 * **Always a dry run first, and the confirm step is the product.** The upload
 * parses and reports; nothing touches the content library until Commit. So the
 * counts and findings below are not a progress indicator, they are the thing the
 * author is agreeing to: "31 questions, 31 keys, 2 warnings" is a claim they can
 * check against the paper in their hand.
 *
 * **The attestation is a required field on this upload, not a setting somebody
 * accepted at signup.** A bulk import is the likeliest single route to a
 * published Cambridge paper landing in this system, so the affirmation is
 * captured per file with the statement version, the uploader and their IP —
 * nothing here is pre-selected, and the statement is shown rather than linked.
 *
 * **A commit lands as a DRAFT.** Import is not a publish bypass: a teacher who
 * cannot publish cannot import their way around it, and the imported version
 * goes through the same gate and the same review as one typed in by hand.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";

import { API_PREFIX, api, problemText } from "../../api/client";
import { getAccessToken } from "../../api/session";

const STATEMENT_VERSION = "1";

type Claim = "original" | "licensed" | "public_domain" | "permitted_excerpt";

const CLAIMS: { value: Claim; label: string }[] = [
  { value: "original", label: "We wrote this material ourselves" },
  { value: "licensed", label: "We hold a licence covering this use" },
  { value: "public_domain", label: "It is in the public domain" },
  { value: "permitted_excerpt", label: "It is a permitted excerpt" },
];

/** Download a template through the API so it carries the bearer token. A plain
 *  link cannot: these endpoints are authenticated, and an anchor would fetch a
 *  401 body and save it as a file called `template.csv`. */
async function downloadTemplate(format: "csv" | "json"): Promise<void> {
  const response = await fetch(`${API_PREFIX}/imports/template?format=${format}`, {
    headers: { Authorization: `Bearer ${getAccessToken() ?? ""}` },
  });
  if (!response.ok) throw new Error("Could not fetch the template.");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `ielts-hub-template.${format}`;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function Import() {
  const queries = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [format, setFormat] = useState<"csv" | "json">("csv");
  const [claim, setClaim] = useState<Claim | "">("");
  const [licenceNote, setLicenceNote] = useState("");
  const [targetTest, setTargetTest] = useState("");
  const [job, setJob] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The limit is part of the key: five other screens also read ["tests"]
  // under a different limit, and a bare shared key serves whichever one
  // last populated the cache to all of them (see tests/query-keys.test.ts).
  const tests = useQuery({
    queryKey: ["tests", 100],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/tests", {
        params: { query: { limit: 100 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const report = useQuery({
    queryKey: ["import", job],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/imports/{xid}", {
        params: { path: { xid: job! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(job),
  });

  const upload = useMutation({
    mutationFn: async () => {
      const file = fileInput.current?.files?.[0];
      if (!file) throw new Error("Choose a file to import.");
      if (!claim) throw new Error("Say where this material came from.");

      // Multipart, because the endpoint takes a file. `attestation` travels as a
      // JSON-encoded STRING: multipart fields carry scalars, so unlike the JSON
      // upload paths the object cannot go as an object.
      const body = new FormData();
      body.append("file", file);
      body.append("format", format);
      body.append("attestation", JSON.stringify({
        claim,
        statement_version: STATEMENT_VERSION,
        ...(claim === "licensed" && licenceNote.trim()
          ? { licence_note: licenceNote.trim() }
          : {}),
      }));
      if (targetTest) body.append("target_test_xid", targetTest);

      // Not through the typed client: `openapi-fetch` serialises JSON bodies, and
      // a FormData body must be handed to fetch untouched so the browser sets the
      // multipart boundary.
      const response = await fetch(`${API_PREFIX}/imports`, {
        method: "POST",
        headers: { Authorization: `Bearer ${getAccessToken() ?? ""}` },
        body,
      });
      const payload = await response.json();
      if (!response.ok) throw payload;
      return payload as { xid: string };
    },
    onSuccess: (data) => {
      setError(null);
      setJob(data.xid);
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const commit = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST("/imports/{xid}/commit", {
        params: { path: { xid: job! } },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: () => {
      setError(null);
      void queries.invalidateQueries({ queryKey: ["import", job] });
      void queries.invalidateQueries({ queryKey: ["tests"] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const counts = report.data?.report?.counts;
  const findings = report.data?.report?.findings ?? [];
  const errors = findings.filter((f) => f.severity === "error");
  const unresolved = report.data?.report?.unresolved_media ?? [];
  const diff = report.data?.report?.diff;
  const committed = report.data?.committed_test_version_xid;

  return (
    <div className="page">
      <h1>Import</h1>

      <h2>1 · Start from the template</h2>
      <p className="muted">
        The supported path is this template, and being blunt about that is
        cheaper than an import that fails unpredictably: parsing a Word file a
        teacher already has is an open-ended problem, and parsing this is a
        bounded one. Fill it in, upload it, check the report, then commit.
      </p>
      <div className="row">
        <button type="button" onClick={() => void downloadTemplate("csv")}>
          Download CSV template
        </button>
        <button type="button" className="link"
                onClick={() => void downloadTemplate("json")}>
          JSON template
        </button>
      </div>

      <h2>2 · Upload it</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          setJob(null);
          upload.mutate();
        }}
      >
        <label htmlFor="i-format">Format</label>
        <select
          id="i-format"
          value={format}
          onChange={(event) => setFormat(event.target.value as "csv" | "json")}
        >
          <option value="csv">CSV</option>
          <option value="json">JSON</option>
        </select>
        <p className="muted">
          {/* DOCX is accepted by the API but only for a locked template carrying
              the canonical document in a file property, and nothing in the
              product produces that template yet. Offering it here would be a
              format that parses nothing. */}
          Word documents are not supported yet — the DOCX path reads a locked
          template this product does not generate.
        </p>

        <label htmlFor="i-file">File</label>
        <input id="i-file" type="file" ref={fileInput}
               accept={format === "csv" ? ".csv,text/csv" : ".json,application/json"} />

        <label htmlFor="i-target">Import as a new version of</label>
        <select
          id="i-target"
          value={targetTest}
          onChange={(event) => setTargetTest(event.target.value)}
        >
          <option value="">— a brand new test —</option>
          {tests.data?.items?.map((test) => (
            <option key={test.xid} value={test.xid}>{test.title}</option>
          ))}
        </select>
        <p className="muted">
          The round trip: export a test, edit it offline, bring it back as a new
          version of the same test rather than as an unrelated copy.
        </p>

        <fieldset>
          <legend>Where did this material come from?</legend>
          {/* Required, never pre-selected, and the statement is shown in full.
              A bulk import is the likeliest single route to a published exam
              paper entering this system, and this control is the evidence a
              rights holder is answered with. */}
          {CLAIMS.map((option) => (
            <label key={option.value} className="choice">
              <input
                type="radio"
                name="import-claim"
                value={option.value}
                checked={claim === option.value}
                onChange={() => setClaim(option.value)}
                required
              />
              {option.label}
            </label>
          ))}
          <p className="muted">
            By uploading you confirm this is true and that this centre holds the
            right to use this material. The confirmation is recorded against your
            name, the time, and this file — and it is recorded even if the file
            fails to parse, because handing it over is when the claim was made.
          </p>
        </fieldset>

        {claim === "licensed" && (
          <>
            <label htmlFor="i-licence">Licence reference</label>
            <input
              id="i-licence"
              value={licenceNote}
              onChange={(event) => setLicenceNote(event.target.value)}
              placeholder="Publisher, agreement number, expiry"
            />
          </>
        )}

        <button disabled={upload.isPending || !claim}>
          {upload.isPending ? "Checking…" : "Upload and check"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}

      {report.data && (
        <div className="issued">
          <h2>3 · What this would create</h2>
          <p className="muted">
            Nothing has been written yet. {report.data.status === "failed"
              ? "This file could not be used as it stands."
              : "Check these against the paper in front of you."}
          </p>

          {counts && (
            <div className="scroll">
              <table>
                <tbody>
                  <tr><td>Sections</td><td>{counts.sections ?? 0}</td></tr>
                  <tr><td>Question groups</td><td>{counts.groups ?? 0}</td></tr>
                  <tr><td>Questions</td><td>{counts.questions ?? 0}</td></tr>
                  <tr>
                    <td>Answer keys</td>
                    <td>
                      {counts.keys ?? 0}
                      {/* The number worth checking. A paper that imports 31
                          questions and 24 keys is seven questions that cannot be
                          marked, and the publish gate will say so later — better
                          here, next to the count it disagrees with. */}
                      {(counts.keys ?? 0) < (counts.questions ?? 0) && (
                        <span className="error">
                          {" "}— {(counts.questions ?? 0) - (counts.keys ?? 0)} question
                          {(counts.questions ?? 0) - (counts.keys ?? 0) === 1 ? "" : "s"}{" "}
                          would arrive with no answer key
                        </span>
                      )}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          )}

          {findings.length > 0 && (
            <>
              <h3>Findings</h3>
              <ul className="report">
                {findings.map((finding, index) => (
                  <li key={index} className={finding.severity === "error" ? "error" : "muted"}>
                    <code>{finding.code}</code> {finding.message}
                    {finding.path && <span className="muted"> · {finding.path}</span>}
                    {finding.fix_hint && <div className="muted">{finding.fix_hint}</div>}
                  </li>
                ))}
              </ul>
            </>
          )}

          {unresolved.length > 0 && (
            <p className="error">
              Could not find {unresolved.length} referenced file
              {unresolved.length === 1 ? "" : "s"}: {unresolved.join(", ")}. Upload
              the audio first, then import — a section whose recording is missing
              cannot be sat.
            </p>
          )}

          {diff && (diff.added?.length || diff.changed?.length || diff.removed?.length) ? (
            <>
              <h3>Against the current version</h3>
              <ul className="report">
                {diff.added?.map((line) => <li key={`a${line}`}>+ {line}</li>)}
                {diff.changed?.map((line) => <li key={`c${line}`}>~ {line}</li>)}
                {diff.removed?.map((line) => <li key={`r${line}`}>− {line}</li>)}
              </ul>
            </>
          ) : null}

          {committed ? (
            <p>
              Committed as a <strong>draft</strong> version.{" "}
              <Link to={`/versions/${committed}`}>Open it</Link> to check it and
              publish. Import lands as a draft deliberately — it must not become a
              way around the publish gate.
            </p>
          ) : report.data.status === "validated" ? (
            <button onClick={() => commit.mutate()} disabled={commit.isPending}>
              {commit.isPending ? "Committing…" : "Commit this import"}
            </button>
          ) : (
            <p className="muted">
              {errors.length > 0
                ? "Fix the errors above and upload it again."
                : report.data.parse_error ?? "This import cannot be committed."}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
