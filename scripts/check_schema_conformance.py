#!/usr/bin/env python3
"""Does the code actually implement the fields the contract declares?

`check_api_coverage.py` proves every documented OPERATION is served. It never
looks inside the payloads, and that is where this project's most persistent class
of defect lives. Across eleven routers, five separate fields were declared in the
OpenAPI document and not implemented at all:

    band_map_version_xid       hardcoded None in tv_dto
    attempt_xid                hardcoded None in _result_dto — and `required`
    committed_test_version_xid hardcoded None in read_import
    assignment_xid             accepted on a request model, never read
    target_test_xid            accepted on the import form, never read

Every one shipped, passed review, and passed a 1400-test suite. None is subtle
once you look for it, and none is findable by reading a diff — the field is
*present* in the code, it just never carries a value. So look for it mechanically.

Five checks, in descending order of precision:

  1. A response field pinned to the literal `None` — or an empty `{}` / `[]`,
     which is the same constant one spelling over — and never assigned anything
     else. A field that is structurally always null is not implemented, whether
     or not the schema permits null.
  2. A request field — Pydantic model attribute, or `Form`/`Query` parameter —
     that no handler ever reads.
  3. A `required` response field whose name appears as a dict key nowhere in the
     API layer. Coarse (a name can coincide) but sound in the direction that
     matters: if it is emitted nowhere, it is emitted nowhere.
  4. A request field the contract declares as an `enum` whose Pydantic model
     accepts any string. Twelve of these shipped: the value reached the INSERT
     and the database CHECK turned it into a 500, where the contract promised a
     422. Accepted spellings are `Literal[...]`, a `StrEnum`, or the repo's own
     `Field(pattern="^(a|b|c)$")` idiom — with the alternatives EQUAL to the
     contract's set, so a pattern that drifts from the enum is reported too.
  5. A spec-side name with no handler counterpart: a query parameter nothing
     binds, or a request-body property no model declares. FastAPI drops both
     silently, so a generated client sends a filter and gets the unfiltered
     list. Checks 1-3 walk the handlers' AST and structurally cannot see this
     direction — a name that is on no model is never examined — so this one
     diffs the contract against the OpenAPI document the app GENERATES, where
     `Query(alias=...)` is already applied and every bound name is listed.

Findings are fixed, not silenced. `ALLOWED` exists for the genuinely-deliberate
cases and every entry carries its reason, because an allowlist without one is a
list somebody will append to during a bad afternoon. And an entry that no longer
suppresses anything is a FAILURE, the way the console gate treats a stale
exemption: four `next_cursor` allowances outlived the paging they excused by
several months, and nothing said so.

    python3 scripts/check_schema_conformance.py
    python3 scripts/check_schema_conformance.py --list   # report, exit 0
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
API = ROOT / "app" / "api"
ROUTERS = API / "routers"

# (file stem, field) -> why this one is deliberate. Check 5 keys on
# (contract path, name) instead, because it matches operations, not modules.
#
# Each of these is a field that IS null or unread on purpose. Anything not listed
# is a field the contract promises and the code does not deliver — and anything
# listed that the checks no longer find is reported as stale.
ALLOWED: dict[tuple[str, str], str] = {
    ("identity", "platform"): (
        "`auth_sessions` has `device_label` and `user_agent_hash` and no platform "
        "column, so there is nothing to return. Either add the column and capture "
        "it at sign-in, or drop the field from the schema — but do not invent a "
        "value. Open item in docs/design/0011-ci.md."),

    # Hotspots are stored and returned as an opaque jsonb list — `list(v.hotspots
    # or [])` — so the object's own keys never appear as Python dict literals. A
    # limitation of check 3, not a defect: the field IS returned, inside a blob
    # this script cannot see into. `Hotspot.slot` is not listed because `"slot"`
    # is a dict key elsewhere in the app (a realtime channel family), which is
    # the name-coincidence check 3 accepts by design.
    ("Hotspot", "x"): "jsonb passthrough — see the comment above.",
    ("Hotspot", "y"): "jsonb passthrough — see the comment above.",

    # `_classmate_dto` nulls these ON PURPOSE — see docs/design/0011-ci.md §14.1.
    # A student sees a classmate's name; a phone number, a minor flag and a target
    # band are what the teaching roles get. The redaction reads exactly like an
    # unimplemented field, which is why it is written down here rather than argued
    # about again in six months.
    ("identity", "phone"): "Redacted for classmates by `_classmate_dto`. §14.1.",
    ("identity", "telegram_username"): "Redacted for classmates. §14.1.",
    ("identity", "is_minor"): (
        "Redacted for classmates — this one above all. §14.1."),
    ("identity", "target_band"): "Redacted for classmates. §14.1.",
    ("identity", "created_at"): "Redacted for classmates. §14.1.",

    # Null because of WHEN it is read, not because it is unbuilt. Both are set
    # later in the row's life by a different endpoint.
    ("platform_ops", "outcome_note"): (
        "A takedown notice that was received one statement ago has no outcome. "
        "`decide_takedown` writes it."),
    ("platform_ops", "paid_at"): (
        "An order created one statement ago is `awaiting_payment`. The provider "
        "callback writes it."),
    ("assets", "parts_received"): (
        "An upload opened one statement ago has received no parts. The part "
        "endpoints write it — `upload_status` in platform_ops reads the real "
        "list back."),

    # Declared with a structure and emitted as empty constants. Not built: the
    # per-skill trend needs per-section bands on their own scale (0014 §8,
    # 'Scoring and bands'), and the cohort contract's `weak_types` shape does
    # not match the per-user one. No client reads either. Listed in 0014 §8 so
    # the inventory matches the code; remove these entries when they are built.
    ("platform_ops", "by_skill"): (
        "`GET /me/progress` answers `{}`: per-skill bands wait on a band map per "
        "skill. docs/design/0014-platform-flow.md §8."),
    ("platform_ops", "weak_types"): (
        "`[]` on both `/me/progress` and cohort progress: no item-type "
        "aggregation per student exists yet. docs/design/0014-platform-flow.md §8."),

    # ── check 4: an enum the handler validates instead of the model ──
    ("platform_ops", "GrantCreate.subject_type"): (
        "Resolved against `_SUBJECT_TABLES` in `create_grant`, which answers 404 "
        "'Unknown subject type.' — the table is the one source of truth for "
        "which kinds can be granted, and a second copy of it in a pattern would "
        "drift."),
    ("platform_ops", "ModerationActionCreate.target_subject_type"): (
        "Same `_SUBJECT_TABLES` lookup, same 404; pinned by "
        "test_safety_and_governance `test_an_unknown_subject_type_is_a_404_not_a_500`."),

    # ── check 5: declared in the contract, bound by no handler ────────
    ("/attempts", "org_context_xid"): (
        "The assignment decides the org context, not the request — exam.py "
        "`start_attempt`. Declared so the response shape is symmetrical; a value "
        "sent here is deliberately not honoured."),
    ("/attempts/{xid}/answers", "telemetry"): (
        "'Optional low-fidelity events for anti-cheat. Dropped silently if "
        "malformed.' There is no anti-cheat sink yet, so it is dropped whether "
        "or not it is malformed. Binding it to a field nothing reads would only "
        "move the finding to check 2."),
    ("/cohorts/{xid}/progress", "from"): (
        "`cohort_progress` reads `mv_cohort_progress` whole; the date window is "
        "declared and not applied. A cohort's history is weeks, not years, at "
        "pilot scale. Open item."),
    ("/cohorts/{xid}/progress", "to"): "Same window as `from`.",
}

# Contract schema -> Pydantic model, where the two names differ. Resolved by
# CLASS NAME and never by field name alone: `kind` is on six models and
# `skill` on five, and pairing by field would let one model vouch for another.
# A schema absent from the routers under both names (a nested object like
# `Block`, or `PaymeRpcRequest` whose handler takes `dict`) is skipped and
# counted; the tripwire below keeps the count honest.
SCHEMA_MODEL: dict[str, str] = {
    "ContentGrantCreate": "GrantCreate",
    "SafetyReportCreate": "ReportCreate",
    "QuestionGroupCreate": "GroupCreate",
    "TelegramVerifyRequest": "TelegramVerify",
    "AnswerKeyCreate": "AnswerKeyIn",
}

# Enum fields check 4 paired with a model on the day it was written. Fewer
# means a model was renamed out from under `SCHEMA_MODEL` and the check is
# quietly inspecting less — the same failure as `MIN_ROUTERS`.
MIN_ENUM_FIELDS = 24

# Every allowance a check actually consulted this run. `main()` reports the
# rest as stale — same shape as `check_console_coverage.py`'s `stale`.
_USED: set[tuple[str, str]] = set()


def _allowed(key: tuple[str, str]) -> bool:
    if key in ALLOWED:
        _USED.add(key)
        return True
    return False


# Parameters every handler has and nothing reads directly.
IGNORED_PARAMS = {"request", "response", "actor", "session", "exam", "ents",
                  "idem", "reg", "now", "db", "background"}


class Spec:
    """The declared shape: which names are response fields, which are required."""

    def __init__(self, document: dict) -> None:
        self.document = document
        self.schemas: dict = document.get("components", {}).get("schemas", {})
        self.response_schemas: set[str] = set()
        self.request_schemas: set[str] = set()
        # Not every schema is a named component. The `/imports` multipart form is
        # written INLINE, so `$ref` collection alone never saw `target_test_xid` —
        # the second of the two defects this script exists to catch.
        self.inline_response: set[str] = set()
        self.inline_request: set[str] = set()
        for item in document.get("paths", {}).values():
            for operation in item.values():
                if not isinstance(operation, dict):
                    continue
                for response in (operation.get("responses") or {}).values():
                    self._collect(response, self.response_schemas)
                    self._inline(response, self.inline_response)
                body = operation.get("requestBody") or {}
                self._collect(body, self.request_schemas)
                self._inline(body, self.inline_request)

    def _collect(self, node, into: set[str]) -> None:
        """Every schema name reachable from here, following $ref one hop at a
        time. A response that embeds another schema makes that schema a response
        schema too — `TestVersionDetail` allOf-ing `TestVersion` is the case."""
        found: set[str] = set()
        self._walk(node, found)
        frontier = set(found)
        while frontier:
            name = frontier.pop()
            if name in into:
                continue
            into.add(name)
            nested: set[str] = set()
            self._walk(self.schemas.get(name, {}), nested)
            frontier |= nested - into

    def _walk(self, node, out: set[str]) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                out.add(ref.rsplit("/", 1)[1])
            for value in node.values():
                self._walk(value, out)
        elif isinstance(node, list):
            for value in node:
                self._walk(value, out)

    def _inline(self, node, out: set[str]) -> None:
        """Property names of schema objects written in place rather than named."""
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                out |= set(node["properties"].keys())
            for value in node.values():
                self._inline(value, out)
        elif isinstance(node, list):
            for value in node:
                self._inline(value, out)

    def _properties(self, names: set[str]) -> set[str]:
        out: set[str] = set()
        for name in names:
            out |= self._props_of(name)
        return out

    def _props_of(self, name: str, seen: set[str] | None = None) -> set[str]:
        seen = seen or set()
        if name in seen:
            return set()
        seen.add(name)
        schema = self.schemas.get(name, {})
        out = set((schema.get("properties") or {}).keys())
        for branch in schema.get("allOf") or []:
            ref = branch.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                out |= self._props_of(ref.rsplit("/", 1)[1], seen)
            out |= set((branch.get("properties") or {}).keys())
        return out

    def response_fields(self) -> set[str]:
        return self._properties(self.response_schemas) | self.inline_response

    def request_fields(self) -> set[str]:
        return self._properties(self.request_schemas) | self.inline_request

    def required_response_fields(self) -> list[tuple[str, str]]:
        out = []
        for name in sorted(self.response_schemas):
            for field in self.schemas.get(name, {}).get("required") or []:
                out.append((name, field))
        return out

    def request_enums(self) -> list[tuple[str, str, frozenset[str]]]:
        """`(schema, field, values)` for every named request schema property
        that carries `enum`, including one declared through a `$ref` to an
        enum schema (`TestUpdate.visibility` -> `Visibility`). Inline request
        schemas have no name to pair a model with and are not covered."""
        out = []
        for name in sorted(self.request_schemas):
            for field, prop in sorted(self._property_specs(name).items()):
                values = self._enum_of(prop)
                if values:
                    out.append((name, field, values))
        return out

    def _enum_of(self, prop) -> frozenset[str] | None:
        if not isinstance(prop, dict):
            return None
        ref = prop.get("$ref", "")
        if ref.startswith("#/components/schemas/"):
            prop = self.schemas.get(ref.rsplit("/", 1)[1], {})
        values = prop.get("enum")
        if values and all(isinstance(v, str) for v in values):
            return frozenset(values)
        return None

    def _property_specs(self, name: str, seen: set[str] | None = None) -> dict:
        """Like `_props_of`, but the property schemas rather than the names."""
        seen = seen or set()
        if name in seen:
            return {}
        seen.add(name)
        schema = self.schemas.get(name, {})
        out = dict(schema.get("properties") or {})
        for branch in schema.get("allOf") or []:
            ref = branch.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                out.update(self._property_specs(ref.rsplit("/", 1)[1], seen))
            out.update(branch.get("properties") or {})
        return out

    def schema_props(self, schema: dict) -> set[str]:
        """Property names of a schema object wherever it is written: a `$ref`,
        inline `properties`, or `allOf`/`anyOf`/`oneOf` branches of either."""
        out: set[str] = set()
        ref = schema.get("$ref", "")
        if ref.startswith("#/components/schemas/"):
            out |= self._props_of(ref.rsplit("/", 1)[1])
        out |= set((schema.get("properties") or {}).keys())
        for key in ("allOf", "anyOf", "oneOf"):
            for branch in schema.get(key) or []:
                out |= self.schema_props(branch)
        return out


# The routers this project has. A check that silently inspects fewer than this
# reads exactly like a clean build — the same failure as a coverage floor matching
# zero files, or an import contract with a typo'd module name.
MIN_ROUTERS = 8


def _modules() -> list[tuple[Path, ast.Module]]:
    files = sorted(p for p in ROUTERS.glob("*.py") if p.name != "__init__.py")
    if len(files) < MIN_ROUTERS:
        raise SystemExit(
            f"FAIL  found {len(files)} router modules under {ROUTERS}, expected at "
            f"least {MIN_ROUTERS}. The package moved, or this script is now "
            f"checking almost nothing and would pass either way.")
    files.append(API / "dto.py")
    return [(p, ast.parse(p.read_text())) for p in files if p.exists()]


# ── check 1: a response field pinned to None ─────────────────────────

def pinned_to_none(modules, spec: Spec) -> list[str]:
    """Dict keys whose value is the literal None — or an empty `{}` / `[]` —
    everywhere they appear.

    A conditional — `str(x.xid) if x else None` — is an `IfExp`, not a constant,
    so a field that is *sometimes* null is not flagged. Only a field that has no
    other spelling anywhere in its module.

    Empty containers count because `GET /me/progress` answered `by_skill: {}`
    and `weak_types: []` for every student while the contract declared their
    structure — the same 'declared and never implemented' defect, one spelling
    away from the one this check was written for. `False`, `0` and `""` still
    do NOT count: they are legitimate defaults far more often than not (0011-ci
    §21.4), and widening to them would flag every one.
    """
    declared = spec.response_fields()
    findings = []
    for path, tree in modules:
        for scope in _dto_scopes(tree):
            constant, other = _keys_in(scope)
            for field, line in sorted(constant.items()):
                if field not in declared or field in other:
                    continue
                if _allowed((path.stem, field)):
                    continue
                findings.append(
                    f"{path.relative_to(ROOT)}:{line} emits {field!r} as a literal "
                    f"None / empty {{}} / [] and never anything else, but the "
                    f"contract declares it as a response field. Either implement "
                    f"it or say why in ALLOWED.")
    return findings


def _dto_scopes(tree: ast.Module):
    """One scope per function, plus the module body for stray literals.

    Per FUNCTION, not per file. A module-wide rule reads any other DTO's correct
    spelling as evidence — `read_review` emits `attempt_xid` properly, three
    functions from `_result_dto`, which hardcoded it to None. That is the exact
    defect this script was written for, and the coarse version did not see it.
    """
    functions = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    yield from functions
    inner = {id(n) for f in functions for n in ast.walk(f)}
    yield ast.Module(body=[n for n in tree.body if id(n) not in inner],
                     type_ignores=[])


def _keys_in(scope) -> tuple[dict[str, int], set[str]]:
    constant: dict[str, int] = {}
    other: set[str] = set()
    # `dict(row) if row else {"first_seen_at": None, ...}` — the literal is a
    # FALLBACK and the real value arrives by spread, so a dict that is a branch of
    # a conditional is never the only spelling of its fields.
    fallbacks = {id(b) for n in ast.walk(scope) if isinstance(n, ast.IfExp)
                 for b in (n.body, n.orelse)}
    for node in ast.walk(scope):
        # `payload["x"] = value` counts as a real assignment too.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)):
                    other.add(target.slice.value)
        if not isinstance(node, ast.Dict) or id(node) in fallbacks:
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if _is_empty_constant(value):
                constant.setdefault(key.value, key.lineno)
            else:
                other.add(key.value)
    return constant, other


def _is_empty_constant(value) -> bool:
    """`None`, `{}`, `[]` or `()` written literally. A `{**row}` is a Dict node
    with a None key, and a `[x for ...]` is a ListComp, so neither is caught."""
    if isinstance(value, ast.Constant):
        return value.value is None
    if isinstance(value, ast.Dict):
        return not value.keys
    if isinstance(value, (ast.List, ast.Tuple)):
        return not value.elts
    return False


# ── check 2: a request field nothing reads ───────────────────────────

def unread_request_fields(modules, spec: Spec) -> list[str]:
    """Declared on the way in, never looked at.

    Covers both halves of how a request arrives: Pydantic model attributes and
    `Form`/`Query`/`Header` parameters. `assignment_xid` was the first kind and
    `target_test_xid` the second, and they are the same defect — the client is
    invited to send something the server ignores.
    """
    declared = spec.request_fields()
    findings = []
    for path, tree in modules:
        # Three ways a field gets read: `body.field`, a bare `field` (which is how
        # a Form parameter arrives), and `"field"` as a string. All three count —
        # the question is only whether the value reaches the handler's logic.
        # `body.field` for a Pydantic model, a bare `field` for a Form parameter.
        # String constants deliberately do NOT count: a field name appearing as
        # its own key in the response DTO is not the handler reading the request,
        # and counting it hid `cue_card_set_version_xid` — accepted on slot
        # creation, echoed back as null, never stored.
        read = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        read |= {n.id for n in ast.walk(tree)
                 if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        wholesale = _models_dumped_wholesale(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in wholesale:
                read |= {f for f, _line in _model_fields(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and _is_model(node):
                for field, line in _model_fields(node):
                    if _flagged(path, field, read, declared):
                        findings.append(
                            f"{path.relative_to(ROOT)}:{line} {node.name}.{field} "
                            f"is accepted from the client and never read.")
            elif isinstance(node, ast.FunctionDef):
                for field, line in _bound_params(node):
                    if _flagged(path, field, read, declared):
                        findings.append(
                            f"{path.relative_to(ROOT)}:{line} {node.name}() takes "
                            f"{field!r} from the request and never reads it.")
    return findings


def _models_dumped_wholesale(tree: ast.Module) -> set[str]:
    """Models whose `model_dump()` is SPREAD or RETURNED, so every field is used.

    `Organization(**body.model_dump())` and `return body.model_dump()` read the
    whole model, and flagging their fields would be noise. A dump passed as an
    ordinary argument does NOT count — `idem.replay("attempts.start",
    body.model_dump())` uses it as an idempotency fingerprint, and treating that
    as a read is what would have hidden `assignment_xid`, the defect this script
    exists for.

    Resolved PER FUNCTION, from the parameter's annotation, rather than per file.
    A module-wide rule would let one wholesale dump in `platform_ops.py` — 1300
    lines and a dozen models — vouch for every other model in it.
    """
    out: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        models = {a.arg: a.annotation.id for a in function.args.args
                  if isinstance(a.annotation, ast.Name)}
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                dumps = [kw.value for kw in node.keywords if kw.arg is None]
                dumps += [a.value for a in node.args if isinstance(a, ast.Starred)]
            elif isinstance(node, ast.Return):
                dumps = [node.value]
            else:
                continue
            for dump in dumps:
                name = _dumped_name(dump)
                if name in models:
                    out.add(models[name])
    return out


def _dumped_name(node) -> str | None:
    """`body` from `body.model_dump(...)`, or None if this is not a dump."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("model_dump", "dict")):
        return None
    target = node.func.value
    return target.id if isinstance(target, ast.Name) else None


