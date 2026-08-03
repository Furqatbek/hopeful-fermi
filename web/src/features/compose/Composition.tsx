/**
 * Composing a draft version: sections, and the question groups inside them.
 *
 * The brief called the authoring system the core of the product and said not to
 * treat it as CRUD over a questions table. This is the screen where that is
 * either true or not. Three things it must get right:
 *
 * **Nothing is copied.** A section references a passage version or an audio
 * track by xid; a group placement references a question group version. Reuse is
 * by reference, which is what makes a bank worth having.
 *
 * **Published versions are immutable.** Every mutating control here is disabled
 * unless `status === "draft"`. The server refuses anyway — three endpoints
 * enforce it — but a UI that offers a button the server will refuse teaches the
 * teacher that this product is unreliable.
 *
 * **Ordering must never leave a hole.** See `ordering.ts`; a hole is a student
 * who cannot enter the next section of a timed exam.
 */

import {
  DndContext,
  type DragEndEvent,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, problemText } from "../../api/client";
import type { components } from "../../api/schema";
import { AddSection } from "./AddSection";
import { AttachGroup } from "./AttachGroup";
import { isContiguous, reorder, sectionMoves } from "./ordering";

type Section = components["schemas"]["Section"];
type Placement = components["schemas"]["GroupPlacement"];

/** `instructions` is an open map keyed by locale, so its values are `unknown`
 *  to the type system and genuinely could be anything. Take the first string
 *  there is; a group with none is "Untitled", not a crash. */
function instructionText(placement: Placement): string {
  const values = Object.values(placement.group_version?.instructions ?? {});
  const first = values.find((value): value is string => typeof value === "string");
  return first ?? "Untitled group";
}

function SortableRow({ id, children }: { id: string; children: React.ReactNode }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id });
  return (
    <li
      ref={setNodeRef}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
        opacity: isDragging ? 0.5 : 1,
      }}
      className="sortable"
    >
      {/* The handle carries the listeners, not the row: a row-wide drag makes
          text unselectable, and an author retyping a question number because
          they cannot select it is a worse trade than a visible grip. Keyboard
          sensors are wired so this is reachable without a mouse. */}
      <button className="grip" {...attributes} {...listeners} aria-label="Reorder">
        ⠿
      </button>
      {children}
    </li>
  );
}

