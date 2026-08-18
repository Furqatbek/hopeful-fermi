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
 *
 * **The lifecycle, and what each step does not do.** A draft can be edited; a
 * published version is frozen and the server answers `version_immutable`, so no
 * edit control is offered for one. Publishing a passage does not touch any test:
 * a section points at ONE passage version, so the new version is used by nothing
 * until somebody re-points a section at it from the test's composition screen.
 * That is why usage is read per VERSION here and shown above the edit control —
 * it is the only place an author can see, before typing, which papers the letters
 * they are about to renumber belong to.
 */

import { ArchiveButton } from "../archive/ArchiveButton";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Pager, usePaged } from "../../app/paging";
import { api, problemText } from "../../api/client";
import { editError, useVersionEdit } from "../edit/useVersionEdit";
import { UsagePanel } from "../usage/UsagePanel";
import { VisibilityPicker } from "../archive/VisibilityPicker";

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

function toText(blocks: { runs?: { v?: string }[] }[]): string {
  return blocks.map((b) => (b.runs ?? []).map((r) => r.v).join("")).join("\n\n");
}

export function PassageLibrary() {
  const queries = useQueryClient();
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [claim, setClaim] = useState<"original" | "licensed" | "public_domain" | "permitted_excerpt" | "">("");
  const [open, setOpen] = useState<{ passage: string; version: string } | null>(null);
  const [editing, setEditing] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [editBody, setEditBody] = useState("");

  const edit = useVersionEdit("/passage-versions/{xid}", ["passages"]);
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);

  // `q` has been implemented on this endpoint and sent by nothing. It matters
  // more now that a page is 25 rather than 100: finding one item in a library of
  // two hundred by pressing "Show more" eight times is worse than the limit it
  // replaced. In the query key, so typing restarts from the first page rather
  // than appending filtered results under unfiltered ones.
  const passages = usePaged(["passages", search], async (cursor) => {
    const { data, error: failure } = await api.GET("/passages", {
      params: { query: { limit: 25, ...(cursor ? { cursor } : {}),
                        ...(search.trim() ? { q: search.trim() } : {}) } },
    });
    if (failure) throw failure;
    return data ?? {};
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
    queryKey: ["passage-version", open?.version],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/passage-versions/{xid}", {
        params: { path: { xid: open!.version } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(open),
  });

  const newVersion = useMutation({
    mutationFn: async (passageXid: string) => {
      const { data, error: failure } = await api.POST("/passages/{xid}/versions", {
        params: { path: { xid: passageXid } },
      });
      if (failure) throw failure;
      return { passageXid, version: data };
    },
    onSuccess: ({ passageXid, version }) => {
      // An empty copy is reported, not shown silently. `new_passage_version`
      // copies from `passages.current_version_id`, and the importer never sets
      // it — so for every passage a centre imported the new draft arrives with
      // no text, and an author looking at a blank editor assumes they deleted it.
      setError(
        version && (version.blocks?.length ?? 0) === 0
          ? "The new version came back with no text. Paste the text in before "
            + "publishing."
          : null,
      );
      setEditing(false);
      const versionXid = version?.xid;
      if (versionXid) {
        setOpen({ passage: passageXid, version: versionXid });
      }
      void queries.invalidateQueries({ queryKey: ["passages"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const publish = useMutation({
    mutationFn: async (versionXid: string) => {
      const { error: failure } = await api.POST("/passage-versions/{xid}/publish", {
        params: { path: { xid: versionXid } },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      setEditing(false);
      void queries.invalidateQueries({ queryKey: ["passage-version", open?.version] });
      void queries.invalidateQueries({ queryKey: ["passages"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const paragraphs = toBlocks(body).length;
  const version = detail.data;
  const isDraft = version?.status === "draft";

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

      <label htmlFor="passages-q" className="sr-label">Search</label>
      <input
        id="passages-q"
        type="search"
        className="search"
        value={search}
        placeholder="Search by title"
        onChange={(event) => setSearch(event.target.value)}
      />
      <div className="scroll">
        <table>
          <thead>
            <tr><th>Title</th><th>Words</th><th>Version</th><th>Visible to</th><th /><th /></tr>
          </thead>
          <tbody>
            {passages.items.map((passage) => {
              const versionXid = passage.current_version?.xid;
              return (
                <tr key={passage.xid}>
                  <td>{passage.title}</td>
                  <td className="muted">{passage.current_version?.word_count ?? "—"}</td>
                  <td className="muted">
                    {passage.current_version
                      ? <>v{passage.current_version.version_no} · {passage.current_version.status}</>
                      : "—"}
                  </td>
                  <td>
                    {versionXid ? (
                      <button
                        className="link"
                        onClick={() => {
                          setEditing(false);
                          setOpen(open?.version === versionXid
                            ? null
                            : { passage: passage.xid, version: versionXid });
                        }}
                      >
                        {open?.version === versionXid ? "Hide" : "View"}
                      </button>
                    ) : (
                      <button
                        className="link"
                        disabled={newVersion.isPending}
                        onClick={() => {
                          setError(null);
                          newVersion.mutate(passage.xid);
                        }}
                      >
                        Start a new version
                      </button>
                    )}
                  </td>
                  <td>
                    <VisibilityPicker endpoint="/passages/{xid}/visibility"
                                      xid={passage.xid ?? ""}
                                      visibility={passage.visibility}
                                      invalidate={["passages"]} />
                  </td>
                  <td>
                    <ArchiveButton endpoint="/passages/{xid}/archive"
                                   xid={passage.xid ?? ""}
                                   invalidate={["passages"]} label="passage"
                                   name={passage.title ?? ""} />
                  </td>
                </tr>
              );
            })}
            {passages.items.length === 0 && (
              <tr><td colSpan={6} className="muted">No passages yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <Pager shown={passages.items.length} hasMore={passages.hasMore}
             onMore={passages.more} loading={passages.loadingMore}
             noun="passages" />
      <p className="muted">
        Word count and version are blank for a passage this browser has not
        created or versioned: the list endpoint returns no version for any row, so
        there is nothing to address one by. Starting a new version answers with a
        draft and opens it.
      </p>

      {open && version && (
        <div className="passage-view">
          <h2>
            {version.title} · v{version.version_no} · {version.status}
          </h2>

          {/* Above every control that changes anything. This is the panel's whole
              purpose: the letters below are reassigned server-side on each save,
              and these are the papers whose matching-headings keys point at them. */}
          <UsagePanel subject="passage-version" xid={open.version} />

          <div className="row">
            {isDraft && !editing && (
              <button
                className="link"
                onClick={() => {
                  setError(null);
                  setEditTitle(version.title ?? "");
                  setEditBody(toText(version.blocks ?? []));
                  setEditing(true);
                }}
              >
                Edit
              </button>
            )}
            {isDraft && (
              <button
                onClick={() => {
                  setError(null);
                  publish.mutate(open.version);
                }}
                disabled={publish.isPending}
              >
                {publish.isPending ? "Publishing…" : "Publish this version"}
              </button>
            )}
            <button
              className="link"
              disabled={newVersion.isPending}
              onClick={() => {
                setError(null);
                newVersion.mutate(open.passage);
              }}
            >
              {newVersion.isPending ? "Starting…" : "Start a new version"}
            </button>
          </div>

          <p className="muted">
            {isDraft
              ? "Publishing freezes this version. It cannot be edited afterwards. "
              : "This version is published, so it cannot be edited. Start a new version to change it. "}
            Publishing changes no test on its own. A section points at one passage
            version, so a paper uses this one only once its composition is pointed
            at it. Publishing may be limited to centre admins; your centre can
            allow teachers.
          </p>
          <p className="muted">
            {/* Verified, and it costs an author their work: the copy is taken
                from `passages.current_version_id`, and `new_passage_version`
                never advances that pointer. */}
            A new version copies the passage's current version, and the server
            does not move that pointer — so starting a second new version copies
            the same text again, not this draft. Finish one before starting
            another.
          </p>

          {editing && (
            <>
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
                      { xid: open.version, body: { title: editTitle.trim(),
                                                   blocks: toBlocks(editBody) } },
                      {
                        onSuccess: () => {
                          setEditing(false);
                          void queries.invalidateQueries({
                            queryKey: ["passage-version", open.version] });
                        },
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
            </>
          )}

          {version.blocks?.map((block, index) => (
            <p key={index}>
              {/* The server-assigned letter, shown beside its paragraph so an
                  author writing a matching-headings key can see which is which. */}
              {version.paragraph_labels?.[index] && (
                <strong className="para-label">
                  {version.paragraph_labels[index]}
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