def _flagged(path: Path, field: str, read: set[str], declared: set[str]) -> bool:
    return (field in declared and field not in read
            and not _allowed((path.stem, field)))


def _is_model(node: ast.ClassDef) -> bool:
    return any(isinstance(b, ast.Name) and b.id == "BaseModel" for b in node.bases)


def _model_fields(node: ast.ClassDef):
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target,
                                                               ast.Name):
            yield statement.target.id, statement.lineno


def _bound_params(node: ast.FunctionDef):
    """Parameters whose default is `Form(...)`, `Query(...)` or `Header(...)` —
    i.e. values that come off the wire rather than out of the DI container."""
    args = node.args
    defaults = dict(zip(args.args[len(args.args) - len(args.defaults):],
                        args.defaults, strict=True))
    for arg, default in defaults.items():
        if arg.arg in IGNORED_PARAMS:
            continue
        if (isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
                and default.func.id in ("Form", "Query", "Header")):
            yield arg.arg, arg.lineno


# ── check 3: a required response field emitted nowhere ───────────────

def never_emitted(modules, spec: Spec) -> list[str]:
    """Coarse: a name can coincide with an unrelated dict key. Sound in the only
    direction that matters — a required field that appears as a key nowhere in the
    API layer is certainly not being returned."""
    # The whole app, not just the router package: a response field can be built by
    # a DTO helper in `app/modules` — `Finding.as_dict()` is one — and narrowing to
    # `app/api` would report those as missing when they are merely elsewhere.
    keys: set[str] = set()
    for source in sorted(APP.rglob("*.py")):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys |= {k.value for k in node.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        # `return body.model_dump()` emits every field of that model without a
        # dict literal anywhere — `add_lexicon` does exactly this.
        wholesale = _models_dumped_wholesale(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in wholesale:
                keys |= {f for f, _line in _model_fields(node)}
    findings = []
    for schema, field in spec.required_response_fields():
        if field in keys or _allowed((schema, field)):
            continue
        findings.append(
            f"{schema}.{field} is `required` in the contract and appears as a "
            f"response key nowhere in app/api.")
    return findings


# ── check 4: a contract enum the model does not enforce ──────────────

def unvalidated_enums(modules, spec: Spec) -> tuple[list[str], int]:
    """A request field declared `enum` in the contract whose Pydantic field
    accepts any string.

    `InviteCreate.role` was `str`; `role: "platform_admin"` went through the
    model, into `INSERT INTO org_invites`, and came back as a 500 from the
    column's CHECK — where the contract promised a 422 naming the field. Eleven
    more request fields had the same shape, and 1400 tests sent none of them a
    value outside the enum, because tests written from the implementation send
    what the implementation stores.

    Three spellings count as enforcement: `Literal[...]`, a `StrEnum` defined in
    the same module, and `Field(pattern="^(a|b|c)$")`. In every case the set of
    accepted values must EQUAL the contract's, so a pattern that lost a member
    when the enum grew is reported as drift rather than passed as 'validated'.

    Returns the findings and how many enum fields were paired with a model — the
    tripwire in `main()` refuses a run that paired fewer than `MIN_ENUM_FIELDS`.
    """
    classes: dict[str, list[tuple[Path, ast.ClassDef]]] = {}
    for path, tree in modules:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, []).append((path, node))

    findings = []
    paired = 0
    for schema, field, values in spec.request_enums():
        names = {schema, SCHEMA_MODEL.get(schema, schema)}
        for name in sorted(names):
            for path, node in classes.get(name, []):
                if not _is_model(node):
                    continue
                statement = _field_statement(node, field)
                if statement is None:
                    # Declared and not on the model at all: check 5's finding,
                    # reported there against the operation.
                    continue
                paired += 1
                if _allowed((path.stem, f"{node.name}.{field}")):
                    continue
                accepted = _accepted_values(statement, path, node)
                where = f"{path.relative_to(ROOT)}:{statement.lineno} {node.name}.{field}"
                if accepted is None:
                    findings.append(
                        f"{where} accepts any string, but the contract declares "
                        f"the enum {sorted(values)}. Use Literal[...], a StrEnum, "
                        f"or Field(pattern=\"^(a|b|c)$\").")
                elif accepted != values:
                    findings.append(
                        f"{where} accepts {sorted(accepted)} but the contract "
                        f"declares {sorted(values)}.")
    return findings, paired


def _field_statement(node: ast.ClassDef, field: str) -> ast.AnnAssign | None:
    for statement in node.body:
        if (isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == field):
            return statement
    return None


def _accepted_values(statement: ast.AnnAssign, path: Path,
                     model: ast.ClassDef) -> frozenset[str] | None:
    """The set of strings this field admits, or None when it admits any."""
    # `Field(pattern="^(a|b|c)$", ...)`, the idiom the routers already use.
    value = statement.value
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            and value.func.id == "Field"):
        for keyword in value.keywords:
            if (keyword.arg == "pattern" and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)):
                return _alternation(keyword.value.value)
    annotation = statement.annotation
    # `Literal["a", "b"]`; `Literal["a"] | None` is a BinOp — take the Literal
    # branch, the `None` half is Pydantic's business.
    for node in ast.walk(annotation):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "Literal"):
            members = (node.slice.elts if isinstance(node.slice, ast.Tuple)
                       else [node.slice])
            if all(isinstance(m, ast.Constant) and isinstance(m.value, str)
                   for m in members):
                return frozenset(m.value for m in members)
    # A `StrEnum` class in the same module: its members are the accepted set.
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            enum = _enum_class(path, node.id)
            if enum is not None:
                return enum
    return None


