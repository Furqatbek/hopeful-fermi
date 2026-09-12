#!/usr/bin/env python3
"""Does anything ever WRITE the column this query filters on?

`check_schema_conformance.py` asks the contract's question: is every declared
field implemented. This asks the database's, and it is the direction that
produced ten of the sixteen defects the console build-out turned up:

    safety_reports.status      read by the minors filter, written by nothing —
                               so the moderation queue could never be emptied
    passages.archived_at       filtered by four listings, written by nothing —
                               so "retire this item" had no action behind it
    cohort_members.left_at     the same shape again, for class membership
    seat_assignments.released_at        and again, for seats

Then it found five more on its own first honest runs, which is the part that
justifies keeping it:

    consents.revoked_at        a parent could grant `stranger_matching` and
                               never take it back — a child-safety control that
                               only switched one way
    entitlements.revoked_at    read by every access decision in the product; a
                               payment reversed at the bank left the feature on
    org_memberships.left_at    a centre could enrol somebody and never un-enrol
    users.deleted_at           read by seven queries; a student who asked to be
                               removed could only be given a conduct suspension
    cue_card_sets.archived_at  the fifth archivable asset, missed an hour
                               earlier when the other four got their endpoint

And three more from the second half, once it learned to read ORM comparisons
rather than raw SQL alone:

    questions.visibility       every asset carries `visibility` with a
    cue_card_sets.visibility   three-value CHECK and only `tests` could ever
                               change it, so `platform_global` was a value the
                               authorization layer asked about on every listing
                               and nothing could produce
    speaking_slots.status      `IN ('booking', 'matching')` in the batch
                               matcher, where nothing has ever written
                               `matching` — half of each predicate unreachable

Every one passed every gate this repository has, because every one is LOCALLY
CORRECT: the column exists, the filter is valid SQL, the read works, the type
checks. What is wrong is the relationship between two places, and nothing was
looking at relationships.

**The claim this gate makes is narrow and therefore checkable.** A predicate on
a column that no code path can change is a predicate that always answers the
same way — `WHERE archived_at IS NULL` over a column nothing sets is not a
filter, it is the word `TRUE` spelled at length. That is a defect independent of
intent, which is what makes it worth failing a build over.

## How it decides

Columns come from the live database rather than the ORM, because the tables
where this bug lives — `safety_reports`, `content_attestations`,
`item_exposure_stats`, `takedown_requests` — have no ORM model at all. They are
raw SQL, which is exactly why nothing else sees them.

Writes and reads come from the AST, not from regex, and that is not fastidious:
regex could not see either of the two shapes this codebase actually uses.
`Consent(user_id=..., kind=..., granted_by_kind=...)` spans lines and contains
nested calls, so a bracket-matching pattern stops early; and
`Organization(**body.model_dump())` cannot be resolved textually at all. The AST
sees the first exactly and can at least KNOW about the second — a `**` splat
marks its model opaque, so the gate says "I cannot tell" instead of "not
written", which is the difference between a useful gate and a noisy one.

There is a third shape, and it took the gate's own false alarm to add it: the
bulk statement, `update(AttemptSection).where(...).values(completed_at=now)`,
which assigns no attribute and constructs nothing. `attempt_sections
.completed_at` has exactly that one writer, and the gate failed the build over
it — a real defect in the gate, with the same signature as the defects it was
built to find, which is what an honest failure of this kind should look like.
`Resolver._values` reads it now, off the model the statement is for.

Raw SQL is still matched with patterns, because a SQL string is a string. But it
is matched against string CONSTANTS pulled out of the AST rather than the whole
file, so a column name inside a comment or a docstring is not a write.

## What it deliberately does not flag

  * Columns the DATABASE writes: a `DEFAULT`, a generated identity, or a
    trigger. `created_at` is not unwritten just because no Python assigns it.
  * Columns with a SQLAlchemy-side `default=` or `server_default=`, for the same
    reason one level up.
  * Partitions. `audit_log_2026_08` inherits every column of `audit_log` and is
    never written by name; flagging 60 partitions would bury the real ones.
  * Anything in `EXEMPT`, each with a reason, and a stale entry fails the build
    the same way the console gate's do — otherwise the list rots into a
    graveyard and the gate stops meaning anything.

## The direction it does NOT check

The mirror — written but never read — was also real here (`takedown_requests
.hidden_at`, `consents.doc_hash`). It is deliberately not a gate, because plenty
of columns exist to be read by a human with psql during an incident:
`audit_log.after` and every `content_attestations` column are written precisely
so that nothing routine reads them. A gate there would be mostly false alarms,
and a gate people learn to ignore is worse than none.
"""

from __future__ import annotations

import ast
import contextlib
import os
import re
import subprocess
import sys
import uuid
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [ROOT / "app"]

#: Columns a query filters on that nothing writes, with the reason it is
#: acceptable. Anything here is a decision, not a way to make the build pass.
EXEMPT: dict[str, str] = {
    "platform_role_grants.user_id": (
        "`resolve_principal` filters on it on every request; nothing in the API "
        "writes it, because there is no endpoint that grants platform admin. "
        "The first one is created with psql, deliberately — an API that can "
        "mint platform admins is a much larger blast radius than a bootstrap "
        "step somebody performs once."),
    "platform_role_grants.revoked_at": (
        "the same table and the same decision one column along: if no endpoint "
        "grants platform admin, none revokes it either, and both are psql. An "
        "endpoint that revokes is an endpoint that can lock every administrator "
        "out of the platform, which is a worse Tuesday than the one where a "
        "compromised admin has to be removed by hand. Revisit this when there "
        "is more than one operator: at that point 'by hand' stops being a "
        "safeguard and starts being a single point of failure."),
    "prices.product_id": (
        "the catalogue is seeded by migration and read at runtime. There is no "
        "price-management endpoint, and adding one is a product decision rather "
        "than a defect — `GET /products` is deliberately read-only."),
}


#: A SQL line comment. Python comments never reach the AST, but a `--` comment
#: lives INSIDE the string and survives — and this codebase writes long ones
#: full of table and column names. One of them, `-- the LEFT JOIN yields NULL`,
#: was being parsed as a table called `yields`.
_SQL_COMMENT = re.compile(r"--[^\n]*")


def _constants(node: ast.AST) -> list[str]:
    out = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            out.append(_SQL_COMMENT.sub(" ", child.value))
    return out


