/**
 * The test library — the first screen of the authoring console.
 *
 * Deliberately the first real screen rather than a dashboard: it exercises the
 * whole chain end to end (generated types → bearer token → refresh on expiry →
 * an authorization-filtered list) against a surface where a mistake is visible.
 * Every listing in `assets.py` and `tests_authoring.py` goes through
 * `authz.filter_content`, which is what keeps one centre's material out of a
 * competitor's library — so if scoping were broken, this screen is where you
 * would see somebody else's tests.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";

export function TestLibrary() {
  const queries = useQueryClient();
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);

  const tests = useQuery({
    queryKey: ["tests"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/tests", {
        params: { query: { limit: 50 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const create = useMutation({
    mutationFn: async (name: string) => {
      const { data, error: failure } = await api.POST("/tests", {
        body: {
          title: name,
          kind: "mock",
          variant: "academic",
          skills: ["reading", "listening"],
        },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: () => {
      setTitle("");
      setError(null);
      void queries.invalidateQueries({ queryKey: ["tests"] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  return (
    <div className="page">
      <h1>Tests</h1>

      <form
        className="row"
        onSubmit={(event) => {
          event.preventDefault();
          if (title.trim()) create.mutate(title.trim());
        }}
      >
        <input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="New test title"
          aria-label="New test title"
        />
        <button disabled={create.isPending || !title.trim()}>
          {create.isPending ? "Creating…" : "Create"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}

      {tests.isPending && <p className="muted">Loading…</p>}
      {tests.isError && <p className="error">{problemText(tests.error)}</p>}

      {tests.data && (
        <table>
          <thead>
            <tr>
              <th>Title</th>
              <th>Skills</th>
              <th>Visibility</th>
              <th>Current version</th>
            </tr>
          </thead>
          <tbody>
            {tests.data.items?.map((test) => (
              <tr key={test.xid}>
                <td>{test.title}</td>
                <td>{test.skills?.join(", ")}</td>
                <td>{test.visibility}</td>
                <td className="muted">
                  {/* A test with no PUBLISHED version cannot be assigned to
                      students, which is the only thing a teacher wants to know
                      from this column. "Draft" is more useful than an empty
                      cell. */}
                  {test.current_published_version_xid ? "published" : "draft"}
                </td>
              </tr>
            ))}
            {tests.data.items?.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  No tests yet. Create one above.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