def _alternation(pattern: str) -> frozenset[str] | None:
    """`^(a|b|c)$` -> {a, b, c}; anything more regex than that is a pattern,
    not an enum, and is reported as 'accepts any string' rather than guessed."""
    if not (pattern.startswith("^(") and pattern.endswith(")$")):
        return None
    alternatives = pattern[2:-2].split("|")
    if any(not alt or set(alt) & set(r".*+?[]{}\()^$") for alt in alternatives):
        return None
    return frozenset(alternatives)


def _enum_class(path: Path, name: str) -> frozenset[str] | None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == name):
            continue
        bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
                 for b in node.bases}
        if not bases & {"StrEnum", "Enum"}:
            return None
        members = set()
        for statement in node.body:
            if (isinstance(statement, ast.Assign)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)):
                members.add(statement.value.value)
        return frozenset(members)
    return None


# ── check 5: a spec-side name no handler binds ───────────────────────

METHODS = ("get", "post", "patch", "put", "delete")
PREFIX = "/api/v1"


def _generated_paths() -> tuple[dict[str, dict], dict]:
    """The document the app generates, keyed by contract path, plus its
    component schemas.

    The same source `check_api_coverage.py` reads, for the same reason: FastAPI
    has already applied every `Query(alias=...)` and listed every bound
    parameter and body property, so a per-operation set diff needs no
    router-prefix arithmetic — `platform_ops.py` alone has seven unprefixed
    sub-routers — and cannot attribute a handler to the wrong operation.
    """
    sys.path.insert(0, str(ROOT))
    from app.api.main import create_app

    generated = create_app().openapi()
    out = {}
    for path, item in generated.get("paths", {}).items():
        if path.startswith(PREFIX):
            out[path[len(PREFIX):] or "/"] = item
    return out, generated.get("components", {}).get("schemas", {})


