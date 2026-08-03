/**
 * The question-type registry, with a screen instead of a curl command.
 *
 * This is the architectural bet of the product: a new question type is added to
 * a RUNNING system with no migration and no redeploy. `TypeForm.tsx` is the
 * other half — a type registered here gets a teacher-facing form without a
 * frontend deploy — and the two have to agree about what a definition is, which
 * is why the browser below shows `authoring.form` next to the schemas.
 *
 * **Validate, then register, and the second is unreachable without the first.**
 * `POST /admin/question-types/validate` exists precisely so a bad definition is
 * caught before it is live in production, and a dry run nobody uses is a dry run
 * that does not exist. So the register button is gated on a passing validation
 * OF THE DEFINITION CURRENTLY ON SCREEN: edit one character afterwards and the
 * gate closes again, because a validation is a statement about one exact
 * document and not about the textarea in general.
 *
 * **Every finding, never the first.** The server returns them all at once —
 * `Report` carries a list and `problemText` already refuses to flatten it — and
 * a UI that showed one at a time would put an author through six round trips to
 * learn six things the server said in one.
 *
 * **A textarea, not a builder.** A definition is JSON with four embedded JSON
 * Schemas in it. A visual editor for that is a project of its own; a textarea
 * with the server's findings under it is honest and can be shipped today.
 * "Copy into the editor" is the path that makes it workable: the server's own
 * fix hint says to check a definition against an existing type, so the existing
 * types are one click from the box you are typing in.
 *
 * **Registering is not an undoable local edit.** It changes what every author on
 * the platform can create, and the contract offers no way back: no delete, no
 * deactivate, no status change. A mistake is corrected by registering a NEW
 * version, and the old one stays — which is also what keeps a published test
 * scoring the way it was published.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, problemText } from "../../api/client";
import { isPlatformAdmin, loadPrincipal } from "../../api/principal";
import type { components } from "../../api/schema";
import { definitionRef, parseDefinition, PRIMITIVES, unknownPrimitive } from "./definition";
import "./registry.css";

/**
 * The body of both admin calls.
 *
 * A pasted document cannot be typed at compile time — the whole point of the
 * endpoint is that its shape is the registry's own schema, checked by the
 * server, which is why the handler takes `body: dict` rather than a Pydantic
 * model. The cast is where that fact enters the client, and it is one place.
 */
type DefinitionBody = components["schemas"]["QuestionTypeDef"];