def sql_strings(tree: ast.AST) -> list[str]:
    """SQL as the code assembles it — **one joined blob per function**, not one
    string per literal.

    This grouping is load-bearing and it is what the first version got wrong.
    `moderation_queue` builds its filter by concatenation:

        sql = \'\'\'SELECT ... FROM safety_reports r WHERE 1=1\'\'\'
        if queue == "minors":
            sql += " AND r.involves_minor AND r.status <> \'dismissed\'"

    That fragment names no table, so scoping filters to the tables a STATEMENT
    mentions discarded it — and the gate missed `safety_reports.status`, the
    exact defect it was written for. Joining every constant in the enclosing
    function puts the fragment back beside its FROM clause.

    Constants only, so a column named in a comment or a docstring cannot look
    like a write — docstrings here discuss columns constantly.
    """
    out: list[str] = []
    covered: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            pieces = _constants(node)
            if pieces:
                out.append("\n".join(pieces))
            covered.update(id(c) for c in ast.walk(node))
    # Module-level constants, which no function encloses.
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in covered):
            out.append(node.value)
    return out


def _annotation_classes(node: ast.AST | None, classes: set[str]) -> list[str]:
    """Model classes named by an annotation, positionally for a `tuple[...]`.

    A list rather than a set so `tv, test = _version(...)` can unpack
    `-> tuple[TestVersion, Test]` in order.
    """
    if node is None:
        return []
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
            and node.value.id in {"tuple", "Tuple"}:
        inner = node.slice
        elements = inner.elts if isinstance(inner, ast.Tuple) else [inner]
        return [e.id for e in elements if isinstance(e, ast.Name) and e.id in classes]
    return [n.id for n in ast.walk(node)
            if isinstance(n, ast.Name) and n.id in classes]


class Scope:
    """Three namespaces, inherited by nested scopes and never written back.

    `models` is variable to ORM class. `schema` is variable to pydantic request
    class, and `fields` is variable to a set of field NAMES — the two that make
    `setattr(user, field, value)` readable instead of unknowable.
    """

    __slots__ = ("models", "schema", "fields")

    def __init__(self, parent: Scope | None = None) -> None:
        self.models: dict[str, set[str]] = dict(parent.models) if parent else {}
        self.schema: dict[str, str] = dict(parent.schema) if parent else {}
        self.fields: dict[str, set[str]] = dict(parent.fields) if parent else {}