def spec_names_nothing_binds(spec: Spec) -> tuple[list[str], int]:
    """A declared query parameter or request-body property that the served
    operation does not accept.

    `GET /cohorts/{xid}/progress` declared `from` and `to` and bound neither;
    `GET /tests` declared `status` and bound `status_filter`; `AnswerBatch`
    declared `telemetry` and the model had `deltas` only. Every generated client
    sends the name and FastAPI drops it — a filter that filters nothing is the
    same defect as a field nothing reads, seen from the other side.

    A handler that takes `body: dict` generates a schema with no properties and
    accepts everything; there is nothing to diff, and it is skipped. The
    `required`-ness of a response field is check 3's business; this check is
    request-side only and never widens to response properties, which are
    name-coincident dict literals the generated document cannot see.

    Returns the findings and how many operations were compared: zero means the
    app failed to import or the prefix moved, and `main()` refuses that run.
    """
    generated, components = _generated_paths()

    def generated_props(schema: dict, seen: set[str]) -> set[str]:
        out: set[str] = set()
        ref = schema.get("$ref", "")
        if ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[1]
            if name not in seen:
                seen.add(name)
                out |= generated_props(components.get(name, {}), seen)
        out |= set((schema.get("properties") or {}).keys())
        for key in ("allOf", "anyOf", "oneOf"):
            for branch in schema.get(key) or []:
                out |= generated_props(branch, seen)
        return out

    def parameter(node: dict) -> dict:
        ref = node.get("$ref", "")
        if ref.startswith("#/components/parameters/"):
            return spec.document["components"]["parameters"][ref.rsplit("/", 1)[1]]
        return node

    findings = []
    compared = 0
    for path, item in spec.document.get("paths", {}).items():
        for method in METHODS:
            operation = item.get(method)
            served = generated.get(path, {}).get(method)
            if not isinstance(operation, dict) or served is None:
                continue
            compared += 1
            label = f"{method.upper()} {path}"

            declared = {p["name"] for p in map(parameter, (item.get("parameters") or [])
                                                + (operation.get("parameters") or []))
                        if p.get("in") == "query"}
            bound = {p["name"] for p in served.get("parameters") or []
                     if p.get("in") == "query"}
            for name in sorted(declared - bound):
                if _allowed((path, name)):
                    continue
                findings.append(
                    f"{label} declares the query parameter {name!r} and the handler "
                    f"binds no parameter of that name. Bind it (`Query(alias=...)` "
                    f"when the Python name differs) or say why in ALLOWED.")

            body = operation.get("requestBody") or {}
            if not body:
                continue
            declared = set()
            for media in (body.get("content") or {}).values():
                declared |= spec.schema_props(media.get("schema") or {})
            accepted: set[str] = set()
            for media in ((served.get("requestBody") or {}).get("content") or {}).values():
                accepted |= generated_props(media.get("schema") or {}, set())
            if not accepted:
                continue          # `body: dict` — everything is accepted.
            for name in sorted(declared - accepted):
                if _allowed((path, name)):
                    continue
                findings.append(
                    f"{label} declares the body property {name!r} and the request "
                    f"model has no such field, so a client that sends it is "
                    f"silently ignored. Add it to the model or say why in ALLOWED.")
    return findings, compared


