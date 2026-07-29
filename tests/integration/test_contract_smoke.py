"""Every operation in the contract is reachable and none of them 500.

This is a crash net, not a behaviour test. It drives all 149 operations with a
valid token and a minimally-shaped body, and asserts only one thing: nothing
answers 5xx. A 404, 403, 409 or 422 from garbage input is a correct answer.

It earns its place because the bugs it catches are invisible to a behaviour
suite — a raw-SQL `WHERE xid = :x` bound to a *string* against a `uuid` column
raises `operator does not exist: uuid = character varying`, which looks fine in
review, passes every unit test, and 500s the first time a user opens the page.
Ten endpoints had exactly that defect when this file was written.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.deps import issue_access_token

ROOT = Path(__file__).resolve().parents[2]
SPEC = yaml.safe_load((ROOT / "openapi" / "openapi.yaml").read_text())
METHODS = ("get", "post", "put", "patch", "delete")

# The payment callbacks speak their providers' protocols, not ours: Payme is
# JSON-RPC with numeric error codes in the body and Click is form-encoded with a
# signature. Both are covered by their own tests rather than by generated input.
SKIP = {("post", "/payments/payme"), ("post", "/payments/click/prepare"),
        ("post", "/payments/click/complete")}


@pytest.fixture
def client(engine, db):
    from app.api import deps
    from app.api.main import create_app

    app = create_app()
    app.dependency_overrides[deps.db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _resolve(schema: dict) -> dict:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = SPEC["components"]["schemas"][name]
    if "allOf" in schema:
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            part = _resolve(part)
            merged["properties"].update(part.get("properties", {}))
            merged["required"].extend(part.get("required", []))
        return merged
    return schema


def _sample(schema: dict) -> Any:
    """A minimal value of the right SHAPE for a schema.

    Deliberately not a valid value — the point is to get past request parsing and
    into the handler, where the crashes live. Semantic rejection afterwards is
    the expected outcome, not a failure.
    """
    schema = _resolve(schema)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    if "enum" in schema:
        return schema["enum"][0]
    if kind == "object":
        return {name: _sample(sub)
                for name, sub in (schema.get("properties") or {}).items()
                if name in (schema.get("required") or [])}
    if kind == "array":
        return []
    if kind == "integer":
        return schema.get("minimum", 1)
    if kind == "number":
        return 1
    if kind == "boolean":
        return False
    fmt = schema.get("format")
    if fmt == "uuid":
        return str(uuid.uuid4())
    if fmt == "date-time":
        return "2026-08-01T09:00:00Z"
    if fmt == "date":
        return "2026-08-01"
    return "x"


def _operations() -> list[tuple[str, str, dict]]:
    out = []
    for path, item in SPEC["paths"].items():
        for method, op in item.items():
            if method in METHODS and (method, path) not in SKIP:
                out.append((method, path, op))
    return sorted(out)


OPERATIONS = _operations()


def test_the_suite_covers_the_whole_contract():
    assert len(OPERATIONS) + len(SKIP) == sum(
        1 for item in SPEC["paths"].values() for m in item if m in METHODS)
    assert len(OPERATIONS) > 140, "the contract shrank; check the spec loaded"


@pytest.mark.parametrize("method,path,op",
                         OPERATIONS, ids=[f"{m.upper()} {p}" for m, p, _ in OPERATIONS])
def test_no_operation_returns_5xx(client, db, seed, admin_auth, method, path, op):
    real = (path.replace("{xid}", str(seed["test_version"].xid))
                .replace("{key}", "sentence_completion")
                .replace("{version}", "1")
                .replace("{position}", "1")
                .replace("{job_xid}", str(uuid.uuid4())))

    kwargs: dict[str, Any] = {"headers": dict(admin_auth)}
    body = op.get("requestBody", {}).get("content", {})
    if "application/json" in body:
        kwargs["json"] = _sample(body["application/json"]["schema"])
    elif "multipart/form-data" in body:
        schema = _resolve(body["multipart/form-data"]["schema"])
        data, files = {}, {}
        for name, sub in (schema.get("properties") or {}).items():
            if _resolve(sub).get("format") == "binary":
                files[name] = ("a.bin", b"x", "application/octet-stream")
            elif name in (schema.get("required") or []):
                data[name] = _sample(sub)
        kwargs["data"], kwargs["files"] = data, files or None

    savepoint = db.begin_nested()
    try:
        response = client.request(method.upper(), "/api/v1" + real, **kwargs)
        assert response.status_code < 500, (
            f"{method.upper()} {real} -> {response.status_code}: {response.text[:400]}")
    finally:
        try:
            savepoint.rollback()
        except Exception:                                     # pragma: no cover
            pass


@pytest.fixture
def admin_auth(db, seed):
    """A platform admin, so authorization never masks a crash further in.

    A 403 at the door would make this suite pass by never reaching the code it
    exists to exercise.
    """
    from app.modules.identity.models import PlatformRoleGrant

    db.add(PlatformRoleGrant(user_id=seed["author"].id, role="platform_admin",
                             granted_by=seed["author"].id))
    db.flush()
    return {"Authorization": f"Bearer {issue_access_token(str(seed['author'].xid))}"}
