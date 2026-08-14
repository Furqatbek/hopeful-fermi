/**
 * One test, its versions, and what can still be done to each.
 *
 * The version list is the spine of the authoring model: a published version is
 * frozen, and changing anything means starting a new draft from it. Making that
 * visible here — rather than offering an edit button that 409s — is the
 * difference between a versioning model a teacher understands and one that feels
 * like the software fighting them.
 *
 * The lifecycle moves are asymmetric, and the asymmetry is the model:
 *
 * **A test's own fields are editable whatever its versions have done.** The
 * title, the description and the tags are how a centre finds a paper in a list
 * of forty; a typo in one should not require a new version of the content.
 *
 * **A published version can only be ARCHIVED, never deleted or edited.**
 * Attempts reference it for ever, and a band a student holds has to keep
 * resolving to the paper that produced it. Archiving stops it being assignable
 * and leaves everything already sat intact.
 *
 * **A test can only be deleted if nothing was ever published.** The server
 * refuses otherwise, for the same reason, and this screen says so rather than
 * offering the button and letting it 409.
 *
 * **Cloning copies the composition, not the assets.** The new draft references
 * the same passage and question-group versions, so next term's mock costs a few
 * hundred bytes and editing it cannot touch the original's material.
 *
 * **Export is the other half of import.** A version leaves as the same document
 * `content.importer` reads, so a centre can take a paper out, work on it in a
 * spreadsheet, and bring it back as a new version rather than as an unrelated
 * copy. It is a file download of an authenticated endpoint, which a plain anchor
 * cannot do — see `downloadExport` below.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { API_PREFIX, api, problemText } from "../../api/client";
import { getAccessToken } from "../../api/session";

/** The formats the handler actually produces.
 *
 *  The contract's enum also offers `docx`, and the handler has no branch for it:
 *  `format=docx` falls through to the JSON branch and answers
 *  `application/json` with a `.json` filename. Offering Word here would hand a
 *  centre a file named `.docx` that Word cannot open — the same gap
 *  `/imports/template` states outright and this endpoint does not. */
type Format = "json" | "csv";

/**
 * Fetch the export and save it.
 *
 * Through `fetch` rather than the typed client because this is a FILE: the
 * response is `text/csv` or a JSON attachment, not a modelled body. A plain
 * `<a href>` cannot be used at all — the endpoint is authenticated, so the
 * browser would fetch a 401 problem document and save that as the paper.
 */