def request_schemas(paths: list[Path]) -> dict[str, set[str]]:
    """`class UserUpdate(BaseModel)` to the field names it declares.

    Needed because the update endpoints are written as a loop:

        for field, value in body.model_dump(exclude_none=True).items():
            setattr(user, field, value)

    which assigns attributes the source never names. Marking `User` unknowable
    would be safe and useless — it would also swallow `users.deleted_at`, which
    seven queries filter on and nothing writes, because there is no
    account-deletion path. `UserUpdate` declares exactly four fields, none of
    them `deleted_at`, so the loop can be read precisely instead.
    """
    out: dict[str, set[str]] = {}
    for root in paths:
        for file in root.rglob("*.py"):
            try:
                tree = ast.parse(file.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not any(isinstance(b, ast.Name) and b.id == "BaseModel"
                           for b in node.bases):
                    continue
                out[node.name] = {
                    s.target.id for s in node.body
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
    return out


class Resolver:
    """Which ORM class does `row` in `row.archived_at = ...` refer to?

    **The gate's first version did not ask.** It kept one global set of every
    attribute name assigned anywhere, so `archived_at` assigned on a `Test`
    counted as a write for `Passage`, `Question`, `QuestionGroup` and
    `AudioTrack` too. Calibration is what exposed it: reverting three of the four
    bugs this gate exists for — the archive endpoints, `cohort_members.left_at`,
    `seat_assignments.released_at` — left it passing. A gate that cannot fail on
    the defects it was written for is decoration.

    So resolve the receiver. This is not general type inference and does not try
    to be; it is four rules that cover the shapes this codebase actually writes:

      1. **A model class named in the expression.** `session.scalars(select(
         CohortMember).where(...)).first()` mentions exactly one model, and that
         is what comes back. Also covers `session.get(Cohort, id)`, `Cohort(...)`
         and comprehensions over either.
      2. **A helper's return annotation.** `test = _test(session, xid, actor)`
         resolves through `-> Test`; `tv, test = _version(...)` unpacks
         `-> tuple[TestVersion, Test]` positionally.
      3. **A parameter, from the call sites in its own file.** `_archive` takes
         `model: Any` and assigns `row.archived_at`; it is called with `Passage`,
         `Question`, `QuestionGroup` and `AudioTrack`. One level of propagation,
         and it is what makes deleting `archive_passage` flag `passages
         .archived_at` instead of leaving three siblings to cover for it.
      4. **Bound names in the expression.** `row = _owned(session, model, ...)`
         carries whatever `model` resolves to.
      5. **The statement form.** `update(AttemptSection).where(...).values(
         completed_at=now)` assigns no attribute anywhere; the model is the
         argument of `update(...)` or `insert(...)`, or the class in front of
         `.__table__.update()`. See `_values`.

    A name resolving to several classes counts for all of them, which is the
    honest reading: `_archive`'s `row` really is any of four. When nothing
    resolves, the assignment is recorded as UNRESOLVED with its location rather
    than silently counting for everything — `main` prints those beside any
    finding, so a false alarm points at the blind spot that caused it.
    """

    def __init__(self, tree: ast.AST, classes: set[str],
                 schemas: dict[str, set[str]], where: str) -> None:
        self.classes = classes
        self.schemas = schemas
        self.where = where
        self.assigned: dict[str, set[str]] = defaultdict(set)
        self.unresolved: dict[str, set[str]] = defaultdict(set)
        #: Classes written through `setattr` with a name nothing can pin down.
        self.dynamic: set[str] = set()
        #: (class, attribute) to the string LITERALS assigned to it, and the
        #: attributes assigned something that is not a literal. Together these
        #: say which values a column can hold, which is what the second half of
        #: this gate needs and could not previously see for an ORM-managed
        #: column at all.
        self.values: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set))
        self.opaque_attrs: dict[str, set[str]] = defaultdict(set)
        self.returns: dict[str, list[str]] = {}
        self.params: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set))
        self._collect_signatures(tree)
        self._scope(getattr(tree, "body", []), Scope())

    # ── pass one: what the file says about its own functions ──────────
    def _collect_signatures(self, tree: ast.AST) -> None:
        functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                functions[node.name] = node
                self.returns[node.name] = _annotation_classes(node.returns, self.classes)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            target = functions.get(node.func.id)
            if target is None:
                continue
            names = [a.arg for a in target.args.posonlyargs + target.args.args]
            for index, argument in enumerate(node.args):
                # A bare class name only. `_archive(session, Passage, ...)` is a
                # fact; anything more clever here would guess.
                if isinstance(argument, ast.Name) and argument.id in self.classes \
                        and index < len(names):
                    self.params[target.name][names[index]].add(argument.id)
            for keyword in node.keywords:
                if keyword.arg and isinstance(keyword.value, ast.Name) \
                        and keyword.value.id in self.classes:
                    self.params[target.name][keyword.arg].add(keyword.value.id)

    # ── pass two: one scope at a time, in source order ────────────────
    def _classes_of(self, node: ast.AST | None, scope: Scope) -> set[str]:
        """A named class WINS over anything a bound variable contributes.

        The union of both was too generous by exactly one shape, and it took a
        sabotage run to find it:

            for row in session.scalars(
                select(CohortMember).join(Cohort, ...)
                .where(Cohort.org_id == org.id,
                       CohortMember.user_id == membership.user_id, ...)):
                row.left_at = now

        `membership` is an `OrgMembership` a few lines up, and it appears here
        only as the right-hand side of a predicate — so unioning its classes in
        made this loop count as a write of `org_memberships.left_at`, and
        deleting the real one left the gate silent. A `select` that names its
        model has told you what comes back; a variable inside the WHERE clause
        is an argument, not the row type.

        The fallback still matters and is not weakened: `row = _owned(session,
        model, ...)` names no class at all, so `model` — resolved from
        `_archive`'s call sites — is the only evidence there is.
        """
        if node is None:
            return set()
        named: set[str] = set()
        inferred: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                if child.id in self.classes:
                    named.add(child.id)
                else:
                    inferred |= scope.models.get(child.id, set())
            elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                inferred.update(self.returns.get(child.func.id, ()))
        return named or inferred

    def _names_of(self, node: ast.AST, scope: Scope) -> set[str] | None:
        """Field names an expression yields: `body.model_dump().items()`.

        None means "not a field-name source", which is different from an empty
        set — an empty set is a schema that declares nothing.
        """
        if isinstance(node, ast.Name):
            return scope.fields.get(node.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"items", "keys"}:
                return self._names_of(node.func.value, scope)
            if node.func.attr == "model_dump" and isinstance(node.func.value, ast.Name):
                cls = scope.schema.get(node.func.value.id)
                return self.schemas.get(cls) if cls else None
        return None

    def _bind(self, target: ast.AST, value: ast.AST, scope: Scope) -> None:
        names = self._names_of(value, scope)
        if names is not None:
            # `data = body.model_dump()`, or `for field, value in (...).items()`
            # where the first element is the name.
            head = target.elts[0] if isinstance(target, ast.Tuple) and target.elts \
                else target
            if isinstance(head, ast.Name):
                scope.fields[head.id] = names
        # `tv, test = _version(...)` against `-> tuple[TestVersion, Test]`.
        if isinstance(target, ast.Tuple) and isinstance(value, ast.Call) \
                and isinstance(value.func, ast.Name):
            parts = self.returns.get(value.func.id, [])
            if len(parts) == len(target.elts):
                for element, cls in zip(target.elts, parts, strict=True):
                    if isinstance(element, ast.Name):
                        scope.models[element.id] = {cls}
                return
        found = self._classes_of(value, scope)
        if not found:
            return
        for element in (target.elts if isinstance(target, ast.Tuple) else [target]):
            if isinstance(element, ast.Name):
                scope.models[element.id] = scope.models.get(element.id, set()) | found

    def _record(self, target: ast.Attribute, scope: Scope, line: int,
                value: ast.AST | None = None) -> None:
        owners = self._classes_of(target.value, scope)
        if not owners:
            self.unresolved[target.attr].add(f"{self.where}:{line}")
            return
        literal = (value.value if isinstance(value, ast.Constant)
                   and isinstance(value.value, str) else None)
        for cls in owners:
            self.assigned[cls].add(target.attr)
            # WHICH value, not just that there is one. `tv.status = "archived"`
            # is a claim that `archived` occurs; `row.status = body.status` is a
            # claim that anything might.
            if literal is None:
                self.opaque_attrs[cls].add(target.attr)
            else:
                self.values[cls][target.attr].add(literal)

    def _setattr(self, node: ast.Call, scope: Scope) -> None:
        """`setattr(user, field, value)` — the attribute name is a variable.

        All four update endpoints are written this way, so `users.family_name`
        had no assignment anywhere in the source and the gate flagged it. The
        name is resolvable, though: `field` iterates `UserUpdate.model_dump()`,
        and `UserUpdate` declares exactly four fields.

        Resolving it rather than declaring the class unknowable is what keeps
        `users.deleted_at` — which seven queries filter on, which nothing writes,
        and which `UserUpdate` does not declare. Blanket opacity would have been
        safe and would have thrown that finding away.
        """
        if len(node.args) < 2:
            return
        owners = self._classes_of(node.args[0], scope)
        name = node.args[1]
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            written: set[str] | None = {name.value}
        else:
            written = self._names_of(name, scope)
        for cls in owners:
            if written is None:
                self.dynamic.add(cls)
            else:
                self.assigned[cls] |= written
                # `setattr(row, name, value)` never names its value, so every
                # attribute it can reach can hold anything.
                self.opaque_attrs[cls] |= written

    def _values(self, node: ast.Call, scope: Scope) -> None:
        """`update(AttemptSection).where(...).values(completed_at=now)` — the
        statement form of a write, which assigns no attribute anywhere.

        Every other write in this codebase is an attribute assignment, a
        constructor keyword or a `setattr`, and those were the shapes the
        resolver read. `attempt_sections.completed_at` is written by exactly one
        thing — the bulk UPDATE in `session.py` that closes every earlier
        section when a student enters a later one — and the gate reported it as
        written by nothing, against a write that had been in the tree for
        weeks. `scan`'s generic keyword pass did see `.values(completed_at=...)`;
        it keys keywords by the callee's name, so the write was filed under a
        class called `values`.

        The owner is the model the statement is FOR, not every class the chain
        mentions. `update(ScoreRun).where(ScoreRun.attempt_id == ...)` names one
        class twice, but a joined update's `.where(Cohort.org_id == ...)` names
        a class the statement does not write — so it is read off `update(X)`,
        `insert(X)` and `X.__table__.update()` only, and a chain with none of
        them is recorded as unresolved beside the attribute assignments that
        could not be typed, rather than counting for everything.
        """
        owners: set[str] = set()
        for child in ast.walk(node.func.value):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Name) \
                    and child.func.id in {"update", "insert"} and child.args:
                owners |= self._classes_of(child.args[0], scope)
            elif isinstance(child.func, ast.Attribute) \
                    and child.func.attr in {"update", "insert"} \
                    and isinstance(child.func.value, ast.Attribute) \
                    and child.func.value.attr == "__table__":
                owners |= self._classes_of(child.func.value.value, scope)
        # Keywords, or the `{"column": value}` form; a `**splat` or a computed
        # key is a name nothing can read, the same as `Organization(**...)`.
        written: list[tuple[str | None, ast.AST]] = [
            (keyword.arg, keyword.value) for keyword in node.keywords]
        for argument in node.args:
            if isinstance(argument, ast.Dict):
                written.extend(
                    (key.value if isinstance(key, ast.Constant)
                     and isinstance(key.value, str) else None, value)
                    for key, value in zip(argument.keys, argument.values, strict=True))
        if not owners:
            for name, _ in written:
                self.unresolved[name or "**"].add(f"{self.where}:{node.lineno}")
            return
        for name, value in written:
            for cls in owners:
                if name is None:
                    self.dynamic.add(cls)
                    continue
                self.assigned[cls].add(name)
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    self.values[cls][name].add(value.value)
                else:
                    self.opaque_attrs[cls].add(name)

    def _scope(self, body: list[ast.stmt], scope: Scope) -> None:
        # Two passes so a helper defined below its use still resolves; source
        # order alone would miss `row = later_helper(...)`.
        for _ in range(2):
            for statement in body:
                self._statement(statement, scope)

    def _statement(self, statement: ast.stmt, scope: Scope) -> None:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            inner = Scope(scope)
            arguments = statement.args
            for argument in (arguments.posonlyargs + arguments.args
                             + arguments.kwonlyargs):
                annotated = _annotation_classes(argument.annotation, self.classes)
                from_calls = self.params.get(statement.name, {}).get(argument.arg, set())
                if annotated or from_calls:
                    inner.models[argument.arg] = set(annotated) | from_calls
                # `body: UserUpdate` — a request schema, not an ORM class.
                if isinstance(argument.annotation, ast.Name) \
                        and argument.annotation.id in self.schemas:
                    inner.schema[argument.arg] = argument.annotation.id
            self._scope(statement.body, inner)
            return
        if isinstance(statement, ast.ClassDef):
            self._scope(statement.body, Scope(scope))
            return

        for node in ast.walk(statement):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) \
                    and node is not statement:
                self._statement(node, scope)
                continue
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    self._bind(target, node.value, scope)
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                self._bind(node.target, node.value, scope)
            elif isinstance(node, ast.NamedExpr):
                self._bind(node.target, node.value, scope)
            elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
                self._bind(node.target, node.iter, scope)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                self._bind(node.optional_vars, node.context_expr, scope)

        # Writes last, so a binding made anywhere in the statement counts.
        for node in ast.walk(statement):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute):
                        self._record(target, scope, node.lineno, node.value)
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                if isinstance(node.target, ast.Attribute):
                    self._record(node.target, scope, node.lineno, node.value)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "setattr":
                self._setattr(node, scope)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "values" \
                    and (node.keywords or any(isinstance(a, ast.Dict) for a in node.args)):
                # A bare `.values()` is a dict's; only one that says what it
                # writes is a statement's.
                self._values(node, scope)


