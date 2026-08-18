/**
 * Running a contest, and the two things that make one mean anything.
 *
 * A competition is a RANKING. That is the whole product of it, and it is what
 * every rule here protects.
 *
 * **The paper must be fresh, and the refusal is not overridable.** A ranking
 * computed over a field where some entrants have already seen the paper is not a
 * slightly wrong number — it is a number that means nothing, published under the
 * platform's name next to the names of students who will screenshot it. The
 * server refuses with two different codes and this screen keeps them apart,
 * because the remedies differ: another contest already used this paper (use a
 * different one), or its items have circulated far enough that a good share of
 * any field will have met them (compose a fresh version). Neither has a force
 * flag and this screen does not pretend to look for one.
 *
 * **A published ranking does not re-rank itself.** When a key fix touches a
 * finished contest, applying the regrade is BLOCKED until somebody records a
 * decision — and it is a platform-admin decision, not a centre one, because the
 * centre whose students are ranked is not the party to decide whether their
 * ranking moves. `leave_as_is` keeps the board and says why; `regrade_and_
 * republish` recomputes and requires a public notice, because a ranking that
 * changes silently is worse than one that was wrong.
 *
 * Registering, the lobby prefetch and the key release are entrant actions and
 * belong to the student app. Nothing here touches them.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { isPlatformAdmin, loadPrincipal } from "../../api/principal";

const TIEBREAKS = [
  { value: "raw_score_desc", label: "Highest score" },
  { value: "duration_asc", label: "then fastest" },
  { value: "submitted_at_asc", label: "then submitted first" },
] as const;

export function Competitions() {
  const queries = useQueryClient();
  const [title, setTitle] = useState("");
  const [versionXid, setVersionXid] = useState("");
  const [startsAt, setStartsAt] = useState("");
  const [minutes, setMinutes] = useState("60");
  const [visibility, setVisibility] = useState<"org" | "invite" | "public">("org");
  const [maxParticipants, setMaxParticipants] = useState("");
  const [opened, setOpened] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const principal = useQuery({
    queryKey: ["principal"],
    queryFn: loadPrincipal,
    staleTime: Infinity,
  });
  const admin = isPlatformAdmin(principal.data ?? null);

  const contests = useQuery({
    queryKey: ["competitions"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/competitions");
      if (failure) throw failure;
      return data;
    },
  });

  const tests = useQuery({
    queryKey: ["tests"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/tests", {
        params: { query: { limit: 100 } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const board = useQuery({
    queryKey: ["leaderboard", opened],
    queryFn: async () => {
      const contest = contests.data?.find((c) => c.xid === opened);
      // A finished contest reads from `competition_results`, which is durable and
      // reproducible; a live one reads the Redis board and is provisional. Asking
      // the wrong one of a finished contest is how a board evaporates on a
      // restart.
      const path = contest?.status === "final" || contest?.status === "grading"
        ? "/competitions/{xid}/results" as const
        : "/competitions/{xid}/leaderboard" as const;
      const { data, error: failure } = await api.GET(path, {
        params: { path: { xid: opened! } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: Boolean(opened),
    // Live boards move. Finished ones do not, and polling one would be asking a
    // durable table the same question for ever.
    refetchInterval: (query) => (query.state.data?.is_provisional ? 10_000 : false),
  });

  const create = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/competitions", {
        body: {
          title: title.trim(),
          test_version_xid: versionXid,
          starts_at: new Date(startsAt).toISOString(),
          duration_seconds: Number(minutes) * 60,
          visibility,
          // `lobby_opens_at` is left to the server, which defaults it to
          // `starts_at - 120s`. That window is what turns a synchronized burst of
          // 200 payload fetches into a trickle, and it is not a number an
          // organiser should be inventing.
          ...(maxParticipants ? { max_participants: Number(maxParticipants) } : {}),
          tiebreak: TIEBREAKS.map((t) => t.value),
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      setTitle("");
      setVersionXid("");
      void queries.invalidateQueries({ queryKey: ["competitions"] });
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const assignable = (tests.data?.items ?? []).filter(
    (t) => t.current_published_version_xid,
  );

  return (
    <div className="page">
      <h1>Competitions</h1>
      {error && (
        <>
          <p className="error">{error}</p>
          <p className="muted">
            A refused paper is one of two things and they have different
            remedies: another contest already used it, or its questions have
            circulated far enough that a good share of any field will have met
            them. Both refuse outright — a contest ranks people against each
            other, so there is no threshold of unfairness worth running.
          </p>
        </>
      )}

      <h2>Schedule one</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          create.mutate();
        }}
      >
        <label htmlFor="k-title">Title</label>
        <input
          id="k-title"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Winter Open"
          required
        />

        <label htmlFor="k-test">Paper</label>
        <select
          id="k-test"
          value={versionXid}
          onChange={(event) => setVersionXid(event.target.value)}
          required
        >
          <option value="">— choose a published test —</option>
          {assignable.map((test) => (
            <option key={test.xid} value={test.current_published_version_xid!}>
              {test.title}
            </option>
          ))}
        </select>
        <p className="muted">
          It must be a paper nobody has contested and whose questions have not
          circulated. Composing a fresh version costs an afternoon; reusing a
          spent one costs the contest.
        </p>

        <div className="row">
          <span>
            <label htmlFor="k-start">Starts</label>
            <input
              id="k-start"
              type="datetime-local"
              value={startsAt}
              onChange={(event) => setStartsAt(event.target.value)}
              required
            />
          </span>
          <span>
            <label htmlFor="k-mins">Minutes</label>
            <input
              id="k-mins"
              inputMode="numeric"
              value={minutes}
              onChange={(event) => setMinutes(event.target.value)}
            />
          </span>
        </div>
        <p className="muted">
          The lobby opens two minutes before the start — the server sets it, and
          it is what turns two hundred simultaneous paper downloads into a
          trickle.
        </p>

        <label htmlFor="k-vis">Who can enter</label>
        <select
          id="k-vis"
          value={visibility}
          onChange={(event) =>
            setVisibility(event.target.value as "org" | "invite" | "public")
          }
        >
          <option value="org">This centre</option>
          <option value="invite">Invited entrants only</option>
          {/* Platform-admin only, and offered only to one. A centre admin
              choosing this gets `admin_only`, which is a control that can never
              work rather than one they have not earned yet. */}
          {admin && <option value="public">Everybody on the platform</option>}
        </select>

        <label htmlFor="k-max">Maximum entrants (optional)</label>
        <input
          id="k-max"
          inputMode="numeric"
          value={maxParticipants}
          onChange={(event) => setMaxParticipants(event.target.value)}
        />

        <p className="muted">
          Ranked by highest score, then fastest, then whoever submitted first.
        </p>

        <button disabled={create.isPending || !versionXid || !startsAt}>
          {create.isPending ? "Scheduling…" : "Schedule"}
        </button>
      </form>

      <h2>Contests</h2>
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th>Title</th><th>Status</th><th>Starts</th>
              <th>Entrants</th><th>Visibility</th><th />
            </tr>
          </thead>
          <tbody>
            {contests.data?.map((contest) => (
              <tr key={contest.xid}>
                <td>{contest.title}</td>
                <td>{contest.status}</td>
                <td className="muted">
                  {new Date(contest.starts_at).toLocaleString()}
                </td>
                <td className="muted">
                  {contest.registered_count ?? 0}
                  {contest.max_participants ? ` / ${contest.max_participants}` : ""}
                </td>
                <td className="muted">{contest.visibility}</td>
                <td>
                  <button
                    className="link"
                    onClick={() => setOpened(opened === contest.xid ? null : contest.xid)}
                  >
                    {opened === contest.xid ? "Hide" : "Board"}
                  </button>
                </td>
              </tr>
            ))}
            {contests.data?.length === 0 && (
              <tr><td colSpan={6} className="muted">No contests yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {opened && board.data && (
        <div className="issued">
          <h2>
            {board.data.is_provisional ? "Live standings" : "Final results"}
          </h2>
          {board.data.is_provisional && (
            <p className="muted">
              Provisional while the contest runs, refreshing every ten seconds.
              The final board is written durably when it ends.
            </p>
          )}
          <div className="scroll">
            <table>
              <thead>
                <tr><th>#</th><th>Entrant</th><th>Score</th><th>Band</th><th>Time</th></tr>
              </thead>
              <tbody>
                {board.data.entries?.map((entry) => (
                  <tr key={entry.user?.xid ?? entry.rank}>
                    <td className="num">{entry.rank}</td>
                    {/* Display name only. A leaderboard is the most-screenshotted
                        surface in the product and must never carry an age, a phone
                        number or a centre name — the API does not send them and
                        this does not ask. */}
                    <td>{entry.user?.display_name}</td>
                    <td>{entry.raw_score}</td>
                    <td>{entry.band ?? "—"}</td>
                    <td className="muted">
                      {entry.duration_ms
                        ? `${Math.floor(entry.duration_ms / 60000)}m ${
                            Math.floor((entry.duration_ms % 60000) / 1000)}s`
                        : "—"}
                    </td>
                  </tr>
                ))}
                {board.data.entries?.length === 0 && (
                  <tr><td colSpan={5} className="muted">Nobody has finished yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>
          {!board.data.is_provisional && (
            <p className="muted">
              This board does not change on its own. If a key fix would move it, a
              platform admin has to decide that deliberately — a published ranking
              that re-ranks silently is worse than one that was wrong.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
