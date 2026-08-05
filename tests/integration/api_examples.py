"""Recording real API calls and rendering them as documentation.

The examples in `docs/api/student-app.md` are not written by hand. They are the
actual bytes this API sent and received, captured while `test_api_examples.py`
drove the flows against a migrated database — and the same test re-renders them
and fails when the committed document no longer matches.

**That last part is the whole point.** Hand-written API examples are correct on
the day they are written and fiction from the first schema change onwards. This
repository has already learned that lesson twice at a different altitude: a gate
in `make ci` that CI never ran, and a schema check that answered confidently
from an empty database. Documentation nobody re-derives is the same shape.

## Determinism

A recording contains a fresh UUID and the current time on every run, so a naive
byte comparison would fail every second. Everything variable is normalised
here, not by hand in the document:

  * **UUIDs** map to a stable sequence in first-seen order, so the same id used
    in three places still reads as the same id.
  * **Timestamps** are re-based. Anything within a few seconds of the recording
    becomes the base instant; anything further away keeps its DURATION, rounded
    to the minute. `expires_at` stays exactly sixty minutes after `started_at`,
    which is the part a reader is actually checking.
  * **Secrets and digests** — tokens, hashes, phone numbers, slugs, request ids
    — become obvious placeholders. A real one would be noise at best and a
    credential at worst.

The normalisation is deliberately lossy about values and exact about SHAPE and
about relationships between fields. Those are what a mobile developer reads
this document for.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

#: What every re-based timestamp is measured from. A Wednesday morning in
#: Tashkent, chosen so nothing in the document implies a deadline at 3 a.m.
BASE = dt.datetime(2026, 3, 4, 9, 0, 0, tzinfo=dt.UTC)

#: Inside this window of ITS OWN CALL, a timestamp is "now" and collapses to
#: BASE. Ten seconds is generous for one request and nowhere near the shortest
#: real offset in this API, which is the one-minute OTP resend.
#:
#: The reference is per call and not per document, and that distinction is not
#: cosmetic: with a single reference the whole document slid by a minute
#: whenever the run took longer than the window — so it passed alone and failed
#: in the parallel suite, which is the worst way for a gate to fail.
NOW_WINDOW = dt.timedelta(seconds=10)

#: A timestamp in the PAST by less than this was created during the recording —
#: a user, a consent, a queue entry made moments earlier and read back a few
#: calls later. It collapses to BASE.
#:
#: The asymmetry is deliberate and has a reason. A FUTURE offset is a duration
#: this API chose and a reader is checking: the one-minute resend, the
#: two-minute media grant, the hour-long time limit. A PAST offset inside one
#: recording is an artefact of how long the recording took, and preserving it
#: means the document changes with machine load — which is precisely how this
#: gate first failed: green alone, red under `-n 4`.
#:
#: Anything genuinely historical — an assignment that opened an hour ago, an
#: entitlement that started yesterday — is far outside this window and keeps its
#: real distance.
JUST_HAPPENED = dt.timedelta(minutes=10)

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
                  r"(?:Z|[+-]\d{2}:\d{2})?\b")
#: A JWT, and also the media grant, which is `v1.<payload>.<sig>` — a leading
#: version segment the first version of this pattern did not allow, so a real
#: signed grant with a real user id inside it went straight into the document.
_JWT = re.compile(r"\b(?:[A-Za-z0-9_-]{1,8}\.)?eyJ[A-Za-z0-9_-]{10,}"
                  r"\.[A-Za-z0-9_-]{10,}\b")
_HEX = re.compile(r"\b[0-9a-f]{32,}\b")
_PHONE = re.compile(r"\+998\d{9}\b")

#: Keys whose value is never interesting and sometimes sensitive. Replaced
#: wholesale rather than pattern-matched, because a six-digit OTP and a page
#: count look identical to a regex.
OPAQUE_KEYS = {
    "code": "123456",
    "refresh_token": "<refresh-token>",
    "access_token": "<access-token>",
    "token": "<token>",
    "ticket": "<ticket>",
    "slug": "tashkent-prep",
    "grant": "<media-grant>",
    "credential": "<turn-credential>",
    "username": "<turn-username>",
    "request_id": "<request-id>",
}

#: `code` is an OTP in one place and a stable machine-readable error code in
#: another. The error one is the single most useful field in this document, so
#: it is never replaced.
KEEP_CODE_UNDER = {"type", "title", "status", "detail", "instance"}


@dataclass
class Call:
    """One request and its response, as it happened."""

    method: str
    path: str
    status: int
    note: str
    #: When this call happened. Every timestamp in it is re-based against THIS
    #: instant, so how long the rest of the document took cannot move it.
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    request: Any = None
    response: Any = None
    #: Rendered even when the response is empty — a 204 says something.
    headers: dict[str, str] = field(default_factory=dict)


class Recorder:
    """A thin wrapper over `TestClient` that keeps what it saw.

    Deliberately not a mock or a fixture: it makes the real call, returns the
    real response, and the flow that uses it reads like the flow a client would
    perform. A recorder that could not fail the test would be documenting
    something other than this API.
    """

    def __init__(self, client, prefix: str = "/api/v1") -> None:
        self.client = client
        self.prefix = prefix
        self.calls: list[Call] = []

    def call(self, method: str, path: str, *, note: str, json_body: Any = None,
             headers: dict[str, str] | None = None,
             expect: tuple[int, ...] = (200, 201, 202, 204)) -> Any:
        kwargs: dict[str, Any] = {"headers": headers or {}}
        if json_body is not None:
            kwargs["json"] = json_body
        at = dt.datetime.now(dt.UTC)
        response = self.client.request(method, f"{self.prefix}{path}", **kwargs)
        assert response.status_code in expect, (
            f"{method} {path} -> {response.status_code}: {response.text}")
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = f"<{len(response.content)} bytes of {response.headers.get('content-type')}>"
        self.calls.append(Call(method=method.upper(), path=path,
                               status=response.status_code, note=note, at=at,
                               request=json_body, response=body))
        return body


class Normaliser:
    """Variable values in, stable placeholders out.

    One instance per document, so an id that appears in flow 3 and again in
    flow 5 normalises to the same placeholder both times. That continuity is
    most of what makes a multi-step example readable.
    """

    def __init__(self) -> None:
        #: The instant the call being normalised happened. `render` sets it
        #: before each call, so a slow document cannot shift a fast one.
        self.ref = dt.datetime.now(dt.UTC)
        self._uuids: dict[str, str] = {}

    def uuid(self, value: str) -> str:
        if value not in self._uuids:
            n = len(self._uuids) + 1
            # Version 7, variant 8 — the shape this API really issues, so a
            # client parsing the example against a UUID library still passes.
            self._uuids[value] = f"019a0000-0000-7000-8000-{n:012d}"
        return self._uuids[value]

    def timestamp(self, value: str) -> str:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        delta = parsed - self.ref
        if abs(delta) <= NOW_WINDOW or -JUST_HAPPENED <= delta < dt.timedelta(0):
            shifted = BASE
        else:
            # Round to the minute so a 3600s limit reads as exactly one hour and
            # not 59:58. Durations are the thing worth preserving here.
            minutes = round(delta.total_seconds() / 60)
            shifted = BASE + dt.timedelta(minutes=minutes)
        return shifted.isoformat().replace("+00:00", "Z")

    def text(self, value: str) -> str:
        value = _JWT.sub("<token>", value)
        value = _UUID.sub(lambda m: self.uuid(m.group(0)), value)
        value = _ISO.sub(lambda m: self.timestamp(m.group(0)), value)
        value = _PHONE.sub("+998901234567", value)
        value = _HEX.sub("<hash>", value)
        return value

    def value(self, node: Any, *, key: str | None = None,
              siblings: set[str] | None = None) -> Any:
        if isinstance(node, dict):
            keys = set(node)
            return {k: self.value(v, key=k, siblings=keys) for k, v in node.items()}
        if isinstance(node, list):
            return [self.value(v) for v in node]
        if isinstance(node, str):
            if key == "code" and siblings and siblings & KEEP_CODE_UNDER:
                return node                       # an error code, not an OTP
            if key in OPAQUE_KEYS:
                return OPAQUE_KEYS[key]
            return self.text(node)
        return node


def render(title: str, preamble: str, flows: list[tuple[str, str, list[Call]]],
           normaliser: Normaliser) -> str:
    """The document. Markdown, because it is read in a browser and in a diff."""
    out: list[str] = [f"# {title}\n", preamble.strip(), ""]

    out.append("## Contents\n")
    for name, _, _ in flows:
        anchor = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        out.append(f"- [{name}](#{anchor})")
    out.append("")

    for name, blurb, calls in flows:
        out.append(f"## {name}\n")
        out.append(blurb.strip())
        out.append("")
        for index, call in enumerate(calls, start=1):
            normaliser.ref = call.at
            path = normaliser.text(call.path)
            out.append(f"### {index}. {call.note.splitlines()[0]}\n")
            rest = "\n".join(call.note.splitlines()[1:]).strip()
            out.append(f"`{call.method} /api/v1{path}`\n")
            if rest:
                out.append(rest + "\n")
            if call.request is not None:
                out.append("Request:\n")
                out.append("```json")
                out.append(json.dumps(normaliser.value(call.request),
                                      indent=2, ensure_ascii=False))
                out.append("```\n")
            label = f"Response `{call.status}`"
            if call.response is None:
                out.append(f"{label} — no body.\n")
            else:
                out.append(f"{label}:\n")
                out.append("```json")
                out.append(json.dumps(normaliser.value(call.response),
                                      indent=2, ensure_ascii=False))
                out.append("```\n")
    return "\n".join(out).rstrip() + "\n"