class Facts:
    """Everything the AST could establish about writes, in one place.

    Grew from a five-tuple once the second half of this gate needed to know
    WHICH values a column can hold rather than merely that something writes it.
    """

    __slots__ = ("assigned", "values", "opaque_attrs", "unresolved",
                 "kwargs", "opaque", "sql")

    def __init__(self) -> None:
        #: class to attribute names written on it, however.
        self.assigned: dict[str, set[str]] = defaultdict(set)
        #: class to attribute to the string literals it is assigned.
        self.values: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set))
        #: class to attributes assigned something that is NOT a literal, so the
        #: value set for them is open rather than closed.
        self.opaque_attrs: dict[str, set[str]] = defaultdict(set)
        #: attribute name to the sites where the receiver could not be typed.
        self.unresolved: dict[str, set[str]] = defaultdict(set)
        #: class to constructor keyword names.
        self.kwargs: dict[str, set[str]] = defaultdict(set)
        #: classes built with `**splat` or written by a computed `setattr`.
        self.opaque: set[str] = set()
        self.sql: list[str] = []

    def can_hold(self, cls: str | None, column: str,
                 default: str | None) -> tuple[set[str], bool]:
        """Values this column can hold through the ORM, and whether it is open.

        Open means a write nothing can pin to a literal, so no conclusion is
        available. A column with no model is closed and empty, which lets the
        raw-SQL side speak for itself.
        """
        values: set[str] = set()
        if default:
            literal = re.match(r"'([^']*)'", default)
            if literal:
                values.add(literal.group(1))
        if cls is None:
            return values, False
        values |= self.values.get(cls, {}).get(column, set())
        open_ended = (cls in self.opaque
                      or column in self.opaque_attrs.get(cls, ()))
        return values, open_ended