export function Composition() {
  const { xid = "" } = useParams();
  const queries = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [reviewNotes, setReviewNotes] = useState("");
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  // `PATCH /test-versions/{xid}` requires `If-Match`, so the ETag from the read
  // has to survive until the write. Held in a ref rather than in the cached data
  // because it belongs to the RESPONSE, not to the version: putting it in the
  // query data would make it look like a field of the thing, and a stale one
  // would then be serialised around as though it described the version itself.
  const etag = useRef<string | null>(null);

  const version = useQuery({
    queryKey: ["test-version", xid],
    queryFn: async () => {
      const { data, error: failure, response } = await api.GET("/test-versions/{xid}", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
      etag.current = response.headers.get("ETag");
      return data;
    },
    enabled: Boolean(xid),
  });

  const draft = version.data?.status === "draft";
  const sections: Section[] = version.data?.sections ?? [];
  const review = version.data?.review;
  /**
   * Approved content is still `in_review` — approval is not a deploy — and every
   * control here used to be gated on `draft`, so submitting a version made the
   * PUBLISH button vanish along with the editing ones. A centre that turned on
   * `require_review` could submit a version and never publish it again: the
   * approve control did not exist, and the publish control disappeared at the
   * moment it became relevant.
   */
  const approved = review?.state === "approved" && !review?.is_stale;
  const publishable = draft || (version.data?.status === "in_review" && approved);

  const refresh = () => queries.invalidateQueries({ queryKey: ["test-version", xid] });

  // The raw→band curve this version is marked against. Without one the publish
  // gate refuses with BAND_MAP_MISSING, and nothing in the console set it — so
  // every test authored here stopped one step short of publishable, with a
  // finding naming a thing the author had no control offering.
  const bandMaps = useQuery({
    queryKey: ["band-maps"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/band-maps");
      if (failure) throw failure;
      return data;
    },
    enabled: draft,
  });

  const setBandMap = useMutation({
    mutationFn: async (bandMapVersionXid: string) => {
      if (!etag.current) throw new Error("Reload the page and try again.");
      const { error: failure } = await api.PATCH("/test-versions/{xid}", {
        params: {
          path: { xid: xid! },
          // Optimistic concurrency: if another author changed this version since
          // it was read, the server refuses rather than letting the later write
          // silently win.
          header: { "If-Match": etag.current },
        },
        body: { band_map_version_xid: bandMapVersionXid },
      });
      if (failure) throw failure;
    },
    onSuccess: refresh,
    onError: (failure) => setError(problemText(failure)),
  });

  const moveGroups = useMutation({
    mutationFn: async (args: { sectionXid: string; placementXids: string[] }) => {
      const { error: failure } = await api.POST("/sections/{xid}/reorder", {
        params: { path: { xid: args.sectionXid } },
        body: { group_placement_xids: args.placementXids },
      });
      if (failure) throw failure;
    },
    onSuccess: refresh,
    onError: (failure) => setError(problemText(failure)),
  });

  const moveSections = useMutation({
    mutationFn: async (desired: Section[]) => {
      // One PATCH per section that actually moves. `PATCH /sections/{xid}` takes
      // a full SectionCreate, so every field has to be resent — omitting `skill`
      // or `title` would be read as a change to them.
      for (const move of sectionMoves(desired.map((s) => ({ xid: s.xid!, position: s.position })))) {
        const section = desired.find((s) => s.xid === move.xid)!;
        const { error: failure } = await api.PATCH("/sections/{xid}", {
          params: { path: { xid: move.xid } },
          body: {
            skill: section.skill as "reading" | "listening",
            title: section.title!,
            position: move.position,
            ...(section.time_limit_seconds != null
              ? { time_limit_seconds: section.time_limit_seconds }
              : {}),
            ...(section.play_once != null ? { play_once: section.play_once } : {}),
          },
        });
        if (failure) throw failure;
      }
    },
    onSuccess: refresh,
    onError: (failure) => setError(problemText(failure)),
  });

  const validate = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST("/test-versions/{xid}/validate", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
      return data;
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const publish = useMutation({
    mutationFn: async () => {
      const { data, error: failure } = await api.POST("/test-versions/{xid}/publish", {
        params: { path: { xid } },
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: refresh,
    // The publish gate returns EVERY finding, and `problemText` renders them
    // all. A publish refused for six reasons that reports one is six round trips.
    onError: (failure) => setError(problemText(failure)),
  });

  const submitReview = useMutation({
    mutationFn: async () => {
      const { error: failure } = await api.POST("/test-versions/{xid}/submit-review", {
        params: { path: { xid } },
        body: { ...(reviewNotes.trim() ? { notes: reviewNotes.trim() } : {}) },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setReviewNotes("");
      refresh();
    },
    onError: (failure) => setError(problemText(failure)),
  });

  const decide = useMutation({
    mutationFn: async (decision: "approved" | "changes_requested") => {
      // `notes` matters far more on a rejection than on an approval: the author
      // is about to fix something and the verdict alone does not say what.
      if (decision === "changes_requested" && !reviewNotes.trim()) {
        throw new Error("Say what needs changing — a rejection with no reason "
                        + "sends the author back to guess.");
      }
      const { error: failure } = await api.POST("/test-versions/{xid}/review", {
        params: { path: { xid } },
        body: {
          decision,
          ...(reviewNotes.trim() ? { notes: reviewNotes.trim() } : {}),
        },
      });
      if (failure) throw failure;
    },
    onSuccess: () => {
      setError(null);
      setReviewNotes("");
      refresh();
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  function onSectionDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const from = sections.findIndex((s) => s.xid === active.id);
    const to = sections.findIndex((s) => s.xid === over.id);
    if (from < 0 || to < 0) return;
    setError(null);
    moveSections.mutate(reorder(sections, from, to));
  }

  function onGroupDragEnd(section: Section, event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const groups: Placement[] = section.groups ?? [];
    const from = groups.findIndex((g) => g.xid === active.id);
    const to = groups.findIndex((g) => g.xid === over.id);
    if (from < 0 || to < 0) return;
    setError(null);
    moveGroups.mutate({
      sectionXid: section.xid!,
      placementXids: reorder(groups, from, to).map((g) => g.xid!),
    });
  }

  if (version.isPending) return <div className="page muted">Loading…</div>;
  if (version.isError)
    return <div className="page error">{problemText(version.error)}</div>;

  const holes = !isContiguous(sections.map((s) => s.position));

  return (
    <div className="page">
      <h1>{version.data?.title ?? "Composition"}</h1>
      <p className="muted">
        v{version.data?.version_no} · {version.data?.status} ·{" "}
        {version.data?.total_questions ?? 0} questions
      </p>

      {!draft && (
        <p className="muted">
          {/* Not an error: an archived or published version is a legitimate
              thing to look at. It just cannot be edited, and saying so once is
              better than disabled controls with no explanation. */}
          This version is {version.data?.status} and cannot be edited. Start a new
          draft from the test page to make changes.
        </p>
      )}

      {draft && (
        <>
          <label htmlFor="c-bandmap">Band map</label>
          <select
            id="c-bandmap"
            value={version.data?.band_map_version_xid ?? ""}
            onChange={(event) => setBandMap.mutate(event.target.value)}
            disabled={setBandMap.isPending}
          >
            <option value="">— choose a band map —</option>
            {bandMaps.data
              ?.filter((map) => map.current_version?.xid)
              .map((map) => (
                <option key={map.xid} value={map.current_version!.xid}>
                  {map.name} ({map.skill}
                  {map.is_platform_default ? ", platform default" : ""}) · max{" "}
                  {map.current_version!.max_raw}
                </option>
              ))}
          </select>
          <p className="muted">
            The raw-score-to-band curve this paper is marked against. Required
            before publishing — a paper with no curve produces a raw score and no
            band, which is not a result a student can read. The platform defaults
            are the standard IELTS conversions; a centre only needs its own if it
            marks differently, and then it is asserting that it does.
          </p>
        </>
      )}

      {holes && (
        <p className="error">
          Section positions are not contiguous. A gap here is a student who cannot
          enter the next section — reorder to repair it before publishing.
        </p>
      )}
      {error && <p className="error">{error}</p>}

      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={onSectionDragEnd}
      >
        <SortableContext
          items={sections.map((s) => s.xid!)}
          strategy={verticalListSortingStrategy}
        >
          <ol className="tree">
            {sections.map((section) => (
              <SortableRow key={section.xid} id={section.xid!}>
                <div className="section">
                  <div className="section-head">
                    <strong>{section.title}</strong>
                    <span className="muted">
                      {section.skill}
                      {section.time_limit_seconds
                        ? ` · ${Math.round(section.time_limit_seconds / 60)} min`
                        : ""}
                      {section.audio_track ? ` · ${section.audio_track.title}` : ""}
                      {section.passage_version ? " · passage" : ""}
                      {/* Play-once is an exam-integrity rule, not a preference —
                          worth showing where a teacher composes rather than
                          buried in a settings pane. */}
                      {section.skill === "listening"
                        ? section.play_once
                          ? " · play once"
                          : " · replayable"
                        : ""}
                    </span>
                  </div>

                  <DndContext
                    sensors={sensors}
                    collisionDetection={closestCenter}
                    onDragEnd={(event) => onGroupDragEnd(section, event)}
                  >
                    <SortableContext
                      items={(section.groups ?? []).map((g) => g.xid!)}
                      strategy={verticalListSortingStrategy}
                    >
                      <ol className="groups">
                        {(section.groups ?? []).map((placement) => (
                          <SortableRow key={placement.xid} id={placement.xid!}>
                            <span>
                              {/* Test-wide IELTS numbering, computed server-side
                                  at composition time. Showing it is how an author
                                  sees that inserting a group renumbered the rest. */}
                              <span className="num">
                                Q{placement.number_start ?? "?"}
                              </span>{" "}
                              {instructionText(placement)}
                            </span>
                          </SortableRow>
                        ))}
                        {(section.groups ?? []).length === 0 && (
                          <li className="muted">No question groups yet.</li>
                        )}
                      </ol>
                    </SortableContext>
                  </DndContext>
                  {draft && (
                    <AttachGroup
                      sectionXid={section.xid!}
                      versionXid={xid}
                      nextPosition={(section.groups ?? []).length + 1}
                    />
                  )}
                </div>
              </SortableRow>
            ))}
            {sections.length === 0 && (
              <li className="muted">No sections yet.</li>
            )}
          </ol>
        </SortableContext>
      </DndContext>

      {draft && <AddSection versionXid={xid} nextPosition={sections.length + 1} />}

      {review && (review.state || review.required) && (
        <div className="issued">
          <h2>Review</h2>

          {review.state === "requested" && (
            <p>
              Submitted for review by{" "}
              <strong>
                {review.requested_by?.given_name} {review.requested_by?.family_name}
              </strong>
              {review.requested_at && (
                <span className="muted">
                  {" "}on {new Date(review.requested_at).toLocaleString()}
                </span>
              )}
              .
            </p>
          )}

          {review.state === "approved" && (
            <p>
              Approved by{" "}
              <strong>
                {review.reviewer?.given_name} {review.reviewer?.family_name}
              </strong>
              {review.decided_at && (
                <span className="muted">
                  {" "}on {new Date(review.decided_at).toLocaleString()}
                </span>
              )}
              .
            </p>
          )}

          {review.state === "changes_requested" && (
            <>
              <p className="error">
                Changes requested by {review.reviewer?.given_name}{" "}
                {review.reviewer?.family_name}.
              </p>
              {/* The verdict alone is useless to the person who has to act on
                  it, so the reason is given the same weight as the decision. */}
              {review.notes && <blockquote>{review.notes}</blockquote>}
              <p className="muted">
                Back to draft — fix it and submit again.
              </p>
            </>
          )}

          {review.is_stale && (
            <p className="error">
              This version has been <strong>edited since it was approved</strong>,
              so the approval no longer covers what is here and publishing will be
              refused. Submit it for review again. A version stays editable while
              in review, which is exactly how "approve, then change the answer
              key, then publish" would otherwise be a sequence one person could
              run on their own.
            </p>
          )}

          {review.state === null && review.required && (
            <p className="muted">
              This centre requires a second pair of eyes before publishing. Submit
              this version when it is ready, and somebody other than you approves
              it.
            </p>
          )}

          {review.can_decide ? (
            <>
              <label htmlFor="c-notes">Notes</label>
              <textarea
                id="c-notes"
                rows={2}
                value={reviewNotes}
                onChange={(event) => setReviewNotes(event.target.value)}
                placeholder="Question 4's key accepts a misspelling"
              />
              <div className="row">
                <button
                  onClick={() => {
                    setError(null);
                    decide.mutate("approved");
                  }}
                  disabled={decide.isPending}
                >
                  {decide.isPending ? "Recording…" : "Approve"}
                </button>
                <button
                  className="link"
                  onClick={() => {
                    setError(null);
                    decide.mutate("changes_requested");
                  }}
                  disabled={decide.isPending}
                >
                  Request changes
                </button>
              </div>
              <p className="muted">
                Approving does not publish. It records that you have read this
                content — the fingerprint of exactly this content — and somebody
                still has to publish it deliberately.
              </p>
            </>
          ) : (
            review.state === "requested" && (
              /* Not a disabled button. The three reasons someone cannot decide —
                 no publish authority, they wrote it, they submitted it — are
                 resolved server-side into `can_decide`, and a greyed-out control
                 invites a support call asking why. */
              <p className="muted">
                Waiting for somebody else to approve it. You cannot approve work
                you wrote or submitted, and approving needs the authority to
                publish.
              </p>
            )
          )}
        </div>
      )}

      <div className="row">
        <button onClick={() => validate.mutate()} disabled={validate.isPending}>
          {validate.isPending ? "Checking…" : "Run the publish gate"}
        </button>
        {publishable && (
          <button
            onClick={() => {
              setError(null);
              publish.mutate();
            }}
            /* Enabled only after the gate has been RUN and passed in this
               session. The server runs it again regardless — this is not the
               control — but offering Publish on an unchecked draft invites a
               422 listing six problems, which reads as the product breaking
               rather than as the gate doing its job. */
            disabled={publish.isPending || !validate.data?.passed}
            title={
              validate.data?.passed
                ? "Publish this version"
                : "Run the publish gate first"
            }
          >
            {publish.isPending ? "Publishing…" : "Publish"}
          </button>
        )}
        {draft && (
          <Link className="link" to={`/versions/${xid}/preview`}>
            Preview
          </Link>
        )}
        {(draft || review?.is_stale) && (
          <button
            className="link"
            onClick={() => {
              setError(null);
              submitReview.mutate();
            }}
            disabled={submitReview.isPending}
            /* Only meaningful when the centre has `require_review` on. It is
               off by default — most centres here are one or two people, and a
               universal review bar plus the no-self-approval rule is a
               one-teacher centre that cannot publish at all. */
            title="For centres that require a second pair of eyes before publishing"
          >
            Submit for review
          </button>
        )}
      </div>

      {validate.data && (
        <div className="report">
          <p className={validate.data.passed ? "muted" : "error"}>
            {validate.data.passed
              ? "Passes. This version can be published."
              : `${validate.data.error_count} error(s), ${validate.data.warning_count} warning(s).`}
          </p>
          {/* EVERY finding, never just the first. The server goes out of its way
              to return them all — an author fixing one error per round trip
              across six round trips is the failure that exists to prevent. */}
          <ul>
            {validate.data.findings?.map((finding, index) => (
              <li key={index} className={finding.severity === "error" ? "error" : "muted"}>
                <code>{finding.path ?? finding.code}</code> — {finding.message}
                {finding.fix_hint && <em> {finding.fix_hint}</em>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
