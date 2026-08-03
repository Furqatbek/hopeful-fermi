/**
 * Speaking cue-card sets: the prompts a speaking session is run from.
 *
 * Authored with the same versioning and visibility as every other content asset,
 * which is why speaking has no content model of its own. What that means in
 * practice here is narrower than it sounds, and the difference is worth stating
 * because it shapes the whole screen.
 *
 * **A set is created complete, and it is the last time anybody sees its body.**
 * `POST /cue-card-sets` writes the set and version 1 together and returns the
 * version's xid. There is no `POST /cue-card-sets/{xid}/versions`, no
 * `GET /cue-card-set-versions/{xid}`, and no PATCH — the four that passages have.
 * So unlike `PassageLibrary`, which creates, then versions, then edits a draft,
 * this screen can only create and list, and the prompts typed into the form
 * below cannot be read back through any endpoint afterwards.
 *
 * Two things follow. The form checks the prompts before sending, because there
 * is no later opportunity. And the version xid is shown once, prominently, like
 * an invite token — it is the value a speaking slot is attached to, and the
 * listing gives it back so this is a convenience rather than the only copy.
 *
 * **Sets default to organization-private.** A centre's material must not reach a
 * competitor, so the listing returns platform-global sets, this centre's sets,
 * and the author's own private ones — nothing else, and the screen says so
 * rather than leaving an author to guess how far their prompts travel.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { promptCount, promptProblems, toBody } from "./prompts";

export function CueCards() {
  const queries = useQueryClient();
  const [title, setTitle] = useState("");
  const [tags, setTags] = useState("");
  const [part1, setPart1] = useState("");
  const [topic, setTopic] = useState("");
  const [bullets, setBullets] = useState("");
  const [part3, setPart3] = useState("");
  const [made, setMade] = useState<{ title: string; version: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const sets = useQuery({
    queryKey: ["cue-card-sets"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/cue-card-sets");
      if (failure) throw failure;
      return data;
    },
  });

  const body = toBody(part1, topic, bullets, part3);
  const problems = promptProblems(title, body, bullets);

  const create = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST("/cue-card-sets", {
        body: {
          title: title.trim(),
          tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
          body,
        },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      // Kept on screen after the form clears. `current_version_xid` is what
      // `POST /speaking/slots` attaches, and a teacher who authored a set for a
      // session this afternoon needs it in the next minute.
      setMade({ title: title.trim(), version: data?.current_version_xid ?? "" });
      setTitle("");
      setTags("");
      setPart1("");
      setTopic("");
      setBullets("");
      setPart3("");
      void queries.invalidateQueries({ queryKey: ["cue-card-sets"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  return (
    <div className="page">
      <h1>Cue card sets</h1>
      <p className="muted">
        The prompts a speaking session runs from: part 1 questions, one part 2
        card, part 3 discussion questions. A set is attached to a speaking slot
        when the slot is opened.
      </p>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          setMade(null);
          if (problems.length === 0) create.mutate();
        }}
      >
        <label htmlFor="cc-title">Title</label>
        <input
          id="cc-title"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Describe a journey you remember"
          required
        />

        <label htmlFor="cc-tags">Tags, separated by commas</label>
        <input
          id="cc-tags"
          value={tags}
          onChange={(event) => setTags(event.target.value)}
          placeholder="travel, part 2"
        />

        <label htmlFor="cc-p1">Part 1 questions — one per line</label>
        <textarea
          id="cc-p1"
          value={part1}
          onChange={(event) => setPart1(event.target.value)}
          rows={4}
          placeholder={"Do you work or study?\nWhere do you live?"}
        />

        <label htmlFor="cc-topic">Part 2 topic</label>
        <input
          id="cc-topic"
          value={topic}
          onChange={(event) => setTopic(event.target.value)}
          placeholder="Describe a journey you remember well"
        />

        <label htmlFor="cc-bullets">Part 2 bullets — one per line</label>
        <textarea
          id="cc-bullets"
          value={bullets}
          onChange={(event) => setBullets(event.target.value)}
          rows={4}
          placeholder={"where you went\nwho you were with\nwhy you remember it"}
        />

        <label htmlFor="cc-p3">Part 3 questions — one per line</label>
        <textarea
          id="cc-p3"
          value={part3}
          onChange={(event) => setPart3(event.target.value)}
          rows={4}
          placeholder={"Why do people travel?\nHas tourism changed your city?"}
        />

        <p className="muted">
          {promptCount(body)} prompt{promptCount(body) === 1 ? "" : "s"}. Check
          them before saving: a set cannot be opened, edited or versioned again
          once it is created, and no endpoint returns its prompts back to this
          console.
        </p>

        {problems.length > 0 && (title || part1 || topic || bullets || part3) && (
          <p className="error">{problems.join("\n")}</p>
        )}

        <button disabled={create.isPending || problems.length > 0}>
          {create.isPending ? "Saving…" : "Create set"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}
      {sets.isError && <p className="error">{problemText(sets.error)}</p>}

      {made && (
        <div className="issued">
          <h2>Saved</h2>
          <p>
            <strong>{made.title}</strong> is stored as version 1. Attach it to a
            speaking slot with this version identifier:
          </p>
          <p className="token">{made.version}</p>
          <p className="muted">
            It is in the list below too. The Speaking screen offers it by name, so
            copying this is only needed if you are opening the slot elsewhere.
          </p>
        </div>
      )}

      <h2>Library</h2>
      <table>
        <thead>
          <tr><th>Title</th><th>Tags</th><th>Visible to</th><th>Current version</th></tr>
        </thead>
        <tbody>
          {sets.data?.map((set) => (
            <tr key={set.xid}>
              <td>{set.title}</td>
              <td className="muted">{(set.tags ?? []).join(", ") || "—"}</td>
              <td className="muted">
                {set.visibility === "platform_global"
                  ? "Every centre"
                  : set.visibility === "author_private"
                    ? "You only"
                    : "Your centre"}
              </td>
              <td className="num">
                {/* A set with no version cannot be attached to anything. The
                    create path always writes version 1, so this reads "—" only
                    for a row written some other way. */}
                {set.current_version_xid ?? "—"}
              </td>
            </tr>
          ))}
          {sets.data?.length === 0 && (
            <tr><td colSpan={4} className="muted">No cue card sets yet.</td></tr>
          )}
        </tbody>
      </table>
      <p className="muted">
        This list holds sets shared with every centre, your own centre's sets, and
        your private ones. A set you create belongs to your centre and is not
        visible to another one.
      </p>
    </div>
  );
}