def scan(paths: list[Path], models: dict[str, str]) -> Facts:
    classes = set(models.values())
    schemas = request_schemas(paths)
    facts = Facts()

    for root in paths:
        for file in root.rglob("*.py"):
            try:
                tree = ast.parse(file.read_text())
            except SyntaxError:
                continue
            facts.sql.extend(sql_strings(tree))
            resolver = Resolver(tree, classes, schemas, str(file.relative_to(ROOT)))
            for cls, attributes in resolver.assigned.items():
                facts.assigned[cls] |= attributes
            for cls, attributes in resolver.opaque_attrs.items():
                facts.opaque_attrs[cls] |= attributes
            for cls, by_attribute in resolver.values.items():
                for attribute, literals in by_attribute.items():
                    facts.values[cls][attribute] |= literals
            for attribute, sites in resolver.unresolved.items():
                facts.unresolved[attribute] |= sites
            facts.opaque |= resolver.dynamic
            for node in ast.walk(tree):
                # `Consent(kind=..., granted_by_kind=...)`, and the splat that
                # makes a model unknowable.
                if isinstance(node, ast.Call):
                    name = (node.func.id if isinstance(node.func, ast.Name)
                            else getattr(node.func, "attr", None))
                    if not name:
                        continue
                    for keyword in node.keywords:
                        if keyword.arg is None:
                            facts.opaque.add(name)
                        else:
                            facts.kwargs[name].add(keyword.arg)
                            # `Attempt(status="in_progress")` names a value the
                            # column can hold; `Attempt(status=body.status)`
                            # says only that anything might.
                            if isinstance(keyword.value, ast.Constant) \
                                    and isinstance(keyword.value.value, str):
                                facts.values[name][keyword.arg].add(
                                    keyword.value.value)
                            else:
                                facts.opaque_attrs[name].add(keyword.arg)
    return facts


def updated_in_sql(table: str, column: str, sql: list[str]) -> bool:
    """Does anything ever CHANGE this column after the row exists?

    Separate from `written_in_sql` because a column with a DEFAULT is written on
    every insert and may still be frozen forever. `safety_reports.status`
    defaults to \'new\', and the minors queue filtered `status <> \'dismissed\'`
    — a comparison against a value the column could never hold, because no
    UPDATE anywhere set it. The queue could not be emptied, and skipping
    defaulted columns is why the first version of this gate did not catch it.
    """
    for statement in sql:
        for match in re.finditer(
                rf"UPDATE\s+{table}\s+SET(.*?)(?:\bWHERE\b|RETURNING|$)",
                statement, re.I | re.S):
            if re.search(rf"\b{column}\s*=", match.group(1)):
                return True
    return False


def compared_literals(table: str, column: str, sql: list[str]) -> set[str]:
    """String literals this column is compared against in SQL.

    LITERALS only, never parameters. `age_band = :band` says nothing about which
    values exist — the caller decides — whereas `status <> \'dismissed\'` is a
    claim in the source that the value `dismissed` occurs. That distinction is
    the whole precision of this half: the first version counted parameters and
    flagged four immutable columns that are set once at insert and correctly
    never change.

    **An UNQUALIFIED comparison counts only when the blob names one table.**
    `filtered_on` is deliberately generous about this — a filter it cannot
    attribute is still a filter, and being generous there only means more
    columns get checked for a writer. Here generosity is the opposite: it
    invents claims. `sql_strings` joins every constant in a function so that a
    fragment like `sql += " AND r.status <> 'dismissed'"` stays beside its FROM
    clause, and the cost is that a function touching two tables joins BOTH — so
    a bare `status = 'waiting'` from the speaking queue was being read as a
    claim about `users.status`, which is four values that column cannot hold and
    was never asked to.

    Qualified references are exact and are always used: `r.status` resolves
    through `FROM safety_reports r`, which is how the defect this gate was
    written for is still seen.
    """
    found: set[str] = set()
    for statement in sql:
        alias = aliases_in(statement)
        tables = set(alias.values())
        if table not in tables:
            continue
        for pattern in (rf"(?:(\w+)\.)?\b{column}\b\s*(?:=|<>|!=)\s*'([^']*)'",
                        rf"(?:(\w+)\.)?\b{column}\b\s+(?:NOT\s+)?IN\s*\(([^)]*)\)"):
            for match in re.finditer(pattern, statement, re.I):
                qualifier = (match.group(1) or "").lower()
                if qualifier:
                    # Unknown is not "mine". `a.status IN ('submitted',
                    # 'scored')` where `a` is a LATERAL subquery alias resolves
                    # to no table at all, and defaulting it to whichever table
                    # was being asked about attributed the attempt lifecycle to
                    # `assignments.status`.
                    if alias.get(qualifier) != table:
                        continue
                elif len(tables) > 1:
                    continue
                found.update(re.findall(r"'([^']*)'", match.group(2))
                             or [match.group(2)])
    # An empty literal is a comparison against "no value", which every column
    # can fail and none needs to produce.
    return {v for v in found if v and "'" not in v}


def producible_values(table: str, column: str, sql: list[str],
                      default: str | None) -> tuple[set[str], bool]:
    """Values this column can hold, and whether anything writes it dynamically.

    A dynamic write — a parameter, an expression — means any value is possible
    and there is nothing to conclude. Only when every write is a literal can the
    set be closed, which is exactly the case that catches a lifecycle column
    nothing ever advances.

    **A write this could not READ is also open**, and forgetting that produced
    three false findings at once. `INSERT INTO competitions (..., visibility,
    ...) VALUES (..., :vis, ...)` has a column list containing `CAST(:x AS
    uuid)`, and the `[^)]*` in the pattern below stops at that inner paren — so
    the insert was invisible, no value was collected, and `visibility = 'public'`
    looked unreachable against a column whose only other evidence was its
    DEFAULT. `written_in_sql` uses a looser pattern and does see it. When it
    says something writes the column and nothing here could say WHAT, the set is
    open rather than empty: "I cannot read this" must not be reported as "this
    cannot happen".
    """
    values: set[str] = set()
    from_writes: set[str] = set()
    dynamic = False
    if default:
        literal = re.match(r"\'([^\']*)\'", default)
        if literal:
            values.add(literal.group(1))
    for statement in sql:
        if table not in set(aliases_in(statement).values()):
            continue
        for match in re.finditer(
                rf"UPDATE\s+{table}\s+SET(.*?)(?:\bWHERE\b|RETURNING|$)",
                statement, re.I | re.S):
            for assign in re.finditer(rf"\b{column}\s*=\s*(\'([^\']*)\'|\S+)",
                                      match.group(1)):
                if assign.group(2) is not None:
                    from_writes.add(assign.group(2))
                else:
                    dynamic = True
        for match in re.finditer(rf"INSERT\s+INTO\s+{table}\s*\(([^)]*)\)"
                                 rf"\s*VALUES\s*\(([^)]*)\)",
                                 statement, re.I | re.S):
            names = [n.strip() for n in match.group(1).split(",")]
            vals = [v.strip() for v in match.group(2).split(",")]
            if column in names and len(names) == len(vals):
                value = vals[names.index(column)]
                literal = re.match(r"\'([^\']*)\'", value)
                if literal:
                    from_writes.add(literal.group(1))
                else:
                    dynamic = True
    # A DEFAULT is not a write, so it cannot answer "did I read every write".
    # Counting it here left `competitions.visibility` looking closed at its
    # default of `org` while the INSERT that sets it went unparsed.
    if not from_writes and written_in_sql(table, column, sql):
        dynamic = True
    return values | from_writes, dynamic


