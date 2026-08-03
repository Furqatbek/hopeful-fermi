/**
 * Pulling an existing question group into a section.
 *
 * Reuse by REFERENCE — the placement points at a question group VERSION, and no
 * copy is made. That is why the same group can sit in two tests and why fixing a
 * key in the bank is a regrade rather than an archaeology exercise.
 *
 * The picker offers only groups whose current version is published or draft-
 * complete enough to have one at all; a group with no version cannot be placed,
 * and offering it produces a 404 the author cannot act on.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";

export function AttachGroup({ sectionXid, versionXid, nextPosition }: {
  sectionXid: string;
  versionXid: string;
  nextPosition: number;
}) {
  const queries = useQueryClient();
  const [open, setOpen] = useState(false);
  const [groupVersionXid, setGroupVersionXid] = useState("");
  const [error, setError] = useState<string | null>(null);

  const groups = useQuery({
    queryKey: ["question-groups"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/question-groups", {
        params: { query: { limit: 100 } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: open,
  });

  const attach = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/sections/{xid}/groups", {
        params: { path: { xid: sectionXid } },
        body: { group_version_xid: groupVersionXid, position: nextPosition },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setOpen(false);
      setGroupVersionXid("");
      void queries.invalidateQueries({ queryKey: ["test-version", versionXid] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  if (!open) {
    return (
      <button className="link small" onClick={() => setOpen(true)}>
        + Add a question group
      </button>
    );
  }

  const placeable = (groups.data?.items ?? []).filter((g) => g.current_version?.xid);

  return (
    <form
      className="row"
      onSubmit={(event) => {
        event.preventDefault();
        setError(null);
        if (groupVersionXid) attach.mutate();
      }}
    >
      <select
        value={groupVersionXid}
        onChange={(event) => setGroupVersionXid(event.target.value)}
        aria-label="Question group"
        required
      >
        <option value="">— choose a group —</option>
        {placeable.map((group) => (
          <option key={group.xid} value={group.current_version!.xid}>
            {group.title} ({group.skill})
          </option>
        ))}
      </select>
      <button disabled={attach.isPending || !groupVersionXid}>
        {attach.isPending ? "Adding…" : "Add"}
      </button>
      <button type="button" className="link" onClick={() => setOpen(false)}>
        Cancel
      </button>
      {groups.data && placeable.length === 0 && (
        <p className="muted">
          No question groups have a version yet. Create one under Questions first.
        </p>
      )}
      {error && <p className="error">{error}</p>}
    </form>
  );
}
