/**
 * One test and its versions.
 *
 * The version list is the spine of the authoring model: a published version is
 * frozen, and changing anything means starting a new draft from it. Making that
 * visible here — rather than offering an edit button that 409s — is the
 * difference between a versioning model a teacher understands and one that
 * feels like the software fighting them.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { api, problemText } from "../../api/client";

export function TestDetail() {
  const { xid = "" } = useParams();
  const queries = useQueryClient();

  const test = useQuery({
    queryKey: ["test", xid],
    queryFn: async () => {
      const { data, error } = await api.GET("/tests/{xid}", { params: { path: { xid } } });
      if (error) throw error;
      return data;
    },
    enabled: Boolean(xid),
  });

  const versions = useQuery({
    queryKey: ["test-versions", xid],
    queryFn: async () => {
      const { data, error } = await api.GET("/tests/{xid}/versions", {
        params: { path: { xid } },
      });
      if (error) throw error;
      return data;
    },
    enabled: Boolean(xid),
  });

  const newDraft = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/tests/{xid}/versions", {
        params: { path: { xid } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => queries.invalidateQueries({ queryKey: ["test-versions", xid] }),
  });

  if (test.isError) return <div className="page error">{problemText(test.error)}</div>;

  return (
    <div className="page">
      <h1>{test.data?.title ?? "Test"}</h1>
      <p className="muted">
        {test.data?.kind} · {test.data?.variant} · {test.data?.skills?.join(", ")}
      </p>

      <div className="row">
        <button onClick={() => newDraft.mutate()} disabled={newDraft.isPending}>
          {newDraft.isPending ? "Creating…" : "Start a new draft version"}
        </button>
      </div>
      {newDraft.isError && <p className="error">{problemText(newDraft.error)}</p>}

      <table>
        <thead>
          <tr><th>Version</th><th>Status</th><th>Questions</th><th /></tr>
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
            </tr>
          ))}
          {versions.data?.length === 0 && (
            <tr><td colSpan={4} className="muted">No versions yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
