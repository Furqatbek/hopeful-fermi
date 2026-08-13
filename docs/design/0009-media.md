# Media storage and the transcode worker

- **Status:** Proposed — awaiting review
- **Date:** 2026-07-29
- **Continues:** `0008-workers.md`
- **Closes:** open item 1 of that document (media storage, the audio transcode
  worker), and the `_sign_grant` placeholder carried since `0006`.

---

## 0. What is delivered

Listening now works end to end: a teacher uploads a WAV, it is normalised and
transcoded, and a student streams it under a per-user grant.

```
unit         340 passed          (no database, no ffmpeg needed)
integration  406 passed          (real PostgreSQL, real ffmpeg, real bytes)
──────────────────────────────
total        746 passed in 6m45s

import contracts   8 kept, 0 broken
openapi            PASS  113 paths / 149 operations
coverage           149/149 (100%)
migrations         0001..0019 apply, downgrade to base, and re-apply
```

Verified against real processes, not just the test suite: a 1.5 MB WAV at
−25.5 LUFS uploaded through HTTP, relayed through Redis, and transcoded by a
real `dramatiq` worker into a 67 KB mono AAC file at **−16.00 LUFS**. A 23×
reduction, on target.

| File | Lines | What it is |
|---|---|---|
| `platform/storage.py` | 387 | The port from ADR-0001 §5.4. The only place boto3 appears. |
| `platform/audio.py` | 277 | ffmpeg as a subprocess. Probe, measure, validate, transcode. |
| `platform/grants.py` | 166 | Short-TTL per-user HMAC grants. Replaces a placeholder. |
| `modules/content/media.py` | 521 | Upload, ingest, delivery resolution, attestations. |
| `api/routers/object_storage.py` | 67 | Presigned-URL target for the file backend. **A production route** — see below. |

---

## 1. Bytes never touch the app server

The client PUTs parts straight to object storage using presigned URLs; we hand
out the URLs and record what comes back. On a 4 vCPU box shared with the exam
endpoints, proxying a 40 MB WAV through gunicorn during a mock is how one
teacher's upload becomes forty students' timeouts.

That decision forces the rest of the design:

**Validation happens at OPEN, not at complete.** Format, size and attestation are
checked before a single byte is sent. Rejecting a 40 MB file after it arrives, on
a connection the teacher pays for by the megabyte, is the kind of thing that
loses a centre. All findings come back at once, like the publish gate.

**Resume returns only the OUTSTANDING parts.** Re-sending URLs for parts already
stored invites a client to upload them again on a metered connection. The
contract now says so.

**`FileStorage` implements the same protocol including multipart.** Parts land as
separate files and are concatenated on complete, so the resumable-upload code the
test suite exercises is the code that runs against S3 — not a simplified path
that happens to pass. Its presigned URLs point at a route mounted in every environment,
when `storage_backend == "file"`, signed with the same HMAC an S3 presigned URL
carries, and absent from the OpenAPI document.

`TestProtocolConformance` compares the two backends' method signatures, so one
gaining a parameter the other lacks fails the build rather than production.

---

## 2. Two-pass loudness normalisation, and the test that proves it

**−16 LUFS integrated, mono, AAC in MP4 at 64 kbps.**

IELTS listening is a timed exam. A student who stops to adjust the volume between
sections has lost time to our engineering, so every delivery file leaves at the
same level.

`loudnorm` run once is a dynamic compressor whose output loudness is
approximate. Run as measure-then-apply with `linear=true` it is a precise linear
gain. The first pass costs a few seconds of CPU.

**The test I first wrote did not prove this.** It normalised a steady sine tone
and asserted the output was within 0.5 LUFS of target — which single-pass also
achieves, because a steady tone has no dynamics to compress. I only found that by
deliberately reverting to single-pass and watching the test still pass.

The fixture is now quiet AND dynamic (alternating loud and soft, standing in for
two speakers at different levels), and the assertion is on **loudness range**:

| | integrated | LRA |
|---|---|---|
| source | −27.19 | 3.50 |
| two-pass | **−16.00** | **3.60** |
| single-pass | −15.93 | 4.10 |

Both hit the level. Only two-pass preserves the range. Compressing an exam
recording changes the relative levels of its speakers — a content change nobody
asked for, which no author would notice until a student said they could not hear
one of them. Reverting to single-pass now fails the test.

