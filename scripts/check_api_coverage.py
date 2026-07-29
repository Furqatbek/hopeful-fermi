#!/usr/bin/env python3
"""Compare the OpenAPI contract against the routes the app actually serves.

A contract nobody checks drifts. This prints, and fails on, three things:
  * paths in the spec with no route      (promised, not built)
  * routes with no path in the spec      (built, not promised — usually a typo)
  * method mismatches on a shared path

Run with --list to print the gap without failing, which is how it is used while
a phase is still in progress.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
METHODS = ("get", "post", "patch", "put", "delete")
PREFIX = "/api/v1"


def spec_ops() -> set[tuple[str, str]]:
    spec = yaml.safe_load((ROOT / "openapi" / "openapi.yaml").read_text())
    return {(m.upper(), p) for p, item in spec["paths"].items()
            for m in item if m in METHODS}


def app_ops() -> set[tuple[str, str]]:
    """Read the app's own generated OpenAPI rather than walking `app.routes`.

    Recent FastAPI wraps included routers in a lazy `_IncludedRouter` that is not
    flattened into `app.routes`, so walking that list silently reports zero
    coverage. The generated document is the authoritative list of what is served
    and does not depend on framework internals.
    """
    sys.path.insert(0, str(ROOT))
    from app.api.main import create_app

    generated = create_app().openapi()
    out: set[tuple[str, str]] = set()
    for path, item in generated.get("paths", {}).items():
        if not path.startswith(PREFIX):
            continue
        # The spec is written without the server prefix.
        normalised = path[len(PREFIX):] or "/"
        for method in item:
            if method in METHODS:
                out.add((method.upper(), normalised))
    return out


def main() -> int:
    listing = "--list" in sys.argv
    spec, app = spec_ops(), app_ops()
    missing = sorted(spec - app)
    extra = sorted(app - spec)

    print(f"spec operations : {len(spec)}")
    print(f"app  operations : {len(app)}")
    print(f"implemented     : {len(spec & app)}/{len(spec)} "
          f"({100 * len(spec & app) // len(spec)}%)")

    if missing:
        print(f"\nNOT IMPLEMENTED ({len(missing)}):")
        for method, path in missing:
            print(f"  {method:6} {path}")
    if extra:
        print(f"\nNOT IN THE CONTRACT ({len(extra)}):")
        for method, path in extra:
            print(f"  {method:6} {path}")

    if listing:
        return 0
    return 1 if (missing or extra) else 0


if __name__ == "__main__":
    sys.exit(main())
