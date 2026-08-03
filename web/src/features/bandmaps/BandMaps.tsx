/**
 * The curve that turns a raw mark into the band a student is told they got.
 *
 * The highest-stakes create screen in this console, for a reason that has
 * nothing to do with how much code it is: a band map is applied to a whole
 * cohort at once, and a mistake in it is not visible as a mistake. It comes out
 * as a class of plausible-looking bands that are wrong.
 *
 * **The server validates the request model's types and nothing else.** Measured
 * against the running API, `POST /band-maps` answered 201 to an empty mapping, a
 * mapping covering only marks 10-20 of a 40-mark paper, rows keyed
 * `{lo, hi, b}`, two rows claiming the same marks with different bands, and
 * `max_raw: -5`. Every one of those is caught here instead, by
 * `bandMapTable.tableProblems`, which applies the rules the rest of the system
 * already depends on:
 *
 *   - the publish gate refuses a test whose map has a gap (`BAND_MAP_GAP`) or is
 *     shorter than the paper (`BAND_MAP_TOO_SHORT`), so a gapped map is a test
 *     that cannot be published, discovered a week later by somebody else;
 *   - the scorer reads `row["raw_min"]`, so a row with the wrong keys is an
 *     error inside scoring — the student's submission fails, rather than their
 *     band being missing;
 *   - the scorer returns the FIRST row that matches, so overlapping rows are not
 *     an error anywhere: they are a band chosen by row order.
 *
 * **Starting from an existing curve is the point of the prefill.** A complete
 * IELTS table is fifteen rows, and fifteen rows typed from memory is where the
 * gap comes from. The platform defaults are the standard conversions; a centre
 * that needs its own is usually adjusting a boundary or two, not inventing a
 * scale.
 *
 * **A map is a version, and versions are never rewritten.** Every score records
 * which band-map version produced it, so retuning the curve later is a regrade
 * with an audit trail rather than a silent reinterpretation of results students
 * have already been given.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import "./bandmaps.css";
import {
  formatRows,
  mappingRows,
  parseRows,
  tableProblems,
  uncovered,
} from "./bandMapTable";

const SKILLS = ["reading", "listening"] as const;
const VARIANTS = ["academic", "general_training"] as const;

export function BandMaps() {
  const queries = useQueryClient();
  const [name, setName] = useState("");
  const [skill, setSkill] = useState<(typeof SKILLS)[number]>("reading");
  const [variant, setVariant] = useState<(typeof VARIANTS)[number]>("academic");
  const [maxRaw, setMaxRaw] = useState(40);
  const [table, setTable] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const maps = useQuery({
    queryKey: ["band-maps"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/band-maps");
      if (failure) throw failure;
      return data;
    },
  });

  const session = useQuery({
    queryKey: ["auth-session"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/auth/session");
      if (failure) throw failure;
      return data;
    },
    staleTime: Infinity,
  });

  // The endpoint files the new map against `actor.org_ids[0]`, and an account
  // with no organization gets `org_id = NULL` — which is what a PLATFORM DEFAULT
  // is. Measured: a platform admin's map came back `is_platform_default: false`
  // from the create call and `true` from the listing one line later, and an
  // unrelated centre saw it in its own list immediately.
  const noOrg = !(session.data?.memberships ?? []).some((m) => m.status === "active");

  const parsed = parseRows(table);
  const problems = [...parsed.errors, ...tableProblems(parsed.rows, maxRaw)];
  const missing = uncovered(parsed.rows, Math.max(0, maxRaw));

  const create = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST("/band-maps", {
        body: {
          name: name.trim(), skill, variant, max_raw: maxRaw,
          mapping: mappingRows(parsed.rows),
        },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: () => {
      setError(null);
      setName("");
      setTable("");
      setConfirmed(false);
      // The listing is the truth about scope: the create response reports
      // `is_platform_default: false` unconditionally, including for the map it
      // just filed as a platform default.
      void queries.invalidateQueries({ queryKey: ["band-maps"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const blocked = problems.length > 0 || !name.trim() || (noOrg && !confirmed);

  return (
    <div className="page">
      <h1>Band maps</h1>
      <p className="muted">
        The raw-mark-to-band table a paper is marked against. Your centre only
        needs its own if it marks differently from the standard conversions, and
        creating one is asserting that it does.
      </p>

      <h2>Existing curves</h2>
      {maps.isError && <p className="error">{problemText(maps.error)}</p>}
      <table>
        <thead>
          <tr><th>Name</th><th>Skill</th><th>Variant</th><th>Scope</th><th>Max</th><th /></tr>
        </thead>
        <tbody>
          {maps.data?.map((map) => (
            <tr key={map.xid}>
              <td>{map.name}</td>
              <td className="muted">{map.skill}</td>
              <td className="muted">{map.variant}</td>
              <td className="muted">
                {map.is_platform_default ? "Every centre" : "Your centre"}
              </td>
              <td className="num">{map.current_version?.max_raw ?? "—"}</td>
              <td>
                {map.current_version?.mapping && (
                  <button
                    className="link"
                    type="button"
                    onClick={() => {
                      // Copied in, not referenced. The new map is a new row with
                      // its own version; nothing here edits the curve a past
                      // score was produced with.
                      setTable(formatRows(map.current_version!.mapping!));
                      setMaxRaw(map.current_version!.max_raw ?? 40);
                      if (map.skill) setSkill(map.skill);
                      if (map.variant) setVariant(map.variant);
                    }}
                  >
                    Start from this
                  </button>
                )}
              </td>
            </tr>
          ))}
          {maps.data?.length === 0 && (
            <tr><td colSpan={6} className="muted">No band maps.</td></tr>
          )}
        </tbody>
      </table>

      <h2>New band map</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          if (!blocked) create.mutate();
        }}
      >
        <label htmlFor="bm-name">Name</label>
        <input
          id="bm-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Tashkent Prep reading, 2026 intake"
          required
        />

        <div className="row">
          <span>
            <label htmlFor="bm-skill">Skill</label>
            <select
              id="bm-skill"
              value={skill}
              onChange={(event) =>
                setSkill(event.target.value as (typeof SKILLS)[number])}
            >
              {SKILLS.map((option) => (
                <option key={option} value={option}>{option}</option>
              ))}
            </select>
          </span>
          <span>
            <label htmlFor="bm-variant">Variant</label>
            <select
              id="bm-variant"
              value={variant}
              onChange={(event) =>
                setVariant(event.target.value as (typeof VARIANTS)[number])}
            >
              {VARIANTS.map((option) => (
                <option key={option} value={option}>{option}</option>
              ))}
            </select>
          </span>
          <span>
            <label htmlFor="bm-max">Marks in the paper</label>
            <input
              id="bm-max"
              type="number"
              min={1}
              max={200}
              value={maxRaw}
              onChange={(event) => setMaxRaw(Number(event.target.value))}
            />
          </span>
        </div>
        <p className="muted">
          Skill and variant are fixed lists in the database. A value outside them
          is rejected by the column, not by the request, so it comes back as an
          internal error rather than a message you can act on.
        </p>

        <label htmlFor="bm-table">
          The curve — one line per band, as <code>30-32 7.0</code>
        </label>
        <textarea
          id="bm-table"
          value={table}
          onChange={(event) => setTable(event.target.value)}
          rows={16}
          placeholder={"0-3 2.0\n4-5 2.5\n6-7 3.0"}
          required
        />
        <p className="muted">
          Every mark from 0 to {Math.max(0, maxRaw)} needs a band, and no mark may
          appear twice. A mark with no band scores as a raw count with no grade,
          and the publish gate refuses any test that uses the map.
        </p>

        <Coverage rows={parsed.rows.length} maxRaw={maxRaw} missing={missing} />

        {problems.length > 0 && table.trim() !== "" && (
          <p className="error">{problems.join("\n")}</p>
        )}

        {noOrg && (
          <label className="choice">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            <span>
              Your account belongs to no centre, so this map is saved as a{" "}
              <strong>platform default</strong> and every centre on the platform
              can mark against it. Tick to confirm that is what you intend.
            </span>
          </label>
        )}

        <button disabled={create.isPending || blocked}>
          {create.isPending ? "Saving…" : "Create band map"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}

      <p className="muted">
        A band map is published as version 1 and never rewritten. Every score
        records the version that produced it, so changing a curve later means a
        new map and a regrade, and a student's past result keeps meaning what they
        were told it meant.
      </p>
    </div>
  );
}

/**
 * Which marks have a band and which do not.
 *
 * A strip rather than a sentence because coverage is positional: "no band for 0,
 * 1, 2, 3" and "no band for 18" are the same sentence and completely different
 * mistakes — one is a table that starts too high, the other is a boundary typed
 * wrong in the middle.
 */
function Coverage({ rows, maxRaw, missing }: {
  rows: number; maxRaw: number; missing: number[];
}) {
  if (rows === 0 || !Number.isInteger(maxRaw) || maxRaw < 1) return null;
  const marks = Array.from({ length: maxRaw + 1 }, (_, mark) => mark);
  const gaps = new Set(missing);
  return (
    <div className="coverage">
      <p className="muted">
        {rows} row{rows === 1 ? "" : "s"} ·{" "}
        {missing.length === 0
          ? `every mark from 0 to ${maxRaw} has a band`
          : `${missing.length} mark${missing.length === 1 ? "" : "s"} with no band`}
      </p>
      <ol className="marks">
        {marks.map((mark) => (
          <li
            key={mark}
            className={gaps.has(mark) ? "mark gap" : "mark"}
            title={gaps.has(mark) ? `${mark}: no band` : `${mark}: covered`}
          >
            <span className="visually-hidden">{mark}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