**Codec choice: AAC over Opus.** Opus at 32 kbps would halve the bytes, and every
byte is origin egress on the proxy path. But Safari's Opus support arrived
recently and unevenly, and a listening section that will not play is a refund. A
30-minute section is about 14 MB as AAC.

**Metadata is stripped** (`-map_metadata -1`). An uploaded WAV can carry the
teacher's name, their software licence, sometimes a path from their laptop. A
test asserts none of it survives into the delivery file.

**`+faststart`** moves the index to the front so playback begins before the whole
file has arrived — the difference between instant and a ten-second stare on 3G.

---

## 3. Grants: the placeholder is gone

`_sign_grant` was a SHA-256 of `(attempt_xid, section_id, timestamp)`. Every input
is known to the client, so anyone could compute a valid grant. It verified
nothing.

A grant is now an HMAC over the app secret binding four things:

| Binding | Dropping it means |
|---|---|
| user | a grant from one student's devtools is a download link for the class |
| object | a practice-track grant opens the exam audio |
| expiry | "short-TTL" is a comment |
| purpose | a review-mode grant replays a play-once exam section |

The key is derived with a fixed label (`media-grant:<secret>`), so a grant and a
session JWT are not interchangeable even though both come from `jwt_secret` — a
bug in one must not become an authentication bypass in the other. A test asserts
a grant does not decode as a JWT.

`verify` raises rather than returning a boolean. A function returning False is
one an exhausted caller wraps in `if not verify(...)` and inverts.

**A grant naming a suspended account is refused.** It lives for two minutes, and a
safety ban must take effect inside those two minutes rather than after them.

This is not DRM and does not pretend to be. A determined student can record their
screen. The goal is that *ordinary* replay is impossible and unusual behaviour
leaves a trace — a goal that can actually be met.

---

## 4. Delivery: proxy or redirect, by config

| | `proxy` (default) | `redirect` |
|---|---|---|
| Bytes | through this process | 302 to a presigned object URL |
| CDN cache | zero, by construction | zero, but no origin egress |
| Binding | per-user for every byte | grant gates the redirect; the URL after it is not user-bound |
| Audit | complete | the redirect only |

Deliverable 3 committed to per-user tokenisation at ~20 GB/month and named
~500 GB/month as the point to revisit. `redirect` is that revisit, and it is a
config change rather than a rewrite.

The proxy path is `async def` — the exception ADR-0001 §5.7 carved out for media
streaming. A 14 MB section held open for four minutes on a sync handler occupies
a threadpool worker for four minutes, and forty students doing that is the whole
pool. Blocking storage reads are pushed to a thread so one slow client cannot
stall the loop every other stream shares.

**Range requests are supported.** Without them an `<audio>` element cannot seek,
and on iOS Safari it will not play at all. Open-ended (`bytes=1000-`) and suffix
(`bytes=-500`) forms both, because that is what real players send.

`Cache-Control: no-store`, not `private`: a shared device in a computer lab must
not keep an exam section in its disk cache after the student logs out.

**An author's xid names the MASTER.** Delivery follows `derived_from_id` to the
transcoded file, so a student asking for the master receives the 67 KB m4a rather
than the 1.5 MB WAV. Tested.

---

## 5. Copyright: evidence, not a checkbox

The brief said to assume some centre will upload a published Cambridge paper.

Every upload records an attestation row carrying the claim, the statement's
**version and SHA-256 hash**, the uploader, the time, the IP and a user-agent
hash. "They ticked a box" is not a defence; "they ticked THIS box, whose text
hashed to X" is.

Two rows are written per upload — one against the media asset, one against the
audio track — because a takedown is filed against a *track*, and the evidence has
to be reachable from what the claimant names.

**A missing attestation is refused, never defaulted.** Quietly defaulting to
`original` manufactures a claim the uploader never made, which is the opposite of
evidence. `licensed` additionally requires a note saying what the licence is.

---

## 6. Five defects the tests caught

### 6.1 The play-once lock was burned on sections with no audio

The old placeholder returned a grant unconditionally. So calling
`POST /attempts/{xid}/sections/{n}/audio-grant` on a **reading** section returned
a meaningless token *and consumed the student's single play* — a stray client
call cost them the section, with no audio involved anywhere.

The check now runs before any mutation and returns `section_has_no_audio`.

This surfaced because two existing play-once tests started failing: they had been
using the seeded reading paper as a stand-in, which only worked while the grant
was a hash of the section id. They now attach a real audio track — which is what
they were always meant to be testing — and a third test asserts the lock survives
a call to a section with no audio.

