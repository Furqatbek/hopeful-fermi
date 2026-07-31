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

Three checks, in descending order of precision:

  1. A response field pinned to the literal `None` and never assigned anything
     else. A field that is structurally always null is not implemented, whether
     or not the schema permits null.
  2. A request field — Pydantic model attribute, or `Form`/`Query` parameter —
     that no handler ever reads.
  3. A `required` response field whose name appears as a dict key nowhere in the
     API layer. Coarse (a name can coincide) but sound in the direction that
     matters: if it is emitted nowhere, it is emitted nowhere.

Findings are fixed, not silenced. `ALLOWED` exists for the genuinely-deliberate
cases and every entry carries its reason, because an allowlist without one is a
list somebody will append to during a bad afternoon.

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

# (file stem, field) -> why this one is deliberate.
#
# Each of these is a field that IS null or unread on purpose. Anything not listed
# is a field the contract promises and the code does not deliver.
ALLOWED: dict[tuple[str, str], str] = {
    ("assets", "next_cursor"): (
        "Cursor pagination is declared and deliberately not implemented yet — the "
        "authoring library is hundreds of rows at MVP scale, and `limit` covers it. "
        "The field is in the envelope so adding cursors later is not a breaking "
        "change. Tracked in docs/design/0011-ci.md."),
    ("teaching", "next_cursor"): "Same envelope, same reason as assets.",
    ("identity", "next_cursor"): "Same envelope, same reason as assets.",
    ("platform_ops", "next_cursor"): "Same envelope, same reason as assets.",

    ("assets", "burn_score"): (
        "Implemented, on `GET /questions/{xid}/exposure`, which reads "
        "`item_exposure_stats`. The library LISTING leaves it null on purpose: "
        "joining exposure stats per row turns a browse into a scan, and an author "
        "picking items opens the one they are considering. Null here means 'ask "
        "the exposure endpoint', not 'not built'."),

    ("identity", "platform"): (
        "`auth_sessions` has `device_label` and `user_agent_hash` and no platform "
        "column, so there is nothing to return. Either add the column and capture "
        "it at sign-in, or drop the field from the schema — but do not invent a "
        "value. Open item in docs/design/0011-ci.md."),

    ("Hotspot", "slot"): (
        "Hotspots are stored and returned as an opaque jsonb list — "
        "`list(v.hotspots or [])` — so the object's own keys never appear as "
        "Python dict literals. A limitation of check 3, not a defect: the field "
        "IS returned, inside a blob this script cannot see into."),
    ("Hotspot", "x"): "Same jsonb passthrough as Hotspot.slot.",
    ("Hotspot", "y"): "Same jsonb passthrough as Hotspot.slot.",

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

}

# Parameters every handler has and nothing reads directly.
IGNORED_PARAMS = {"request", "response", "actor", "session", "exam", "ents",
                  "idem", "reg", "now", "db", "background"}


class Spec:
    """The declared shape: which names are response fields, which are required."""

    def __init__(self, document: dict) -> None:
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
    """Dict keys whose value is the literal None, everywhere they appear.

    A conditional — `str(x.xid) if x else None` — is an `IfExp`, not a constant,
    so a field that is *sometimes* null is not flagged. Only a field that has no
    other spelling anywhere in its module.
    """
    declared = spec.response_fields()
    findings = []
    for path, tree in modules:
        for scope in _dto_scopes(tree):
            constant, other = _keys_in(scope)
            for field, line in sorted(constant.items()):
                if field not in declared or field in other:
                    continue
                if (path.stem, field) in ALLOWED:
                    continue
                findings.append(
                    f"{path.relative_to(ROOT)}:{line} emits {field!r} as a literal "
                    f"None and never anything else, but the contract declares it "
                    f"as a response field. Either implement it or say why in "
                    f"ALLOWED.")
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
            if isinstance(value, ast.Constant) and value.value is None:
                constant.setdefault(key.value, key.lineno)
            else:
                other.add(key.value)
    return constant, other


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
            and (path.stem, field) not in ALLOWED)


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
        if field in keys or (schema, field) in ALLOWED:
            continue
        findings.append(
            f"{schema}.{field} is `required` in the contract and appears as a "
            f"response key nowhere in app/api.")
    return findings


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

    checks = (
        ("response fields pinned to None", pinned_to_none(modules, spec)),
        ("request fields nothing reads", unread_request_fields(modules, spec)),
        ("required response fields never emitted", never_emitted(modules, spec)),
    )
    print(f"modules          {len(modules)}")
    print(f"response fields  {len(spec.response_fields())} "
          f"across {len(spec.response_schemas)} schemas")
    print(f"request fields   {len(spec.request_fields())} "
          f"across {len(spec.request_schemas)} schemas")
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
              f"not implement")
        return 1
    print("\nPASS  every declared field is implemented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
