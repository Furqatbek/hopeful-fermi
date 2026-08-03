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
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api, problemText } from "../../api/client";

export function TestDetail() {
  const { xid = "" } = useParams();
  const navigate = useNavigate();
  const queries = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [error, setError] = useState<string | null>(null);

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
      if (data?.xid) navigate(`/tests/${data.xid}`);
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

  const remove = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.DELETE("/tests/{xid}", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      void queries.invalidateQueries({ queryKey: ["tests"] });
      navigate("/tests");
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
          <tr><th>Version</th><th>Status</th><th>Questions</th><th /><th /></tr>
        </thead>
        <tbody>
          {/* A bare array, not the `{items, next_cursor}` envelope `/tests`
              uses. Defensible — a test has a handful of versions and paging them
              would be ceremony — but it does mean the shape differs between two
              adjacent list endpoints, so read the types rather than assume. */}
          {versions.data?.map((version) => (
            <tr key={version.xid}>
              <td>v{version.version_no}</td>
              <td>{version.status}</td>
              <td>{version.total_questions ?? 0}</td>
              <td>
                <Link to={`/versions/${version.xid}`}>
                  {version.status === "draft" ? "Compose" : "View"}
                </Link>
              </td>
              <td>
                {version.status === "published" && (
                  <button
                    className="link"
                    onClick={() => archive.mutate(version.xid!)}
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
            </tr>
          ))}
          {versions.data?.length === 0 && (
            <tr><td colSpan={5} className="muted">No versions yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