def written_in_sql(table: str, column: str, sql: list[str]) -> bool:
    for statement in sql:
        for match in re.finditer(rf"INSERT\s+INTO\s+{table}\s*\(([^)]*)\)",
                                 statement, re.I | re.S):
            if re.search(rf"\b{column}\b", match.group(1)):
                return True
        for match in re.finditer(rf"UPDATE\s+{table}\s+SET(.*?)(?:\bWHERE\b|RETURNING|$)",
                                 statement, re.I | re.S):
            if re.search(rf"\b{column}\s*=", match.group(1)):
                return True
    return False


#: Words that follow FROM/JOIN and are not a table, or follow a table and are
#: not its alias. `LEFT JOIN LATERAL (...) a ON true` was reporting a table
#: called `lateral`, and `FROM x SET` an alias called `set`.
_NOT_A_NAME = frozenset({
    "lateral", "only", "on", "using", "where", "set", "join", "left", "right",
    "full", "inner", "outer", "cross", "group", "order", "limit", "offset",
    "values", "returning", "select", "and", "or", "as", "natural", "union",
    "except", "intersect", "having", "window", "with", "for", "into"})

#: `FROM test_versions tv` / `JOIN tests AS tst`. The alias is optional.
#:
#: The two lookbehinds are for `SELECT ... FOR UPDATE` and `FOR NO KEY UPDATE`,
#: which are locking clauses and not statements. Without them the `UPDATE` in
#: `FOR UPDATE` matched, ate the newline, and took the next line's real
#: `UPDATE content_reviews` as its TABLE — leaving `content_reviews` recorded as
#: an alias of a table called `update`, so the statement that writes the column
#: was invisible to everything downstream.
_SOURCE = re.compile(
    r"(?:FROM|JOIN|(?<!FOR )(?<!KEY )UPDATE|INSERT\s+INTO)\s+([a-z_][a-z0-9_]*)"
    r"(?:\s+(?:AS\s+)?([a-z_][a-z0-9_]*))?", re.I)


def tables_in(statement: str) -> set[str]:
    """Which tables a SQL statement actually touches."""
    return {t for t in aliases_in(statement).values() if t is not None}


def aliases_in(statement: str) -> dict[str, str | None]:
    """Alias to table, so `tv.published_at` can be told from `qv2.published_at`.

    Without this the exposure query — which joins `question_versions qv2` and
    `test_versions tv` in one statement and ends `ORDER BY tv.published_at` —
    reported `question_versions.published_at` as filtered and unwritten. It is
    neither: that ORDER BY belongs to `test_versions`, which `repo.py` writes on
    publish. A statement naming a table is not a statement filtering it.

    **None means AMBIGUOUS**, and it is not the same as absent. `sql_strings`
    joins a whole function into one blob, so a function whose first query says
    `FROM assignments a` and whose second says `JOIN attempts a` binds `a`
    twice. Last-write-wins would silently pick one; recording the conflict lets
    the caller decline to guess.
    """
    out: dict[str, str | None] = {}
    for match in _SOURCE.finditer(statement):
        table = match.group(1).lower()
        if table in _NOT_A_NAME:
            continue
        names = [table]
        alias = (match.group(2) or "").lower()
        if alias and alias not in _NOT_A_NAME:
            names.append(alias)
        for name in names:
            out[name] = table if out.get(name, table) == table else None
    return out


def filtered_on(table: str, column: str, sql: list[str],
                compares: set[tuple[str, str]]) -> bool:
    """Is this column used as a PREDICATE **on this table**?

    Table-scoped, and that correction mattered more than it sounds. Matching the
    column name alone made every `user_id` in the schema look filtered, because
    some table's `user_id` always is — which produced five findings on tables no
    code touches at all. A filter belongs to a table only when the statement
    naming the filter also names the table.
    """
    if (table, column) in compares:
        return True
    for statement in sql:
        alias = aliases_in(statement)
        if table not in set(alias.values()):
            continue
        for pattern in (rf"(?:WHERE|AND|OR|ON)\s+(?:(\w+)\.)?\b{column}\b"
                        rf"\s*(?:=|<|>|IS|IN|ANY|<>|!=)",
                        rf"ORDER\s+BY\s+[\w.,\s]*?(?:(\w+)\.)?\b{column}\b"):
            for match in re.finditer(pattern, statement, re.I):
                qualifier = (match.group(1) or "").lower()
                # Unqualified, or qualified with something this statement never
                # aliased, or ambiguous: attribute it to any table present.
                # Generosity is the safe direction HERE — it only means more
                # columns get checked for a writer. Only a qualifier that
                # resolves to a DIFFERENT source is evidence against.
                if not qualifier or alias.get(qualifier, table) in (table, None):
                    return True
    return False


def _string_operands(node: ast.AST) -> set[str]:
    """String literals on the right of a comparison, including inside a list.

    `Attempt.status == "submitted"` and `.in_(["scored", "released"])` are both
    claims in the source that those values occur.

    The operand ITSELF, or the elements of a literal collection — not any string
    anywhere underneath it. Walking the whole subtree read
    `QuestionVersion.xid == uuid.UUID(raw or "")` as a claim that the column
    holds the empty string.
    """
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) and node.value else set()
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        out: set[str] = set()
        for element in node.elts:
            out |= _string_operands(element)
        return out
    return set()


