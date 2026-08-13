#!/usr/bin/env python3
"""Does every route USE the path parameters it declares?

A path parameter is the strongest statement a route can make about what varies:
`/internal/storage/{bucket}/{key}` says the bucket decides which object you get.
When the handler then never reads it, the URL is making a promise the code does
not keep — and unlike an unused local, this one is reachable by anyone who can
type a URL.

**That is not hypothetical; it is why this file exists.** `get_object` took
`bucket` and served every request out of the CONFIGURED bucket, and
`GET /media/{xid}/content` resolved the same way while every media row recorded
the bucket its object actually lives in. An object stored anywhere else streamed
zero bytes behind a correct `Content-Length`, which reaches the client as a
truncated 200 and the operator as a uvicorn protocol error — a failure with no
name and no obvious cause. It cost a debugging session to find.

── why not just turn on ruff's ARG rules ───────────────────────────────────────

`ruff --select ARG` does catch this exact case, and it also raises 38 other
findings across `app/`, nearly all of them correct-by-design: a normalizer that
takes a context it does not need, a dramatiq actor whose signature is fixed by
the broker, a protocol conformance stub. Enabling it would mean 38 suppressions,
and a gate whose output is mostly suppressions is a gate people stop reading.

The claim here is narrower and therefore worth failing a build over: a path
parameter is declared in the ROUTE, so it is never a signature somebody else
fixed. If it is unused, it is dead or it is a bug.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTERS = ROOT / "app" / "api" / "routers"

PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[^}]+)?\}")
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Each entry is a decision, not a suppression. Keep it short and keep the reason.
EXEMPT: set[str] = set()


def routes(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """The path templates this function is mounted at."""
    found = []
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        func = decorator.func
        if not isinstance(func, ast.Attribute) or func.attr not in METHODS:
            continue
        if decorator.args and isinstance(decorator.args[0], ast.Constant):
            value = decorator.args[0].value
            if isinstance(value, str):
                found.append(value)
    return found


def names_used(node: ast.AST) -> set[str]:
    """Every identifier the body reads, including inside f-strings."""
    used: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            used.add(child.id)
        # `bucket=bucket` in a call, and attribute bases, are Names already.
    return used


def main() -> int:
    offenders: list[str] = []
    checked = 0

    for path in sorted(ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text())
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            templates = routes(node)
            if not templates:
                continue
            declared: set[str] = set()
            for template in templates:
                declared |= set(PLACEHOLDER.findall(template))
            if not declared:
                continue
            checked += 1

            # The body only — the signature's own annotations and defaults do not
            # count as using a parameter.
            body = ast.Module(body=node.body, type_ignores=[])
            used = names_used(body)
            for name in sorted(declared - used):
                where = f"{rel}:{node.name}:{name}"
                if where in EXEMPT:
                    continue
                offenders.append(
                    f"  {rel}:{node.lineno} {node.name}() declares "
                    f"{{{name}}} in its route and never reads it")

    if offenders:
        print("Route path parameters that are declared and ignored:\n")
        print("\n".join(offenders))
        print("\nEither use it or take it out of the path. A URL segment the "
              "handler does not read is a promise the code does not keep.")
        return 1

    print(f"path params ok ({checked} routes with parameters)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