### 6.2 `audio_tracks.loudness_lufs` was missing from the ORM

The column exists in the schema and the OpenAPI contract documents it
("Measured at ingest; wildly quiet uploads are rejected"), and the mapping did
not have it — so the API could never have reported it, whatever the worker wrote.

### 6.3 The presigned-part key did not match the contract

The contract says `n`; the implementation emitted `part`. Caught by reading the
spec rather than by a test, which is an argument for `check_api_coverage.py`
growing a shape check as well as a path check. `offset`, `length`, `media_xid`
and `status` were genuinely useful additions, so the spec now documents them.

### 6.4 An unparseable IP lost the whole attestation

`content_attestations.ip` is `inet`, and `request.client.host` is `"testclient"`
under the test client — and can be anything a proxy puts in a header in
production. The insert failed outright, taking the attestation with it. The claim
is the evidence; the address is context, and it is now dropped if it will not
parse rather than losing the row.

### 6.5 The event-route guard did not cover the new emitter

`test_every_emitted_event_type_has_a_route` scans the source for emitted event
types. `media.uploaded` is emitted through a helper whose shape the patterns did
not match, so a missing route would not have been caught. The scan now covers
three emitter shapes and asserts a floor count plus the presence of
`media.uploaded`, so the patterns silently matching nothing cannot pass.

---

## 7. Still open

1. **No MinIO in the test suite.** `S3Storage` is exercised by the signature
   conformance test and by nothing else. That is a deliberate line — a container
   dependency in the unit suite is a cost every contributor pays forever — but it
   means the first real S3 deploy is the first real test of that class. Standing
   up MinIO in CI is a couple of hours and should happen before production.
   → **Closed in `0011-ci.md` §9.** MinIO runs in CI, not on laptops, so the line
   held. It found two divergences immediately, both of them botocore exceptions
   escaping the one module that exists to contain them — and one of those made
   `stat()` report every object as missing during an outage.

2. **`processing_error` is shown but not translated.** An author gets "This file
   is silent or almost silent" in English. Every other transactional string is in
   a catalog; these are not, and uz-Latn and ru are non-negotiable in this market.
   More strings arrived with the integrity checks (`0011-ci.md` §24), which makes
   this slightly worse and no more urgent.

   → Those integrity checks close a gap this document did not know it had:
   `expected_bytes` and `received_bytes` were both recorded and never compared, so
   an upload that dropped after part 1 became a `ready` listening section at
   whatever length happened to arrive. Both are now checked in `ingest_audio`,
   before ffmpeg runs, along with the client's declared `checksum_sha256` — which
   the contract had asked for since it was written and the router discarded.

3. **No image pipeline.** `ALLOWED_IMAGE` and the size limits exist and diagram
   uploads validate, but nothing resizes or strips EXIF from an image. EXIF can
   carry GPS coordinates, which for a photograph taken in a classroom is a
   safeguarding question, not a bandwidth one. Small, and it should land before
   diagram-based question types are used in anger.

4. **Transcode has no dead-letter surface.** A file that fails ffmpeg twice is
   marked `failed` with a reason the author sees, which is right. A file that
   fails for an *unexpected* reason exhausts its Dramatiq retries and is visible
   only in the worker log — it is not in `outbox_stuck` because the event was
   dispatched successfully. An `assets stuck in processing` count belongs in the
   health snapshot.

5. **The scratch directory is not bounded.** `media_scratch_dir` is configurable
   and the worker cleans up in a `finally`, but two concurrent 400 MB masters plus
   their outputs is 1 GB of transient disk with nothing checking there is room.
   A pre-flight free-space check is a few lines and prevents a confusing failure.

6. **Delivery is one format.** No adaptive bitrate, no fallback codec. If a
   student's browser cannot play AAC-in-MP4 they get nothing. Acceptable — the
   combination is essentially universal — but it is a single point of failure
   with no graceful degradation.

7. **The integration suite is 6m45s.** The `CREATE DATABASE ... TEMPLATE`
   optimisation has been overdue for two documents now, and the media tests add
   real ffmpeg work on top. It should be the next thing, ahead of features.
   → **Closed in `0010-test-suite-speed.md`**, though not by the template: the
   per-test `TRUNCATE` was the cost, and it is now a 2 ms `DELETE`. 1m18s serial,
   32s under `-n 4`. The ten ffmpeg tests are 14 s of that and stay.
