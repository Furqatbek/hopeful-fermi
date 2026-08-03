/**
 * Question groups — the missing link in the authoring chain.
 *
 * A section is not filled with questions, it is filled with GROUP VERSIONS
 * (`GroupPlacementCreate.group_version_xid`). So a test cannot be composed until
 * a group exists, and until this screen there was no way to make one: the console
 * could create tests, versions, sections, passages, audio and questions, and then
 * stop. Groups arrived only through import or a direct API call.
 *
 * **The group carries what an IELTS question set shares**, and none of it is
 * decoration:
 *
 * `instructions` is the rubric a candidate reads — "Complete the sentences.
 * Write ONE WORD ONLY." Localized by key, because a Tashkent centre may want the
 * rubric in Uzbek beside the English the real exam prints.
 *
 * `word_limit` is **enforced by the scorer**, not printed next to it. An
 * over-limit answer is marked wrong outright rather than truncated and
 * re-compared, which is how the real exam marks and why the setting belongs to
 * the group rather than to each question: "ONE WORD ONLY" governs the whole set,
 * and letting it drift per item is how one question in eleven starts marking
 * differently.
 *
 * `option_bank` is the shared list the matching and word-bank types choose from.
 * A per-question copy would let two questions in one set offer different letters
 * for the same heading.
 *
 * Adding a question is **reuse by reference** — the item points at a question
 * VERSION and nothing is copied. That is what lets the same item sit in two tests
 * and what makes a key fix a regrade rather than an archaeology exercise.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { editError, useVersionEdit } from "../edit/useVersionEdit";

export function GroupLibrary() {
  const queries = useQueryClient();
  const [title, setTitle] = useState("");
  const [skill, setSkill] = useState<"reading" | "listening">("reading");
  const [rubric, setRubric] = useState("");
  const [maxWords, setMaxWords] = useState("");
  const [bank, setBank] = useState("");
  const [opened, setOpened] = useState<string | null>(null);
  const [adding, setAdding] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [editRubric, setEditRubric] = useState("");
  const [editWords, setEditWords] = useState("");
  const [editing, setEditing] = useState(false);

  const edit = useVersionEdit("/question-group-versions/{xid}", ["question-groups"]);

  const groups = useQuery({
    queryKey: ["question-groups"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/question-groups", {
        params: { query: { limit: 100 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const questions = useQuery({
    queryKey: ["questions"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/questions", {
        params: { query: { limit: 200 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const detail = useQuery({
    queryKey: ["group-version", opened],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/question-group-versions/{xid}", {
        params: { path: { xid: opened! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(opened),
  });

  const create = useMutation({
    mutationFn: async () => {
      const options = bank
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          // "A = Living near water" — the letter a candidate writes, then the
          // text they read. Split on the FIRST separator only, so a heading may
          // itself contain one.
          const at = line.search(/[=|]/);
          const id = at === -1 ? line : line.slice(0, at).trim();
          const text = at === -1 ? line : line.slice(at + 1).trim();
          return { id, text };
        });
      const { data, error: failure } = await api.POST("/question-groups", {
        body: {
          title: title.trim(),
          skill,
          ...(rubric.trim() ? { instructions: { en: rubric.trim() } } : {}),
          ...(maxWords
            ? {
                word_limit: {
                  max_words: Number(maxWords),
                  // Sent explicitly rather than left to the server's defaults.
                  // These two decide real marks — whether "14" satisfies "ONE
                  // WORD" and whether "double-decker" is one word or two — and a
                  // rule that important should be in the request that set it,
                  // not inferred from a default that may change.
                  allow_number: true,
                  hyphen_counts_as_one: true,
                  // The only value the enum admits, and the reason the limit is
                  // a marking rule rather than a hint: an over-limit answer is
                  // marked wrong, never truncated and compared again.
                  on_violation: "mark_incorrect",
                },
              }
            : {}),
          ...(options.length ? { option_bank: options } : {}),
        },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      setTitle("");
      setRubric("");
      setMaxWords("");
      setBank("");
      // Straight into the new group: a group with no questions in it cannot fill
      // a section, so creating one is half the job.
      if (data?.current_version?.xid) setOpened(data.current_version.xid);
      void queries.invalidateQueries({ queryKey: ["question-groups"] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const addItem = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST(
        "/question-group-versions/{xid}/items",
        {
          params: { path: { xid: opened! } },
          // No `position`: the server appends. Sending one from a list the
          // author has not reordered would assert an order they never chose.
          body: { question_version_xid: adding },
        },
      );
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      setAdding("");
      void queries.invalidateQueries({ queryKey: ["group-version", opened] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const withVersions = (questions.data?.items ?? []).filter((q) => q.current_version?.xid);
  const already = new Set(
    (detail.data?.items ?? []).map((item) => item.question_version?.xid),
  );

  return (
    <div className="page">
      <h1>Question groups</h1>
      <p className="muted">
        A section is filled with groups, not with loose questions — so a test
        cannot be composed until the questions are gathered into one.
      </p>
      {error && <p className="error">{error}</p>}

      <h2>New group</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          create.mutate();
        }}
      >
        <label htmlFor="g-title">Title</label>
        <input
          id="g-title"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Questions 1–5"
          required
        />

        <label htmlFor="g-skill">Skill</label>
        <select
          id="g-skill"
          value={skill}
          onChange={(event) => setSkill(event.target.value as "reading" | "listening")}
        >
          <option value="reading">Reading</option>
          <option value="listening">Listening</option>
        </select>

        <label htmlFor="g-rubric">Instructions</label>
        <textarea
          id="g-rubric"
          rows={2}
          value={rubric}
          onChange={(event) => setRubric(event.target.value)}
          placeholder="Complete the sentences below. Write ONE WORD ONLY for each answer."
        />

        <label htmlFor="g-words">Word limit</label>
        <select
          id="g-words"
          value={maxWords}
          onChange={(event) => setMaxWords(event.target.value)}
        >
          <option value="">No limit</option>
          <option value="1">ONE WORD ONLY</option>
          <option value="2">NO MORE THAN TWO WORDS</option>
          <option value="3">NO MORE THAN THREE WORDS</option>
        </select>
        <p className="muted">
          Enforced by the marker, not printed beside it: an over-limit answer is
          marked wrong outright rather than trimmed and re-checked. A number
          counts as a word and a hyphenated compound counts as one, as in real
          IELTS marking.
        </p>

        <label htmlFor="g-bank">Option bank</label>
        <textarea
          id="g-bank"
          rows={4}
          value={bank}
          onChange={(event) => setBank(event.target.value)}
          placeholder={"A = The city's first bridge\nB = Living near water"}
        />
        <p className="muted">
          One per line, <code>letter = text</code>. Only for matching and
          word-bank sets; leave it empty otherwise. It lives on the group so every
          question in the set offers the same letters.
        </p>

        <button disabled={create.isPending || !title.trim()}>
          {create.isPending ? "Creating…" : "Create group"}
        </button>
      </form>

      <h2>Groups</h2>
      <table>
        <thead>
          <tr><th>Title</th><th>Skill</th><th>Rubric</th><th>Limit</th><th /></tr>
        </thead>
        <tbody>
          {groups.data?.items?.map((group) => {
            const version = group.current_version;
            return (
              <tr key={group.xid}>
                <td>{group.title}</td>
                <td className="muted">{group.skill}</td>
                <td className="muted">
                  {String((version?.instructions as Record<string, string>)?.["en"] ?? "—")}
                </td>
                <td className="muted">
                  {version?.word_limit?.max_words
                    ? `${version.word_limit.max_words} word${version.word_limit.max_words === 1 ? "" : "s"}`
                    : "—"}
                </td>
                <td>
                  {version?.xid && (
                    <button
                      className="link"
                      onClick={() =>
                        setOpened(opened === version.xid ? null : version.xid!)
                      }
                    >
                      {opened === version.xid ? "Close" : "Questions"}
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
          {groups.data?.items?.length === 0 && (
            <tr><td colSpan={5} className="muted">No groups yet.</td></tr>
          )}
        </tbody>
      </table>

      {opened && detail.data && (
        <div className="issued">
          <h2>Questions in this group</h2>
          {editing ? (
            <>
              <label htmlFor="g-erubric">Instructions</label>
              <textarea id="g-erubric" rows={2} value={editRubric}
                        onChange={(event) => setEditRubric(event.target.value)} />
              <label htmlFor="g-ewords">Word limit</label>
              <select id="g-ewords" value={editWords}
                      onChange={(event) => setEditWords(event.target.value)}>
                <option value="">No limit</option>
                <option value="1">ONE WORD ONLY</option>
                <option value="2">NO MORE THAN TWO WORDS</option>
                <option value="3">NO MORE THAN THREE WORDS</option>
              </select>
              <p className="muted">
                Changing the limit changes how every answer in this set is
                MARKED, not just how it reads — an over-limit answer is marked
                wrong. Tightening it after a paper has been sat is a regrade,
                not an edit.
              </p>
              <div className="row">
                <button
                  disabled={edit.isPending}
                  onClick={() =>
                    edit.mutate(
                      {
                        xid: opened,
                        body: {
                          instructions: editRubric.trim()
                            ? { en: editRubric.trim() } : {},
                          ...(editWords
                            ? { word_limit: { max_words: Number(editWords),
                                              allow_number: true,
                                              hyphen_counts_as_one: true,
                                              on_violation: "mark_incorrect" } }
                            : {}),
                        },
                      },
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
            </>
          ) : (
            detail.data.status === "draft" && (
              <button
                className="link"
                onClick={() => {
                  setError(null);
                  setEditRubric(String(
                    (detail.data!.instructions as Record<string, string>)?.["en"] ?? ""));
                  setEditWords(String(detail.data!.word_limit?.max_words ?? ""));
                  setEditing(true);
                }}
              >
                Edit instructions and word limit
              </button>
            )
          )}
          <ol className="tree">
            {detail.data.items?.map((item) => (
              <li key={item.question_version?.xid}>
                {item.question_version?.type_key}{" "}
                <span className="muted">
                  · v{item.question_version?.version_no} ·{" "}
                  {item.question_version?.slot_keys?.join(", ") || "no slots"}
                </span>
              </li>
            ))}
            {detail.data.items?.length === 0 && (
              <li className="muted">
                Empty. A group with no questions passes nothing to a section.
              </li>
            )}
          </ol>

          <form
            className="row"
            onSubmit={(event) => {
              event.preventDefault();
              setError(null);
              if (adding) addItem.mutate();
            }}
          >
            <select
              value={adding}
              onChange={(event) => setAdding(event.target.value)}
              aria-label="Question to add"
            >
              <option value="">— add a question —</option>
              {withVersions
                // Already-placed items are hidden rather than shown and refused:
                // the same version twice in one group would number the same
                // question twice on the paper.
                .filter((q) => !already.has(q.current_version!.xid))
                .map((q) => (
                  <option key={q.xid} value={q.current_version!.xid}>
                    {q.type_key} · {q.skill} ·{" "}
                    {q.current_version!.slot_keys?.join(", ") || "no slots"}
                  </option>
                ))}
            </select>
            <button disabled={addItem.isPending || !adding}>
              {addItem.isPending ? "Adding…" : "Add"}
            </button>
          </form>
          <p className="muted">
            Added by reference — nothing is copied, so the same question can sit in
            two tests and a key fix reaches both.
          </p>
        </div>
      )}
    </div>
  );
}