export function QuestionTypes() {
  const queries = useQueryClient();
  const principal = useQuery({
    queryKey: ["principal"],
    queryFn: loadPrincipal,
    staleTime: Infinity,
  });
  const admin = isPlatformAdmin(principal.data ?? null);

  const [opened, setOpened] = useState<{ key: string; version: number } | null>(null);
  const [text, setText] = useState("");
  // The canonical form of the document the server last passed. Not a boolean:
  // "something validated" is not "this validated".
  const [cleared, setCleared] = useState<string | null>(null);
  const [understood, setUnderstood] = useState(false);
  const [registered, setRegistered] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // `include_deprecated`, unlike the authoring picker: this screen is the
  // registry itself, and a deprecated type is still what a test published last
  // year is scored against. Hiding it here would hide the definition somebody
  // came to read.
  const types = useQuery({
    queryKey: ["question-types", "registry"],
    queryFn: async () => {
      const { data, error: failure } = await api.GET("/question-types", {
        params: { query: { include_deprecated: true } },
      });
      if (failure) throw failure;
      return data;
    },
  });

  const definition = useQuery({
    queryKey: ["question-type", opened?.key, opened?.version],
    queryFn: async () => {
      if (!opened) throw new Error("No type chosen.");
      const { data, error: failure } = await api.GET("/question-types/{key}/{version}", {
        params: { path: { key: opened.key, version: opened.version } },
      });
      if (failure) throw failure;
      return data;
    },
    enabled: opened !== null,
  });

  const validate = useMutation({
    mutationFn: async () => {
      const parsed = parseDefinition(text);
      if (!parsed.ok) throw new Error(parsed.message);
      const { data, error: failure } = await api.POST("/admin/question-types/validate", {
        body: parsed.value as DefinitionBody,
      });
      if (failure) throw failure;
      return { report: data, canonical: parsed.canonical };
    },
    onSuccess: (result) => {
      setError(null);
      // Only a PASSING run opens the gate. A run that came back with errors is a
      // completed validation and still not permission to register.
      setCleared(result.report?.passed ? result.canonical : null);
      setUnderstood(false);
    },
    onError: (failure) => {
      setCleared(null);
      setError(problemText(failure) || String(failure));
    },
  });

  const register = useMutation({
    mutationFn: async () => {
      const parsed = parseDefinition(text);
      if (!parsed.ok) throw new Error(parsed.message);
      const { data, error: failure } = await api.POST("/admin/question-types", {
        body: parsed.value as DefinitionBody,
      });
      if (failure) throw failure;
      return data;
    },
    onSuccess: (data) => {
      setError(null);
      setRegistered(data ? `${data.key}@v${data.version}` : null);
      setCleared(null);
      setUnderstood(false);
      // Both listings: this screen's, and the authoring picker's, which is the
      // one that decides whether a teacher can create the type that was just
      // added.
      void queries.invalidateQueries({ queryKey: ["question-types"] });
    },
    onError: (failure) => setError(problemText(failure) || String(failure)),
  });

  const parsed = parseDefinition(text);
  const rogue = parsed.ok ? unknownPrimitive(parsed.value) : null;
  const ref = parsed.ok ? definitionRef(parsed.value) : null;
  const report = validate.data?.report;
  // Passed, and passed on THIS text. Reformatting is not an edit; changing a
  // value is.
  const current = parsed.ok && cleared !== null && cleared === parsed.canonical;

  return (
    <div className="page">
      <h1>Question types</h1>
      <p className="muted">
        Every question type on the platform, as data. Adding one needs no
        database migration and no release — but it does change what every author
        in the system can create.
      </p>

      {error && <p className="error">{error}</p>}
      {types.isError && <p className="error">{problemText(types.error)}</p>}

      <h2>Registered definitions</h2>
      <table>
        <thead>
          <tr><th>Type</th><th>Key</th><th>Version</th><th>Skills</th><th>Scoring</th><th /></tr>
        </thead>
        <tbody>
          {types.data?.map((type) => (
            <tr key={`${type.key}@${type.version}`}>
              <td>
                {type.title}
                {type.status !== "active" && (
                  <span className="muted"> · {type.status}</span>
                )}
              </td>
              <td className="muted"><code>{type.key}</code></td>
              <td className="num">v{type.version}</td>
              <td className="muted">{type.skills?.join(", ")}</td>
              <td className="muted"><code>{type.scoring?.primitive}</code></td>
              <td>
                <button
                  className="link"
                  onClick={() =>
                    setOpened(
                      opened?.key === type.key && opened.version === type.version
                        ? null
                        : { key: type.key, version: type.version },
                    )
                  }
                >
                  {opened?.key === type.key && opened.version === type.version
                    ? "Hide"
                    : "Open"}
                </button>
              </td>
            </tr>
          ))}
          {types.data?.length === 0 && (
            <tr><td colSpan={6} className="muted">No types registered.</td></tr>
          )}
        </tbody>
      </table>

      {definition.isError && <p className="error">{problemText(definition.error)}</p>}
      {definition.data && (
        <div className="issued definition">
          <h2>
            {definition.data.title}{" "}
            <span className="muted">
              {definition.data.key}@v{definition.data.version}
            </span>
          </h2>
          {definition.data.description && <p>{definition.data.description}</p>}
          <p className="muted">
            Scored by <code>{definition.data.scoring?.primitive}</code>
            {definition.data.scoring?.normalizers?.length
              ? ` · normalizers: ${definition.data.scoring.normalizers.join(", ")}`
              : " · no normalizers"}
          </p>
          {/* The three schemas, then how it is scored, then the UI hints.
              `authoring.form` is what `TypeForm.tsx` builds the teacher's
              editor from, so it is the part of a definition a reader most often
              came here to compare against a type that already works. */}
          <Block label="payload_schema" value={definition.data.payload_schema} />
          <Block label="key_schema" value={definition.data.key_schema} />
          <Block label="response_schema" value={definition.data.response_schema} />
          <Block label="scoring" value={definition.data.scoring} />
          <Block label="validation" value={definition.data.validation} />
          <Block label="authoring" value={definition.data.authoring} />
          {admin && (
            <button
              className="link"
              onClick={() => {
                setText(JSON.stringify(definition.data, null, 2));
                setCleared(null);
                setUnderstood(false);
                setRegistered(null);
                setError(null);
              }}
            >
              Copy into the editor
            </button>
          )}
        </div>
      )}

      <h2>Add a type</h2>
      {!admin ? (
        <p className="muted">
          You do not have access to this. Registering a question type is a
          platform admin action, because it changes what every centre on the
          platform can author. The definitions above are readable by everyone.
        </p>
      ) : (
        <>
          <p className="muted">
            Two steps, in this order. The check is a dry run — it writes nothing —
            and registering is only offered once the definition on screen has
            passed it. The quickest start is to open a type above, use{" "}
            <em>Copy into the editor</em>, and change <code>key</code> and{" "}
            <code>version</code>: a key and version that already exist are
            refused, because a definition is never edited in place.
          </p>

          <label htmlFor="def">Definition (JSON)</label>
          <textarea
            id="def"
            rows={16}
            className={text.trim() && !parsed.ok ? "invalid" : undefined}
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setRegistered(null);
            }}
            placeholder={
              'Open a type above and use "Copy into the editor" to start from one that works.'
            }
          />

          {text.trim() !== "" && !parsed.ok && (
            <p className="error">{parsed.message}</p>
          )}

          <p className="muted">
            <code>scoring.primitive</code> must be one of{" "}
            {PRIMITIVES.map((primitive, index) => (
              <span key={primitive}>
                {index > 0 && ", "}
                <code>{primitive}</code>
              </span>
            ))}
            . Everything the platform scores is built from those three. A genuinely
            new one is engine code and needs a release, so it cannot be added here.
          </p>
          {rogue !== null && (
            <p className="error">
              <code>{rogue}</code> is not one of the three scoring primitives, so
              this definition cannot be registered. Choose the primitive that
              matches how the answer is marked, or ask for the new one to be
              built and released.
            </p>
          )}

          <div className="row">
            <button
              type="button"
              onClick={() => {
                setError(null);
                validate.mutate();
              }}
              disabled={validate.isPending || !parsed.ok}
            >
              {validate.isPending ? "Checking…" : "Check this definition"}
            </button>
            {cleared !== null && !current && (
              <span className="muted">
                Edited since the last check — check it again before registering.
              </span>
            )}
          </div>

          {report && (
            <div className="report">
              <p className={report.passed ? "muted" : "error"}>
                {report.passed
                  ? "This definition is sound. Nothing has been registered yet."
                  : `${report.error_count} error(s), ${report.warning_count} warning(s). Nothing has been registered.`}
              </p>
              {/* The whole list, never `findings[0]`. That is the contract's
                  shape and what every other validator here returns, and this
                  endpoint is the exception worth knowing about:
                  `QuestionTypeDef.from_dict` raises on the FIRST missing field,
                  so a definition with three problems comes back with one.
                  Measured in `tests/integration/test_console_registry.py`.
                  Rendering the list costs nothing and is right the day that is
                  fixed. */}
              <ul>
                {report.findings?.map((finding, index) => (
                  <li
                    key={index}
                    className={finding.severity === "error" ? "error" : "muted"}
                  >
                    <code>{finding.code}</code> {finding.message}
                    {finding.path && <span className="muted"> · {finding.path}</span>}
                    {finding.fix_hint && <div className="muted">{finding.fix_hint}</div>}
                  </li>
                ))}
              </ul>
              {!report.passed && (
                <p className="muted">
                  The check stops at the first problem it finds, so there may be
                  more once this one is fixed. Run it again after each change.
                </p>
              )}
            </div>
          )}

          {current && (
            <>
              <p className="muted">
                Registering <strong>{ref ?? "this definition"}</strong> adds it to
                what authors across the platform can create. It cannot be removed,
                switched off or edited from here — the API has no control that
                does any of those. A mistake is put right by registering a new
                version; this one stays, so anything already written against it
                keeps scoring the way it was published.
              </p>
              <label className="choice">
                <input
                  type="checkbox"
                  checked={understood}
                  onChange={(event) => setUnderstood(event.target.checked)}
                />
                I understand this cannot be undone from this console
              </label>
            </>
          )}

          <button
            type="button"
            onClick={() => {
              setError(null);
              register.mutate();
            }}
            disabled={register.isPending || !current || !understood}
            title={
              current
                ? "Register this definition"
                : "Check the definition first — registering is not offered until it passes"
            }
          >
            {register.isPending ? "Registering…" : "Register this type"}
          </button>

          {registered && (
            <p className="muted">
              {/* This used to need a caveat. The registry each server loaded
                  at start-up was built from `registry/question_types/*.json`
                  and nothing read the table, so a type that existed only as a
                  row could not be scored after a restart — and with four
                  workers, only the one that answered the request had it before
                  that. The registry is now the files UNION the rows, re-read on
                  a generation stamp, so the claim this screen makes is true.
                  Verified in `tests/integration/test_registry_persistence.py`,
                  against a registry built the way a FRESH PROCESS builds one. */}
              Registered <strong>{registered}</strong>. It is live on every
              server within a few seconds and it survives a restart. Nothing
              takes it back: definitions are never edited in place, so a
              correction is a new version.
            </p>
          )}
        </>
      )}
    </div>
  );
}

/** One part of a definition, as the JSON it is. Long lines scroll inside the
 *  block rather than stretching the page. */
function Block({ label, value }: { label: string; value: unknown }) {
  if (value === undefined || value === null) return null;
  return (
    <>
      <h3>{label}</h3>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </>
  );
}