def orm_compares(paths: list[Path], models: dict[str, str]) -> tuple[
        set[tuple[str, str]], dict[tuple[str, str], set[str]]]:
    """`Question.archived_at.is_(None)` and `Attempt.status == \'x\'`.

    Resolved to (table, column): the class name is right there in the AST, so
    unlike raw SQL this direction can be exact — no aliases, no two-table
    ambiguity, no guessing which model a bare `status` belongs to.

    Returns the comparisons AND the literals compared against, because the
    second half of this gate needs the latter and was reading only raw SQL for
    it. Half these tables are queried through the ORM, so half the evidence was
    invisible: `Attempt.status == "submitted"` is exactly the claim
    `status = 'submitted'` makes, and only one of the two was being counted.
    """
    by_class = {cls: table for table, cls in models.items()}
    found: set[tuple[str, str]] = set()
    literals: dict[tuple[str, str], set[str]] = defaultdict(set)
    for root in paths:
        for file in root.rglob("*.py"):
            try:
                tree = ast.parse(file.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                attribute, operands = None, set()
                if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute):
                    attribute = node.left
                    if all(isinstance(o, ast.Eq | ast.NotEq) for o in node.ops):
                        for comparator in node.comparators:
                            operands |= _string_operands(comparator)
                elif (isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Attribute)
                      and node.func.attr in {"is_", "isnot", "in_", "notin_",
                                             "ilike", "like", "any_"}
                      and isinstance(node.func.value, ast.Attribute)):
                    attribute = node.func.value
                    if node.func.attr in {"in_", "notin_"}:
                        for argument in node.args:
                            operands |= _string_operands(argument)
                if attribute is None or not isinstance(attribute.value, ast.Name):
                    continue
                table = by_class.get(attribute.value.id)
                if table:
                    found.add((table, attribute.attr))
                    literals[table, attribute.attr] |= operands
    return found, literals


def models_by_table() -> dict[str, str]:
    """`__tablename__` to ORM class name.

    A table WITHOUT a model is written only by raw SQL, which this gate matches
    per table — so the raw-SQL tables are exactly where it is most precise. That
    is fortunate rather than accidental: `safety_reports`, `takedown_requests`,
    `content_attestations` and `item_exposure_stats` have no model, and four of
    the defects that motivated this gate were in them.
    """
    out: dict[str, str] = {}
    for file in (ROOT / "app").rglob("models.py"):
        cls = None
        for line in file.read_text().splitlines():
            named = re.match(r"class (\w+)\(", line)
            if named:
                cls = named.group(1)
            table = re.search(r'__tablename__\s*=\s*"(\w+)"', line)
            if table and cls:
                out[table.group(1)] = cls
    return out


def model_defaults() -> set[tuple[str, str]]:
    """`mapped_column(default=...)` and `server_default=...`: the ORM writes it.

    **`default=None` does not count, and that exclusion is the whole gate.**

    Every one of the four columns this script was written for — `passages
    .archived_at`, `cohort_members.left_at`, `seat_assignments.released_at`,
    `test_versions.archived_at` — is declared `mapped_column(default=None)`, and
    the first version skipped all four here before any of the later logic ran. It
    passed while three of its four motivating bugs were reverted one at a time.

    The reasoning was wrong in a way worth naming: a default is a reason not to
    expect a write only when it supplies a VALUE. `default=None` supplies the
    absence of one. "This column starts empty" is not evidence that anything
    fills it — it is the precondition for the defect. A lifecycle column always
    starts null; the question this gate asks is whether it can ever stop being
    null, and `default=None` is silent on it.
    """
    out: set[tuple[str, str]] = set()
    for file in (ROOT / "app").rglob("models.py"):
        table = None
        for line in file.read_text().splitlines():
            named = re.search(r'__tablename__\s*=\s*"(\w+)"', line)
            if named:
                table = named.group(1)
            column = re.match(r"\s+(\w+):\s*Mapped\[.*mapped_column\((.*)", line)
            if column and table and re.search(
                    r"\b(?:default|server_default)\s*=\s*(?!None\b)", column.group(2)):
                out.add((table, column.group(1)))
    return out


