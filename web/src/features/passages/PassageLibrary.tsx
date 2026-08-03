/**
 * Reading passages.
 *
 * A passage is BLOCKS, not HTML — `{ type, runs: [{ t, v }] }` — and that is a
 * decision worth preserving in the editor rather than smoothing over. Blocks are
 * what let a `blank` run carry `ref: "<question_xid>:<slot_key>"`, which is how
 * a gap-fill question is anchored inside the text; an HTML blob could not.
 *
 * **Paragraph labels are assigned by the server and shown read-only here.**
 * Matching-headings questions reference them, so a letter that the client chose
 * would be a letter the scorer disagrees with. `Block.label` is `readOnly: true`
 * in the contract for exactly this reason; the editor displays what came back.
 *
 * A passage carries a copyright attestation for the same reason audio does — it
 * is the other route by which a published Cambridge paper arrives.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { editError, useVersionEdit } from "../edit/useVersionEdit";

const STATEMENT_VERSION = "1";

/** Blank line separates paragraphs. Deliberately the only structure this editor
 *  imposes: a real block editor is its own project, and a plain textarea that
 *  round-trips correctly beats a rich editor that mangles `runs`. */
function toBlocks(text: string) {
  return text
    .split(/\n\s*\n/)
    .map((chunk) => chunk.trim())
    .filter(Boolean)
    .map((chunk) => ({ type: "paragraph" as const, runs: [{ t: "text" as const, v: chunk }] }));
}

export function PassageLibrary() {
  const queries = useQueryClient();
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [claim, setClaim] = useState<"original" | "licensed" | "public_domain" | "permitted_excerpt" | "">("");
  const [open, setOpen] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [editBody, setEditBody] = useState("");

  const edit = useVersionEdit("/passage-versions/{xid}", ["passages"]);
  const [error, setError] = useState<string | null>(null);

  const passages = useQuery({
    queryKey: ["passages"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/passages", {
        params: { query: { limit: 100 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const create = useMutation({
    mutationFn: async () => {
      if (!claim) throw new Error("Say where this passage came from.");
      const { data, error: failure } = await api.POST("/passages", {
        body: {
          title: title.trim(),
          blocks: toBlocks(body),
          attestation: { claim, statement_version: STATEMENT_VERSION },
        },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: () => {
      setTitle("");
      setBody("");
      setClaim("");
      setError(null);
      void queries.invalidateQueries({ queryKey: ["passages"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const detail = useQuery({
    queryKey: ["passage-version", open],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/passage-versions/{xid}", {
        params: { path: { xid: open! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(open),
  });

  const paragraphs = toBlocks(body).length;

  return (
    <div className="page">
      <h1>Reading passages</h1>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          create.mutate();
        }}
      >
        <label htmlFor="p-title">Title</label>
        <input
          id="p-title"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="The history of glass"
          required
        />

        <label htmlFor="p-body">
          Text — one paragraph per block, separated by a blank line
        </label>
        <textarea
          id="p-body"
          value={body}
          onChange={(event) => setBody(event.target.value)}
          rows={10}
          required
        />
        <p className="muted">
          {paragraphs} paragraph{paragraphs === 1 ? "" : "s"}. Letters (A, B, C…)
          are assigned by the server once saved — matching-headings questions
          reference them, so they are not the editor's to choose.
        </p>

        <fieldset>
          <legend>Where did this passage come from?</legend>
          {([
            ["original", "We wrote it ourselves"],
            ["licensed", "We hold a licence covering this use"],
            ["public_domain", "It is in the public domain"],
            ["permitted_excerpt", "It is a permitted excerpt"],
          ] as const).map(([value, label]) => (
            <label key={value} className="choice">
              <input
                type="radio"
                name="p-claim"
                checked={claim === value}
                onChange={() => setClaim(value)}
                required
              />
              {label}
            </label>
          ))}
        </fieldset>

        <button disabled={create.isPending}>
          {create.isPending ? "Saving…" : "Create passage"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}
      {passages.isError && <p className="error">{problemText(passages.error)}</p>}

      <table>
        <thead>
          <tr><th>Title</th><th>Words</th><th>Version</th><th /></tr>
        </thead>
        <tbody>
          {passages.data?.items?.map((passage) => (
            <tr key={passage.xid}>
              <td>{passage.title}</td>
              <td className="muted">{passage.current_version?.word_count ?? "—"}</td>
              <td className="muted">
                v{passage.current_version?.version_no} · {passage.current_version?.status}
              </td>
              <td>
                <button
                  className="link"
                  onClick={() =>
                    setOpen(open === passage.current_version?.xid
                      ? null
                      : (passage.current_version?.xid ?? null))
                  }
                >
                  {open === passage.current_version?.xid ? "Hide" : "View"}
                </button>
              </td>
            </tr>
          ))}
          {passages.data?.items?.length === 0 && (
            <tr><td colSpan={4} className="muted">No passages yet.</td></tr>
          )}
        </tbody>
      </table>

      {open && detail.data && editing && (
        <div className="issued">
          <h2>Edit passage</h2>
          <label htmlFor="p-etitle">Title</label>
          <input id="p-etitle" value={editTitle}
                 onChange={(event) => setEditTitle(event.target.value)} />
          <label htmlFor="p-ebody">Text</label>
          <textarea id="p-ebody" rows={10} value={editBody}
                    onChange={(event) => setEditBody(event.target.value)} />
          <p className="muted">
            One paragraph per blank line. Paragraph LETTERS are reassigned
            server-side on every save, so adding a paragraph in the middle
            renumbers the ones after it — and a matching-headings key that
            pointed at C now points somewhere else. Check the keys after a
            structural edit; the publish gate checks the count, not the meaning.
          </p>
          <div className="row">
            <button
              disabled={edit.isPending}
              onClick={() =>
                edit.mutate(
                  { xid: open, body: { title: editTitle.trim(),
                                       blocks: toBlocks(editBody) } },
                  {
                    onSuccess: () => setEditing(false),
                    onError: (failure) => setError(editError(failure)),
                  },
                )
              }
            >
              {edit.isPending ? "Saving…" : "Save"}
            </button>
            <button type="button" className="link" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {open && detail.data && !editing && (
        <div className="passage-view">
          <h2>{detail.data.title}</h2>
          {detail.data.status === "draft" && (
            <button
              className="link"
              onClick={() => {
                setError(null);
                setEditTitle(detail.data!.title ?? "");
                setEditBody((detail.data!.blocks ?? [])
                  .map((b) => (b.runs ?? []).map((r) => r.v).join(""))
                  .join("\n\n"));
                setEditing(true);
              }}
            >
              Edit
            </button>
          )}
          {detail.data.blocks?.map((block, index) => (
            <p key={index}>
              {/* The server-assigned letter, shown beside its paragraph so an
                  author writing a matching-headings key can see which is which. */}
              {detail.data!.paragraph_labels?.[index] && (
                <strong className="para-label">
                  {detail.data!.paragraph_labels[index]}
                </strong>
              )}{" "}
              {(block.runs ?? []).map((run) => run.v).join("")}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