async function downloadExport(xid: string, versionNo: number, format: Format,
                              includeKeys: boolean): Promise<void> {
  const query = new URLSearchParams({ format, include_keys: String(includeKeys) });
  const response = await fetch(
    `${API_PREFIX}/test-versions/${xid}/export?${query.toString()}`,
    { headers: { Authorization: `Bearer ${getAccessToken() ?? ""}` } });
  if (!response.ok) {
    // The refusal is a problem document, and it says which authority is missing —
    // export, or the separate check on seeing the keys. Surfacing it beats a
    // generic failure, which reads as an outage.
    throw await response.json().catch(() => new Error("The export failed."));
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `test-v${versionNo}.${format}`;
  anchor.click();
  URL.revokeObjectURL(url);
}

export function TestDetail() {
  const { xid = "" } = useParams();
  const navigate = useNavigate();
  const queries = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<{ xid: string; versionNo: number } | null>(null);
  const [format, setFormat] = useState<Format>("json");
  const [includeKeys, setIncludeKeys] = useState(false);

  // `PATCH /tests/{xid}` requires `If-Match`. The tag belongs to the RESPONSE,
  // not to the test, so it lives beside the query rather than inside its data.
  const etag = useRef<string | null>(null);

  const test = useQuery({
    queryKey: ["test", xid],
    queryFn: async () => {
      const { data, error: failure, response } = await api.GET("/tests/{xid}", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
      etag.current = response.headers.get("ETag");
      return data;
    },
    enabled: Boolean(xid),
  });

  useEffect(() => {
    if (test.data && !editing) {
      setTitle(test.data.title ?? "");
      setDescription(test.data.description ?? "");
      setTags((test.data.tags ?? []).join(", "));
    }
  }, [test.data, editing]);

  const versions = useQuery({
    queryKey: ["test-versions", xid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/tests/{xid}/versions", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(xid),
  });

  const refresh = () => {
    void queries.invalidateQueries({ queryKey: ["test", xid] });
    void queries.invalidateQueries({ queryKey: ["test-versions", xid] });
    void queries.invalidateQueries({ queryKey: ["tests"] });
  };

  const save = useMutation({
    mutationFn: async () => {
      if (!etag.current) throw new Error("Reload the page and try again.");
      const { error: failure } = await api.PATCH("/tests/{xid}", {
        params: { path: { xid }, header: { "If-Match": etag.current } },
        body: {
          title: title.trim(),
          description: description.trim(),
          // Split and trimmed here rather than sent raw: a trailing comma
          // otherwise becomes a tag called "" that shows in every filter.
          tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      setEditing(false);
      refresh();
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const newDraft = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST("/tests/{xid}/versions", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: refresh,
    onError: (failure) => setError(problemText(failure)),
  });

  const clone = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST("/tests/{xid}/clone", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      void queries.invalidateQueries({ queryKey: ["tests"] });
      // react-router 7 returns a promise from `navigate`. Nothing here can
      // usefully await it, so the intent is stated rather than left floating.
      if (data?.xid) void navigate(`/tests/${data.xid}`);
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const archive = useMutation({
    mutationFn: async (versionXid: string) => {
      const { error: failure } = await api.POST("/test-versions/{xid}/archive", {
        params: { path: { xid: versionXid } },
      });
      if (failure) throw failure;
    },
    onSuccess: refresh,
    onError: (failure) => setError(problemText(failure)),
  });

  const download = useMutation({
    mutationFn: async () => {
      if (!exporting) throw new Error("Choose a version to export.");
      await downloadExport(exporting.xid, exporting.versionNo, format, includeKeys);
    },
    onSuccess: () => setError(null),
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.DELETE("/tests/{xid}", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      void queries.invalidateQueries({ queryKey: ["tests"] });
      void navigate("/tests");
    },
    onError: (failure) => setError(problemText(failure)),
  });

  // Never published, so nothing references it and it can go entirely.
  const everPublished = (versions.data ?? []).some(
    (v) => v.status === "published" || v.status === "archived",
  );

  if (test.isError) return <div className="page error">{problemText(test.error)}</div>;

  return (
    <div className="page">
      {editing ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            setError(null);
            save.mutate();
          }}
        >
          <label htmlFor="t-title">Title</label>
          <input id="t-title" value={title} required
                 onChange={(event) => setTitle(event.target.value)} />

          <label htmlFor="t-desc">Description</label>
          <textarea id="t-desc" rows={2} value={description}
                    onChange={(event) => setDescription(event.target.value)} />

          <label htmlFor="t-tags">Tags</label>
          <input id="t-tags" value={tags} placeholder="academic, winter, mock"
                 onChange={(event) => setTags(event.target.value)} />
          <p className="muted">
            Comma separated. These are how a paper is found in a library of
            forty, which is why they are editable without a new version — the
            content has not changed, only how you look for it.
          </p>

          <div className="row">
            <button disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save"}
            </button>
            <button type="button" className="link" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <>
          <h1>{test.data?.title ?? "Test"}</h1>
          {test.data?.description && <p>{test.data.description}</p>}
          <p className="muted">
            {test.data?.kind} · {test.data?.variant} · {test.data?.skills?.join(", ")}
            {test.data?.tags?.length ? ` · ${test.data.tags.join(", ")}` : ""}
          </p>
          <button className="link" onClick={() => setEditing(true)}>Edit details</button>
        </>
      )}

      {error && <p className="error">{error}</p>}

      <div className="row">
        <button onClick={() => newDraft.mutate()} disabled={newDraft.isPending}>
          {newDraft.isPending ? "Creating…" : "Start a new draft version"}
        </button>
        <button className="link" onClick={() => clone.mutate()} disabled={clone.isPending}>
          {clone.isPending ? "Cloning…" : "Clone this test"}
        </button>
        {!everPublished && (
          <button
            className="link"
            onClick={() => remove.mutate()}
            disabled={remove.isPending}
          >
            Delete
          </button>
        )}
      </div>
      <p className="muted">
        A clone copies the composition, not the material: it points at the same
        passages and question groups, so it costs almost nothing and editing it
        cannot touch this one.
        {everPublished && (
          <> This test cannot be deleted — a version of it has been published,
          and attempts reference published versions for ever. Archive a version
          instead.</>
        )}
      </p>

      <table>
        <thead>
          <tr><th>Version</th><th>Status</th><th>Questions</th><th /><th /><th /></tr>
        </thead>
        <tbody>
          {/* A bare array, not the `{items, next_cursor}` envelope `/tests`
              uses. Defensible — a test has a handful of versions and paging them
              would be ceremony — but it does mean the shape differs between two
              adjacent list endpoints, so read the types rather than assume. */}
          {versions.data?.map((version) => (
            <tr key={version.xid}>
              <td>
                v{version.version_no}
                <SectionSummary xid={version.xid} />
              </td>
              <td>{version.status}</td>
              <td>{version.total_questions ?? 0}</td>
              <td>
                {/* `link`, because this is an ACTION sitting beside Archive and
                    Export — not the row's subject. Without it `td a` painted it
                    ink while its two neighbours came out accent, and three
                    peers one cell apart read as different kinds of thing. */}
                <Link className="link" to={`/versions/${version.xid}`}>
                  {version.status === "draft" ? "Compose" : "View"}
                </Link>
              </td>
              <td>
                {version.status === "published" && (
                  <button
                    className="link"
                    onClick={() => archive.mutate(version.xid)}
                    disabled={archive.isPending}
                    /* The ONLY move available on published content. It stops the
                       version being assignable and leaves every attempt already
                       sat against it resolving exactly as before. */
                    title="Stop this version being assignable; keep what was already sat"
                  >
                    Archive
                  </button>
                )}
              </td>
              <td>
                {/* Every status. A draft is exactly what a centre wants to take
                    into a spreadsheet and bring back, and a published one is what
                    they want a copy of for their files. */}
                <button
                  className="link"
                  onClick={() => {
                    setError(null);
                    setExporting(
                      exporting?.xid === version.xid
                        ? null
                        : { xid: version.xid, versionNo: version.version_no },
                    );
                  }}
                >
                  {exporting?.xid === version.xid ? "Close" : "Export"}
                </button>
              </td>
            </tr>
          ))}
          {versions.data?.length === 0 && (
            <tr><td colSpan={6} className="muted">No versions yet.</td></tr>
          )}
        </tbody>
      </table>

      {exporting && (
        <div className="issued">
          <h2>Export v{exporting.versionNo}</h2>

          <label htmlFor="x-format">Format</label>
          <select
            id="x-format"
            value={format}
            onChange={(event) => setFormat(event.target.value as Format)}
          >
            <option value="json">JSON — the whole paper</option>
            <option value="csv">CSV — one row per question</option>
          </select>
          <p className="muted">
            Both re-import through Import as a new version of this test. Word is
            not offered: the API accepts it and answers with the JSON file, so the
            option would hand you a Word document that is not one.
          </p>

          <label className="choice">
            <input
              type="checkbox"
              checked={includeKeys}
              onChange={(event) => setIncludeKeys(event.target.checked)}
            />
            Include the answer keys
          </label>
          <p className="muted">
            {/* The box does something: without it the file carries no
                `answer_keys` at all, in either format — the contract's claim
                that JSON always includes them is not what the handler does.
                The second authority check is real (`view_exposure` on top of
                `export`) and today refuses nobody who could export, since both
                carry the same three roles — so this copy says the server checks,
                and does not promise that it stops anyone.
                Nothing here says the download is recorded, because it is not:
                the export endpoint writes no audit row. */}
            Off by default. With it on, the file contains every correct answer —
            treat it like the marked paper. The server checks again that you may
            see the keys.
          </p>

          <p className="muted">
            An export is rebuilt from this test's material as it stands now. A
            published version serves a copy frozen at publication, so if a passage
            or an item has been edited since, this file will not match the paper
            that was sat.
          </p>

          <button onClick={() => download.mutate()} disabled={download.isPending}>
            {download.isPending ? "Preparing…" : "Download"}
          </button>
        </div>
      )}
    </div>
  );
}

/** What is actually in a version, without opening it.
 *
 * `GET /test-versions/{xid}/sections` is the light listing beside the heavy
 * detail read the composer uses, and nothing called it. The question it answers
 * here is the one asked while looking at a list of four versions: which of
 * these is the one with the listening section I fixed. Opening each in turn to
 * find out is the alternative, and it loads a full composition every time.
 *
 * Deliberately no passage or audio detail even though the schema carries both:
 * this is a one-line summary under a version number, and a row that grows to
 * four lines stops being scannable, which is the only thing it is for.
 */
function SectionSummary({ xid }: { xid: string }) {
  const sections = useQuery({
    queryKey: ["sections", xid],
    queryFn: async () => {
      const { data, error: failure } = await api.GET(
        "/test-versions/{xid}/sections", { params: { path: { xid } } });
      if (failure) throw failure;
      return data;
    },
  });
  if (!sections.data?.length) return null;
  const skills = sections.data.map((section) => section.skill);
  const counts = skills.reduce<Record<string, number>>(
    (tally, skill) => ({ ...tally, [skill]: (tally[skill] ?? 0) + 1 }), {});
  return (
    <div className="muted">
      {Object.entries(counts)
        .map(([skill, n]) => (n > 1 ? `${n} ${skill}` : skill))
        .join(" · ")}
    </div>
  );
}
