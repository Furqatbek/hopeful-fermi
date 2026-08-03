"""The registry console: browse a definition, dry-run a new one, register it,
and add a tolerance pair.

These are the exact call sequences `web/src/features/registry/QuestionTypes.tsx`
and `Lexicon.tsx` issue, in the order they issue them, with the bodies they send.
The screens make three promises that only a real request can check:

  * **Registering is unreachable without a passing dry run.** The dry run must
    therefore write nothing and must report a fourth scoring primitive, because
    the closed set of three is the one architectural limit of the registry
    (ADR-0001 §8.3) and an admin who types a fourth should be told which three
    exist rather than shown a refusal.
  * **Re-adding a lexicon pair that already exists reports success and changes
    nothing.** `ON CONFLICT (kind, a, b) DO NOTHING`, and the response echoes the
    body that was sent rather than the row that survived — so the screen has to
    catch the duplicate itself.
  * **Nothing here can be taken back.** No delete, no deactivate, no status
    change, for either a question type or a lexicon entry.

Two defects are pinned rather than fixed, because the screens' copy depends on
them being true today. Both are in the report: a registered type does not
survive a restart, and a lexicon pair added through the API never reaches the
running scorer.

Every definition gets a unique key. The `Registry` is a PROCESS-WIDE singleton
that `reg.register()` mutates and the per-test database wipe cannot reach, so a
shared key would make the second test in a session register into a registry that
already held it.
"""

from __future__ import annotations

import uuid as _uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token


