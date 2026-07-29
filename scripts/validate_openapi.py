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


if __name__ == "__main__":
    sys.exit(main())
