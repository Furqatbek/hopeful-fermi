#!/usr/bin/env python3
"""Validate the OpenAPI document beyond schema conformance.

`openapi-spec-validator` passing is necessary but not sufficient: OpenAPI 3.1
schemas are JSON Schema 2020-12, which ignores unknown keywords silently. A
stray `nullable: true` (3.0 syntax) validates fine and then makes every client
generator emit a non-nullable type. Hence the extra checks below.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from openapi_spec_validator import validate

SPEC = Path(__file__).resolve().parents[1] / "openapi" / "openapi.yaml"
METHODS = ("get", "post", "patch", "put", "delete")


def main() -> int:
    text = SPEC.read_text()
    spec = yaml.safe_load(text)
    problems: list[str] = []

    validate(spec)

    # 3.0 leftovers that 3.1 silently ignores
    for kw in ("nullable:", "example:\n", "exclusiveMinimum: true", "exclusiveMaximum: true"):
        if kw == "nullable:" and kw in text:
            problems.append("`nullable:` is OpenAPI 3.0; use `type: [x, 'null']`")

    refs = set(re.findall(r"\$ref: '#/components/(\w+)/([A-Za-z0-9_]+)'", text))
    for section, name in sorted(refs):
        if name not in (spec["components"].get(section) or {}):
            problems.append(f"dangling $ref components/{section}/{name}")

    declared_tags = {t["name"] for t in spec.get("tags", [])}
    for path, item in spec["paths"].items():
        names = set(re.findall(r"\{(\w+)\}", path))
        shared = _param_names(spec, item.get("parameters", []))
        for method, op in item.items():
            if method not in METHODS:
                continue
            where = f"{method.upper()} {path}"
            for field in ("tags", "summary", "responses"):
                if not op.get(field):
                    problems.append(f"{where}: missing {field}")
            for tag in op.get("tags") or []:
                if tag not in declared_tags:
                    problems.append(f"{where}: undeclared tag {tag!r}")
            missing = names - (shared | _param_names(spec, op.get("parameters", [])))
            if missing:
                problems.append(f"{where}: undeclared path params {sorted(missing)}")

    # Schemas unreachable from any path are fine ONLY for realtime event payloads.
    used = {n for s, n in refs if s == "schemas"}
    orphans = sorted(set(spec["components"]["schemas"]) - used)
    for name in orphans:
        if not name.startswith("Rt"):
            problems.append(f"schema {name} is unreachable and is not a realtime payload")

    # Prose that a comma split out of a description.
    #
    # `{ type: integer, description: Test-wide IELTS numbering, computed at
    # composition time. }` is a YAML FLOW mapping, so the comma ends the
    # description and `computed at composition time.` becomes a KEY with a null
    # value — sitting inside an OpenAPI Schema Object, where it is not valid.
    # Two things break quietly: the description loses the half after the comma,
    # which is reliably the load-bearing half ("never from the device clock",
    # "never by the reporter"), and a code generator emits the junk key.
    #
    # Eleven of these existed. PyYAML parses them without complaint, this script
    # did not look, and nothing else in the pipeline reads descriptions at all —
    # so the only reason they surfaced is that somebody generated a real client.
    # No OpenAPI keyword contains a space, which makes the check one line.
    for where, key in _keys_with_spaces(spec.get("components", {}), "components"):
        problems.append(f"{where}: {key!r} is a key, not prose — quote the "
                        "description above it; a comma in a flow mapping ended it")

    if problems:
        print("FAIL")
        for p in problems:
            print(f"  - {p}")
        return 1

    ops = sum(1 for i in spec["paths"].values() for m in i if m in METHODS)
    print(f"PASS  paths={len(spec['paths'])} operations={ops} "
          f"schemas={len(spec['components']['schemas'])} realtime_events={len(orphans)}")
    return 0


def _param_names(spec: dict, params: list) -> set[str]:
    out = set()
    for p in params:
        if "$ref" in p:
            out.add(spec["components"]["parameters"][p["$ref"].rsplit("/", 1)[-1]]["name"])
        elif p.get("in") == "path":
            out.add(p["name"])
    return out



def _keys_with_spaces(node, path: str):
    """Every mapping key containing a space, with where it lives.

    Scoped to `components` on purpose: `paths` legitimately holds keys with
    spaces nowhere, but a media type like `multipart/form-data` and a path
    template are easy to confuse a broader walk with, and the defect this exists
    for lives in schema objects.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and " " in key:
                yield path, key
            yield from _keys_with_spaces(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _keys_with_spaces(value, f"{path}[{index}]")


if __name__ == "__main__":
    sys.exit(main())