@pytest.fixture
def client(db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def auth(xid) -> dict:
    return {"Authorization": f"Bearer {issue_access_token(str(xid))}"}


def _ok(response, *expected):
    assert response.status_code in (expected or (200, 201)), response.text
    return response.json()


@pytest.fixture
def admin(db) -> dict:
    row = db.execute(text("""
        INSERT INTO users (phone, given_name, date_of_birth, status)
        VALUES (:p, 'Platform Admin', '1985-01-01', 'active')
        RETURNING id, xid
    """).bindparams(p=f"+9989{_uuid.uuid4().int % 10**8:08d}")).mappings().one()
    db.execute(text("""
        INSERT INTO platform_role_grants (user_id, role, granted_by)
        VALUES (:u, 'platform_admin', :u)
    """).bindparams(u=row["id"]))
    db.flush()
    return auth(row["xid"])


def _definition(**overrides) -> dict:
    """What the textarea holds: a real definition, of the shape
    `docs/design/examples/matching_sentence_endings.v1.json` documents."""
    body = {
        "key": f"console_registry_probe_{_uuid.uuid4().hex[:8]}",
        "version": 1,
        "status": "active",
        "title": "Matching sentence endings",
        "description": "Complete each stem with an ending from a shared list.",
        "skills": ["reading"],
        "payload_schema": {
            "type": "object", "required": ["stem"],
            "properties": {"stem": {"type": "string", "minLength": 1}},
        },
        "key_schema": {"type": "object", "required": ["slots"]},
        "response_schema": {
            "type": "object", "required": ["slots"],
            "properties": {
                "slots": {"type": "object",
                          "additionalProperties": {"type": ["string", "null"]}},
            },
        },
        "scoring": {
            "primitive": "choice_per_slot",
            "options": {"option_source": "group.option_bank", "unique_options": True,
                        "aggregate": "per_slot", "points_per_slot": 1},
            "normalizers": ["trim", "casefold"],
        },
        "validation": {},
        "authoring": {"form": [{"field": "stem", "widget": "richtext", "required": True}]},
    }
    body.update(overrides)
    return body


class TestBrowsingADefinition:
    """The screen's read path: the listing for the table, then one definition."""

    def test_the_row_and_the_definition_agree(self, client):
        listed = _ok(client.get("/api/v1/question-types",
                                params={"include_deprecated": True}))
        row = next(t for t in listed if t["key"] == "sentence_completion")
        opened = _ok(client.get(
            f"/api/v1/question-types/{row['key']}/{row['version']}"))
        # Byte for byte the same DTO. The table shows `title`, `skills` and
        # `scoring.primitive` from the listing and the panel shows the schemas
        # from the read — two sources for one screen only works if they agree.
        assert opened == row

    def test_the_definition_carries_the_form_the_authoring_editor_renders(self, client):
        """`TypeForm.tsx` builds a teacher's question form from `authoring.form`.
        The browser shows the same field so the two screens can be compared, and
        so an author of a NEW type can see what a working one declares."""
        opened = _ok(client.get("/api/v1/question-types/sentence_completion/1"))
        assert isinstance(opened["authoring"]["form"], list)
        assert opened["authoring"]["form"], "no form means no generated editor"

    def test_reading_a_definition_needs_no_token(self, client):
        """Which is why the browser half of the screen is shown to a teacher and
        only the add-a-type half is replaced with a refusal. `read_question_type`
        takes no `principal` at all."""
        assert client.get("/api/v1/question-types/sentence_completion/1").status_code == 200

    def test_an_unregistered_type_is_a_404(self, client):
        assert client.get("/api/v1/question-types/not_a_type/1").status_code == 404


class TestTheDryRunComesFirst:
    def test_it_writes_nothing(self, client, db, admin):
        before = db.scalar(text("SELECT count(*) FROM question_type_defs"))
        report = _ok(client.post("/api/v1/admin/question-types/validate",
                                 headers=admin, json=_definition()))
        assert report["passed"] is True
        assert db.scalar(text("SELECT count(*) FROM question_type_defs")) == before

    def test_a_fourth_primitive_is_refused_by_name(self, client, admin):
        """The closed set of three is the limit the screen states up front. The
        server's own answer names the bad value, which is why the screen also
        lists the three that would have worked."""
        report = _ok(client.post(
            "/api/v1/admin/question-types/validate", headers=admin,
            json=_definition(scoring={"primitive": "telepathy"})))
        assert report["passed"] is False
        assert report["error_count"] >= 1
        assert "telepathy" in " ".join(f["message"] for f in report["findings"])

    def test_every_finding_carries_what_the_list_renders(self, client, admin):
        report = _ok(client.post(
            "/api/v1/admin/question-types/validate", headers=admin,
            json=_definition(scoring={"primitive": "telepathy"})))
        for finding in report["findings"]:
            # The screen renders code, severity, message, path and fix_hint.
            assert finding["code"] and finding["message"]
            assert finding["severity"] in ("error", "warning", "info")
            assert "path" in finding and "fix_hint" in finding

    def test_the_report_has_no_run_at_however_the_contract_reads(self, client, admin):
        """`ValidationReport` declares `run_at` REQUIRED and `Report.as_dict`
        never sets it, so the generated TypeScript promises a string the server
        does not send. Pinned so the screen is never written to render it."""
        report = _ok(client.post("/api/v1/admin/question-types/validate",
                                 headers=admin, json=_definition()))
        assert "run_at" not in report

    def test_only_the_first_problem_is_reported(self, client, admin):
        """The house promise is EVERY finding at once, and this endpoint cannot
        keep it: `QuestionTypeDef.from_dict` raises on the first missing field,
        so a definition with three things wrong comes back with one finding.

        The screen still renders the whole list — it is the contract's shape and
        the shape every other validator here returns — but an admin fixing a
        broken definition will go round more than once. Recorded, not fixed."""
        broken = _definition(scoring={"primitive": "telepathy"})
        del broken["key_schema"]
        del broken["response_schema"]
        report = _ok(client.post("/api/v1/admin/question-types/validate",
                                 headers=admin, json=broken))
        assert report["passed"] is False
        assert len(report["findings"]) == 1

    def test_a_teacher_cannot_dry_run_a_definition(self, client, seed):
        refused = client.post("/api/v1/admin/question-types/validate",
                              headers=auth(seed["author"].xid), json=_definition())
        assert refused.status_code == 403


class TestRegistering:
    def test_the_screen_sequence_validates_then_registers(self, client, admin):
        body = _definition()
        assert _ok(client.post("/api/v1/admin/question-types/validate",
                               headers=admin, json=body))["passed"] is True
        created = _ok(client.post("/api/v1/admin/question-types",
                                  headers=admin, json=body), 201)
        assert created["key"] == body["key"]
        # Live with no restart, which is the whole claim. The screen shows the
        # new row by invalidating the listing; this is the same read.
        opened = _ok(client.get(
            f"/api/v1/question-types/{body['key']}/{body['version']}"))
        assert opened["title"] == body["title"]

    def test_a_definition_that_passed_can_still_be_refused(self, client, admin):
        """The dry run does not check for a duplicate `(key, version)`, so a
        clean report is not a promise that registering will succeed. The screen
        renders the refusal through `problemText` rather than assuming."""
        body = _definition()
        _ok(client.post("/api/v1/admin/question-types", headers=admin, json=body), 201)
        again = client.post("/api/v1/admin/question-types", headers=admin, json=body)
        assert again.status_code == 409
        assert again.json()["code"] == "type_version_exists"

    def test_a_bad_definition_never_reaches_the_table(self, client, db, admin):
        before = db.scalar(text("SELECT count(*) FROM question_type_defs"))
        refused = client.post("/api/v1/admin/question-types", headers=admin,
                              json=_definition(scoring={"primitive": "telepathy"}))
        assert refused.status_code == 422
        assert refused.json()["findings"][0]["code"] == "DEFINITION_INVALID"
        assert db.scalar(text("SELECT count(*) FROM question_type_defs")) == before

    def test_a_teacher_cannot_register(self, client, seed):
        refused = client.post("/api/v1/admin/question-types",
                              headers=auth(seed["author"].xid), json=_definition())
        assert refused.status_code == 403


class TestThereIsNoWayBack:
    """What the confirmation on the screen says, checked against the router."""

    @staticmethod
    def _offered(client, needle: str) -> set[tuple[str, str]]:
        """Every method the running application serves under a path.

        Read from the generated document rather than `app.routes`, which in this
        FastAPI version holds opaque `_IncludedRouter` entries with no paths on
        them — a walk over it silently finds nothing, and a guard that silently
        finds nothing passes."""
        document = client.app.openapi()["paths"]
        return {(method.upper(), path)
                for path, methods in document.items() if needle in path
                for method in methods}

    def test_the_api_offers_no_delete_deactivate_or_edit(self, client):
        assert self._offered(client, "question-types") == {
            ("GET", "/api/v1/question-types"),
            ("GET", "/api/v1/question-types/{key}/{version}"),
            ("POST", "/api/v1/admin/question-types"),
            ("POST", "/api/v1/admin/question-types/validate"),
        }, "the screen tells an admin registering cannot be undone from here"

    def test_the_lexicon_offers_no_delete_either(self, client):
        assert self._offered(client, "admin/lexicon") == {
            ("GET", "/api/v1/admin/lexicon"),
            ("POST", "/api/v1/admin/lexicon"),
        }

    def test_a_registered_type_is_absent_from_what_a_restart_would_load(
            self, client, admin):
        """DEFECT, pinned because the screen's copy depends on it.

        `default_registry()` loads `registry/question_types/*.json` from DISK and
        never reads `question_type_defs`, and `register_question_type` makes the
        new type live by mutating the singleton in the process that served the
        request. So the type is live in that worker only, and after a restart no
        worker has it — while `question_versions` still carries a foreign key to
        the row, so content authored against it outlives the definition that
        scores it."""
        from pathlib import Path

        from app.modules.qtypes.registry import REGISTRY_ROOT, Registry
        from app.platform.errors import RegistryError

        body = _definition()
        _ok(client.post("/api/v1/admin/question-types", headers=admin, json=body), 201)
        assert _ok(client.get(
            f"/api/v1/question-types/{body['key']}/{body['version']}"))

        on_boot = Registry.from_directory(Path(REGISTRY_ROOT) / "question_types")
        with pytest.raises(RegistryError):
            on_boot.get(body["key"], body["version"])


class TestTheLexiconRow:
    def test_adding_a_pair_is_one_row(self, client, admin):
        added = _ok(client.post("/api/v1/admin/lexicon", headers=admin,
                                json={"kind": "spelling_variant", "a": "kerbside",
                                      "b": "curbside", "bidirectional": True,
                                      "note": "38 students on Mock 1 Q7"}), 201)
        assert added["a"] == "kerbside"
        listed = _ok(client.get("/api/v1/admin/lexicon", headers=admin))
        mine = next(e for e in listed if e["a"] == "kerbside")
        assert (mine["b"], mine["bidirectional"]) == ("curbside", True)
        assert mine["note"] == "38 students on Mock 1 Q7"

    def test_the_kind_filter_narrows_the_list(self, client, admin):
        every = _ok(client.get("/api/v1/admin/lexicon", headers=admin))
        variants = _ok(client.get("/api/v1/admin/lexicon", headers=admin,
                                  params={"kind": "spelling_variant"}))
        assert {e["kind"] for e in variants} == {"spelling_variant"}
        assert 0 < len(variants) < len(every), "the filter did not narrow anything"

    def test_re_adding_a_pair_reports_success_and_changes_nothing(self, client, admin):
        """DEFECT the screen works around.

        `ON CONFLICT (kind, a, b) DO NOTHING` and a response built from
        `body.model_dump()`, so the second call answers 201 with the note that
        was sent while the stored row keeps the old one. A screen that trusted
        the response would tell an admin their correction was saved."""
        pair = {"kind": "spelling_variant", "a": "kerbside", "b": "curbside"}
        _ok(client.post("/api/v1/admin/lexicon", headers=admin,
                        json={**pair, "note": "first"}), 201)
        echoed = _ok(client.post("/api/v1/admin/lexicon", headers=admin,
                                 json={**pair, "note": "second"}), 201)
        assert echoed["note"] == "second", "the response echoes the request"

        listed = _ok(client.get("/api/v1/admin/lexicon", headers=admin))
        stored = [e for e in listed if e["a"] == "kerbside" and e["b"] == "curbside"]
        assert len(stored) == 1
        assert stored[0]["note"] == "first", "an existing pair is never overwritten"

    def test_an_unknown_kind_is_refused_with_the_field_named(self, client, admin):
        """It was a 500 once. The screen renders `problemText`, which reads the
        findings — so the path is what tells an admin which box was wrong."""
        refused = client.post("/api/v1/admin/lexicon", headers=admin,
                              json={"kind": "spelling", "a": "colour", "b": "color"})
        assert refused.status_code == 422
        assert refused.json()["findings"][0]["path"] == "body.kind"

    def test_a_teacher_can_neither_read_nor_add(self, client, seed):
        who = auth(seed["author"].xid)
        assert client.get("/api/v1/admin/lexicon", headers=who).status_code == 403
        assert client.post("/api/v1/admin/lexicon", headers=who,
                           json={"kind": "spelling_variant", "a": "kerbside",
                                 "b": "curbside"}).status_code == 403


class TestWhatAddingAPairActuallyChanges:
    """DEFECT, pinned because it decides what the screen may promise.

    `default_scorer()` builds its `Lexicon` from `registry/lexicon/*.json` on
    disk — `StaticLexiconSource` is the only implementation of `LexiconSource`
    and nothing in the application reads `lexicon_entries` — so a pair added
    through the admin API is stored, listed, and never consulted when an answer
    is marked. The contract's `201` says "scorer caches invalidate within
    seconds"; nothing invalidates and nothing reads.

    The screen therefore says the entry is recorded and says nothing about when
    marking changes."""

    @staticmethod
    def _verdict(accepted: str, written: str):
        from app.modules.qtypes.registry import ScoreRequest, default_scorer

        return default_scorer().score_item(ScoreRequest(
            type_key="short_answer", type_version=1, payload={},
            key={"slots": {"s1": {"accept": [accepted]}}},
            response={"slots": {"s1": written}},
        )).slots[0].verdict

    def test_a_pair_already_on_disk_is_honoured(self):
        """The control. `short_answer` runs `spelling_uk_us`, and
        `registry/lexicon/spelling_variants.json` carries colour/color — so the
        normalizer chain works and the test below is measuring the source of the
        entries, not a broken setup."""
        from app.modules.qtypes.schemas import Verdict

        assert self._verdict("colour", "color") is Verdict.CORRECT

    def test_a_pair_added_through_the_api_is_not(self, client, admin):
        from app.modules.qtypes.schemas import Verdict

        assert self._verdict("kerbside", "curbside") is Verdict.INCORRECT
        _ok(client.post("/api/v1/admin/lexicon", headers=admin,
                        json={"kind": "spelling_variant", "a": "kerbside",
                              "b": "curbside"}), 201)
        assert any(e["a"] == "kerbside" for e in
                   _ok(client.get("/api/v1/admin/lexicon", headers=admin)))
        assert self._verdict("kerbside", "curbside") is Verdict.INCORRECT