@contextlib.contextmanager
def migrated_schema(url: str):
    """A scratch database at `alembic upgrade head`, dropped afterwards.

    **The gate used to read whatever `DATABASE_URL` happened to point at**, and
    that worked on a development box where it points at a migrated database. In
    CI `TEST_DATABASE_URL` is the bare `postgres` database — the suite builds
    its own scratch databases — so `information_schema.columns` came back empty,
    nothing was filtered, nothing was uncovered, and every EXEMPT entry looked
    stale. The gate failed the build with three confident findings derived from
    reading no schema at all.

    That is this gate's own subject matter turned on itself: an answer that does
    not depend on the code being checked. `check_invariants.py` had the right
    shape all along — migrate a scratch database, use it, drop it — so this now
    does the same, and the check no longer depends on which database somebody
    happens to have configured.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    name = f"ielts_wp_{uuid.uuid4().hex[:8]}"
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    scratch = parsed.set(database=name)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        migrate = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
            env={**os.environ,
                 "DATABASE_URL": scratch.render_as_string(hide_password=False)},
            capture_output=True, text=True)
        if migrate.returncode != 0:
            sys.stderr.write(migrate.stderr)
            raise SystemExit("FAIL  could not migrate the scratch database")
        engine = create_engine(scratch)
        try:
            yield engine
        finally:
            engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def main() -> int:
    from sqlalchemy import text

    url = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if not url:
        print("SKIP  no DATABASE_URL; this gate needs the schema")
        return 0

    with migrated_schema(url) as engine, engine.connect() as connection:
        partitions = {r[0] for r in connection.execute(text("""
            SELECT c.relname FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
        """))}
        # A trigger that writes NEW.<column> is the database writing it.
        trigger_bodies = " ".join(r[0] or "" for r in connection.execute(text("""
            SELECT prosrc FROM pg_proc p
            JOIN pg_trigger t ON t.tgfoid = p.oid WHERE NOT t.tgisinternal
        """)))
        columns = connection.execute(text("""
            SELECT table_name, column_name, column_default,
                   column_default IS NOT NULL AS has_default,
                   is_generated, is_identity
            FROM information_schema.columns WHERE table_schema = 'public'
        """)).all()

    # An empty schema is not a clean bill of health. Reading zero columns makes
    # every EXEMPT entry look stale and every filter look absent — a verdict that
    # does not depend on the code being checked, which is the exact defect this
    # file exists to find. Refuse rather than report.
    if len(columns) < 100:
        print(f"FAIL  the schema has {len(columns)} columns; expected the whole "
              "application. This gate cannot answer from an empty database, and "
              "answering anyway would report every exemption as stale.")
        return 1

    models = models_by_table()
    facts = scan(SOURCES, models)
    sql = facts.sql
    compares, compared = orm_compares(SOURCES, models)
    defaults = model_defaults()

    uncovered: list[tuple[str, str]] = []
    frozen: list[tuple[str, str, list[str]]] = []
    checked = 0
    for table, column, default, has_default, generated, identity in columns:
        # An identity column is written by the database on every insert, and
        # every foreign key in the schema filters on one — 30 of the first 90
        # findings were `x.id`, which is the database doing its job.
        if table in partitions or generated != "NEVER" or identity == "YES":
            continue
        cls = models.get(table)
        if has_default:
            # A column with a DEFAULT is written on every insert and may still
            # be frozen for ever. `safety_reports.status` defaulted to `new` and
            # the minors queue filtered `status <> 'dismissed'` — a comparison
            # against a value the column could never hold.
            if f"{table}.{column}" in EXEMPT:
                continue
            # Both sides of the evidence, from BOTH query styles. This used to
            # read raw SQL only and to skip any column the ORM assigns at all —
            # so for a half-ORM table it saw half the comparisons and none of
            # the values, and every such column came out looking frozen.
            wanted = compared_literals(table, column, sql) | compared.get(
                (table, column), set())
            if not wanted:
                continue
            sql_values, sql_open = producible_values(table, column, sql, default)
            orm_values, orm_open = facts.can_hold(cls, column, default)
            if sql_open or orm_open:
                continue
            unreachable = wanted - sql_values - orm_values
            if unreachable:
                frozen.append((table, column, sorted(unreachable)))
            continue
        if (table, column) in defaults:
            continue
        if re.search(rf"NEW\.{column}\b", trigger_bodies):
            continue
        if not filtered_on(table, column, sql, compares):
            continue
        checked += 1
        # Raw SQL is matched per table, always. Everything else needs a model,
        # and a table without one cannot be written any other way.
        written = written_in_sql(table, column, sql)
        if cls and not written:
            written = (
                column in facts.kwargs.get(cls, ())
                # A model built with `**something` writes columns this cannot
                # name. Unknown is not a finding — scoped to THAT model, so one
                # splat does not silence the whole schema.
                or cls in facts.opaque
                # `row.left_at = ...`, where `row` resolved to THIS class. See
                # `Resolver`: a global set of attribute names let four models
                # cover for each other and made the gate miss three of the four
                # bugs it exists for.
                or column in facts.assigned.get(cls, ()))
        if not written:
            uncovered.append((table, column))

    real = [f"{t}.{c}" for t, c in uncovered if f"{t}.{c}" not in EXEMPT]
    frozen_real = [(f"{t}.{c}", vals) for t, c, vals in frozen]
    stale = sorted(set(EXEMPT)
                   - {f"{t}.{c}" for t, c in uncovered}
                   - {f"{t}.{c}" for t, c, _ in frozen})

    print(f"columns          {len(columns)}")
    print(f"used as a filter {checked}")
    print(f"exempt           {len(EXEMPT) - len(stale)}")

    if real:
        print(f"\nFAIL  {len(real)} columns are FILTERED ON and written by nothing.")
        print("      A predicate on a column no code path can change always")
        print("      answers the same way. Write it, stop filtering on it, or")
        print("      add an EXEMPT entry saying why it is acceptable.")
        for name in sorted(real):
            print(f"        {name}")
            # The gate's own blind spot, printed next to the finding rather than
            # left for somebody to rediscover. If one of these assignments IS
            # this column on this model, the resolver could not see it — that is
            # a bug in `Resolver`, not in the code under test, and it should be
            # taught rather than exempted.
            sites = sorted(facts.unresolved.get(name.split(".", 1)[1], ()))
            if sites:
                print(f"          (unresolved `*.{name.split('.', 1)[1]} = ...` "
                      f"at {', '.join(sites)})")
    if frozen_real:
        # ADVISORY, and the reason is worth stating rather than hiding in an
        # exit code. Joining a function's SQL fragments is what lets this half
        # see `sql += " AND r.status <> \'dismissed\'"` at all — but a function
        # that touches two tables joins BOTH, and `status` exists on nine
        # tables here. Qualified references are resolved through their aliases
        # and an unqualified one is only believed when the blob names a single
        # table, which took this from 13 findings to 3 — all three real. It is
        # still a heuristic, and a heuristic should not fail a build.
        #
        # Under-reports too, and in a way worth knowing: `policy.filter_content
        # (actor, query, model)` compares `model.visibility` against
        # `platform_global` for EVERY content listing, and `model` is a
        # parameter no static reading of one call site resolves. Three of the
        # five assets whose visibility could never change were invisible here
        # for that reason; they were fixed because the other two named the bug.
        print(f"\nLOOK  {len(frozen_real)} columns are compared to a value that may")
        print("      not be producible. Heuristic — a function touching two")
        print("      tables can attribute one's literals to the other.")
        for name, values in sorted(frozen_real):
            print(f"        {name} — compared to {values}")
    if stale:
        print(f"\nFAIL  {len(stale)} exemptions are stale — these ARE written now.")
        for name in stale:
            print(f"        {name}  ({EXEMPT[name]})")

    # Only the first half fails. It is precise: a filter on a column no code
    # path writes is a filter that always answers the same way.
    #
    # **Calibrated, which is the only reason to believe any of it.** Each of the
    # eight defects of this shape was reverted in turn, one at a time, against a
    # tree that otherwise passes. All eight come back:
    #
    #   passages.archived_at             (with the other three archive endpoints
    #                                     LEFT IN, so a sibling cannot cover)
    #   cue_card_sets.archived_at
    #   cohort_members.left_at
    #   seat_assignments.released_at
    #   consents.revoked_at
    #   org_memberships.left_at
    #   entitlements.revoked_at
    #   users.deleted_at
    #
    # The first version of this file caught two of the eight while claiming to
    # be calibrated, because "revert it and see" had never actually been run.
    #
    # The advisory half is calibrated the same way, against the three it found
    # once it could read the ORM as well as raw SQL: reverting the visibility
    # endpoints brings back `questions.visibility` and
    # `cue_card_sets.visibility`, and restoring the dead `IN ('booking',
    # 'matching')` arm brings back `speaking_slots.status`.
    if real or stale:
        return 1
    print("\nPASS  every filtered column has a writer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
