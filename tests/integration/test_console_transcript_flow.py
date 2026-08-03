"""The transcript editor's call sequence, and the thing it exists to make work.

`PUT /audio-tracks/{xid}/transcript` had no screen, so the transcript column on
the Marking panel was always empty and post-exam review of a listening question
showed a timestamp and no words.

The segments below are exactly what `web/src/features/audio/transcript.ts`
produces from a pasted WebVTT file — `{start_ms, end_ms, text, speaker?}` — so
this is the contract between the parser and the endpoint.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import issue_access_token

# What `parseSubtitles` returns for a three-cue WebVTT paste, including the
# speaker lifted out of "NARRATOR: ...".
PARSED = [
    {"start_ms": 0, "end_ms": 4000, "speaker": "NARRATOR",
     "text": "You will hear a conversation in a university library."},
    {"start_ms": 4000, "end_ms": 9000, "text": "I came by bicycle this morning."},
    {"start_ms": 9000, "end_ms": 14000, "text": "The bus would have been faster."},
]


@pytest.fixture
def client(engine, db):
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


class TestTheEditorsSequence:
    def test_an_untranscribed_track_reports_404_not_an_error(
            self, client, seed, with_audio):
        """The ordinary state for a track nobody has transcribed. The editor
        treats it as "nothing yet" rather than as a failure to shout about."""
        response = client.get(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/transcript",
            headers=auth(seed["author"].xid))
        assert response.status_code == 404

    def test_what_the_parser_produces_is_what_the_endpoint_takes(
            self, client, seed, with_audio):
        stored = _ok(client.put(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/transcript",
            headers=auth(seed["author"].xid),
            json={"language": "en", "segments": PARSED}))
        assert len(stored["segments"]) == 3
        assert stored["source"] == "uploaded"
        # The speaker survives the round trip; the two cues without one carry no
        # empty string, because `exclude_none` drops it.
        assert stored["segments"][0]["speaker"] == "NARRATOR"
        assert "speaker" not in stored["segments"][1]

        read_back = _ok(client.get(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/transcript",
            headers=auth(seed["author"].xid)))
        assert read_back["segments"] == stored["segments"]

    def test_the_track_listing_reports_it(self, client, seed, with_audio):
        """`has_transcript` is what the Add/Edit control reads."""
        before = _ok(client.get("/api/v1/audio-tracks",
                                headers=auth(seed["author"].xid)))
        assert before["items"][0]["has_transcript"] is False
        _ok(client.put(f"/api/v1/audio-tracks/{seed['audio_track'].xid}/transcript",
                       headers=auth(seed["author"].xid),
                       json={"language": "en", "segments": PARSED}))
        after = _ok(client.get("/api/v1/audio-tracks",
                               headers=auth(seed["author"].xid)))
        assert after["items"][0]["has_transcript"] is True

    def test_a_second_paste_replaces_rather_than_appends(
            self, client, seed, with_audio):
        """The editor says "pasting below replaces all of them", and it must."""
        track = seed["audio_track"].xid
        _ok(client.put(f"/api/v1/audio-tracks/{track}/transcript",
                       headers=auth(seed["author"].xid),
                       json={"language": "en", "segments": PARSED}))
        _ok(client.put(f"/api/v1/audio-tracks/{track}/transcript",
                       headers=auth(seed["author"].xid),
                       json={"language": "en",
                             "segments": [{"start_ms": 0, "end_ms": 1000,
                                           "text": "Only this."}]}))
        read_back = _ok(client.get(f"/api/v1/audio-tracks/{track}/transcript",
                                   headers=auth(seed["author"].xid)))
        assert [s["text"] for s in read_back["segments"]] == ["Only this."]


class TestWhatTheEditorRefusesBeforeSending:
    """The parser refuses these client-side so the author is told which LINE is
    wrong. Asserted here because the server is the authority — if it ever stopped
    refusing them, the parser would be enforcing a rule nothing else believes."""

    def test_a_reversed_span_is_refused(self, client, seed, with_audio):
        refused = client.put(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/transcript",
            headers=auth(seed["author"].xid),
            json={"language": "en",
                  "segments": [{"start_ms": 5000, "end_ms": 1000, "text": "x"}]})
        assert refused.status_code == 422

    def test_a_segment_with_no_end_is_refused(self, client, seed, with_audio):
        """`exam.session._excerpt` skips a segment missing either bound, so this
        would store fine, read back fine, and contribute nothing to review."""
        refused = client.put(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/transcript",
            headers=auth(seed["author"].xid),
            json={"language": "en", "segments": [{"start_ms": 0, "text": "x"}]})
        assert refused.status_code == 422

    def test_omitting_segments_does_not_wipe_the_transcript(
            self, client, seed, with_audio):
        """The upload is the only copy; nobody re-types a listening transcript."""
        track = seed["audio_track"].xid
        _ok(client.put(f"/api/v1/audio-tracks/{track}/transcript",
                       headers=auth(seed["author"].xid),
                       json={"language": "en", "segments": PARSED}))
        refused = client.put(f"/api/v1/audio-tracks/{track}/transcript",
                             headers=auth(seed["author"].xid), json={"language": "en"})
        assert refused.status_code == 422
        still = _ok(client.get(f"/api/v1/audio-tracks/{track}/transcript",
                               headers=auth(seed["author"].xid)))
        assert len(still["segments"]) == 3


class TestOnlyStaffMayReadIt:
    def test_a_student_at_the_centre_cannot(self, client, seed, with_audio):
        """The transcript is the answer sheet. `scoped()` admits every member of
        the owning org, so read-scope alone would hand a student every answer in
        the paper they are about to sit."""
        _ok(client.put(f"/api/v1/audio-tracks/{seed['audio_track'].xid}/transcript",
                       headers=auth(seed["author"].xid),
                       json={"language": "en", "segments": PARSED}))
        refused = client.get(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/transcript",
            headers=auth(seed["student"].xid))
        assert refused.status_code in (403, 404)


class TestTheTranscriptReachesTheMarkingPanel:
    """Why any of this exists. Without a transcript the Results screen's Marking
    panel shows an audio timestamp and no words, which answers "why was I marked
    wrong" with the moment and not the sentence.

    The transcript arrives through the endpoint the EDITOR calls, not a direct
    INSERT — so this covers the console's whole path rather than the domain join
    `test_exam_endpoints` already proves.
    """

    def test_a_transcript_uploaded_from_the_console_reaches_review(
            self, client, db, seed, with_audio, entitled, clock):
        from sqlalchemy import text

        from app.modules.content import repo as content_repo

        _ok(client.put(f"/api/v1/audio-tracks/{seed['audio_track'].xid}/transcript",
                       headers=auth(seed["author"].xid),
                       json={"language": "en", "segments": PARSED}))

        # Asked about 5-8s, INSIDE the 4000-9000 segment rather than aligned to
        # it: on a boundary, overlap and containment agree and the distinction
        # that matters goes untested.
        db.execute(text("UPDATE test_version_groups SET audio_start_ms = 5000, "
                        "audio_end_ms = 8000"))
        db.execute(text("UPDATE test_version_sections SET skill = 'listening'"))
        db.flush()
        # Published AFTER the range is set: review reads the snapshot the attempt
        # was served, and a published version cannot go back to draft to be
        # rebuilt — the database refuses it.
        content_repo.publish(db, seed["test_version"].id, seed["author"].id,
                             clock.now())
        db.flush()

        student = auth(seed["student"].xid)
        attempt = _ok(client.post("/api/v1/attempts", headers=student, json={
            "test_version_xid": str(seed["test_version"].xid)}), 201)["xid"]
        paper = _ok(client.get(f"/api/v1/attempts/{attempt}/payload", headers=student))
        q = paper["sections"][0]["groups"][0]["questions"][0]
        _ok(client.post(f"/api/v1/attempts/{attempt}/answers", headers=student, json={
            "deltas": [{"question_version_xid": q["question_version_xid"],
                        "slot_key": q["slot_keys"][0],
                        "response": "bicycle", "client_seq": 1}]}))
        _ok(client.post(f"/api/v1/attempts/{attempt}/submit", headers=student))

        item = _ok(client.get(f"/api/v1/attempts/{attempt}/review",
                              headers=student))["items"][0]
        assert item["audio_range"] == {"start_ms": 5000, "end_ms": 8000}
        # Cut by OVERLAP, so the sentence that began before the marker and
        # carries the answer is included rather than clipped out of it.
        assert "bicycle" in (item["transcript_excerpt"] or "")

        # Staff read the same field on ASSIGNED work — this attempt is self-serve
        # practice, which no staff member may open, so the Marking panel's
        # transcript column is exercised in `test_exam_endpoints`. What is proved
        # here is that a transcript uploaded through the console populates it at
        # all, which it could not before.
        assert client.get(f"/api/v1/attempts/{attempt}/review",
                          headers=auth(seed["author"].xid)).status_code == 404