def main() -> int:
    listing = "--list" in sys.argv
    document = yaml.safe_load((ROOT / "openapi" / "openapi.yaml").read_text())
    spec = Spec(document)
    modules = _modules()

    # The same tripwire as the coverage floors and the import contracts: a check
    # that inspects nothing passes silently and reads exactly like a clean build.
    if not modules or not spec.response_fields() or not spec.request_fields():
        print("FAIL  nothing to check — the spec or the router package moved.")
        return 1

    enum_findings, enum_fields = unvalidated_enums(modules, spec)
    unbound_findings, operations = spec_names_nothing_binds(spec)
    if enum_fields < MIN_ENUM_FIELDS or not operations:
        print(f"FAIL  check 4 paired {enum_fields} enum fields (expected at least "
              f"{MIN_ENUM_FIELDS}) and check 5 compared {operations} operations. "
              f"A model was renamed out from under SCHEMA_MODEL, or the app did "
              f"not import — either way the gate would pass by inspecting nothing.")
        return 1

    checks = (
        ("response fields pinned to None", pinned_to_none(modules, spec)),
        ("request fields nothing reads", unread_request_fields(modules, spec)),
        ("required response fields never emitted", never_emitted(modules, spec)),
        ("contract enums the model does not enforce", enum_findings),
        ("declared names no handler binds", unbound_findings),
        # After every check has run, so `_USED` is complete. An allowance that
        # nothing consulted is either fixed — delete it — or the check stopped
        # seeing the field it excuses, which is worse.
        ("stale ALLOWED entries", [
            f"ALLOWED[{key!r}] suppresses nothing. The field is implemented now, "
            f"or the check that used to find it no longer does; delete the entry "
            f"or find out which."
            for key in sorted(set(ALLOWED) - _USED)]),
    )
    print(f"modules          {len(modules)}")
    print(f"response fields  {len(spec.response_fields())} "
          f"across {len(spec.response_schemas)} schemas")
    print(f"request fields   {len(spec.request_fields())} "
          f"across {len(spec.request_schemas)} schemas")
    print(f"enum fields      {enum_fields} paired with a model")
    print(f"operations       {operations} compared with the generated document")
    print(f"allowed          {len(ALLOWED)}")

    total = 0
    for title, findings in checks:
        print(f"\n{'  ' if not findings else '✗ '}{title}: {len(findings)}")
        for finding in findings:
            print(f"    {finding}")
        total += len(findings)

    if listing:
        return 0
    if total:
        print(f"\nFAIL  {total} field(s) the contract declares and the code does "
              f"not implement, or allowance(s) that excuse nothing")
        return 1
    print("\nPASS  every declared field is implemented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
