"""Playing audio, previewing a listening paper, reading a score, and the two
tidy-up endpoints beside them — every request in the order a screen makes it.

Four screens, and each of them was missing the one call that made the rest of it
worth having:

  * the audio library could upload a 400 MB wav, watch it transcode and read its
    measured loudness, and never play a second of it, because the only issuer of
    a media grant lived inside an exam attempt;
  * the preview screen rendered every section type except the one whose content
    cannot be checked by reading it;
  * the results screen showed a band with no way to see that somebody had moved
    it;
  * cancelling an upload walked away from an open multipart, and a regrade with
    no key fix behind it — a retuned band map, an engine fix — could not be
    staged at all.

The grant tests carry the weight. A media grant is a working download link for a
listening paper, and the properties that keep it from becoming one — bound to a
user, bound to an object, dead in two minutes, never a stable URL — are exactly
the properties a UI is able to break by holding on to one.
"""

from __future__ import annotations

import datetime as dt
import os
import urllib.parse
import uuid as _uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import issue_access_token


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


def media_url(issued: dict) -> str:
    """The URL `player.mediaUrl` builds, character for character.

    Written out rather than interpolated loosely because the grant is base64url
    plus dots and the client encodes it — a test that skipped the encoding would
    pass while the browser sent something else.
    """
    grant = urllib.parse.quote(issued["grant"], safe="")
    return f"/api/v1/media/{issued['media_xid']}/content?grant={grant}"


@pytest.fixture
def store(tmp_path):
    """A file-backed store rooted in this test's own directory.

    Installed process-wide because the API resolves `storage()` itself, and put
    back afterwards so a later test in the same session is not uploading into a
    deleted temporary directory.
    """
    from app.platform.storage import FileStorage, set_storage

    backend = FileStorage(root=tmp_path / "media", bucket="test-media")
    set_storage(backend)
    yield backend
    set_storage(None)


class TestPlayingBackWhatWasUploaded:
    """`AudioLibrary` → `TrackPlayer`: request a grant, put it on an `<audio>`."""

    def test_the_two_calls_the_player_makes(self, client, seed, with_audio):
        issued = _ok(client.post(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/grant",
            headers=auth(seed["author"].xid)))
        assert set(issued) >= {"grant", "media_xid", "expires_at"}

        # No Authorization header, deliberately: a media element cannot set one,
        # which is the entire reason the grant travels in the query string. A
        # test that sent a bearer token here would prove nothing about the thing
        # the browser actually does.
        streamed = client.get(media_url(issued))
        assert streamed.status_code == 200, streamed.text
        # The BYTES, not just the status. Every assertion in this class used to
        # read a header computed from the database row, so the whole file was
        # green while the endpoint delivered an empty body — see `with_audio`.
        assert streamed.content == bytes(range(256)) * 16
        assert streamed.headers["accept-ranges"] == "bytes"
        # Never cached. A shared classroom machine must not keep an exam section
        # on disk after the student who played it logs out.
        assert streamed.headers["cache-control"] == "no-store"

    def test_seeking_works_because_ranges_do(self, client, seed, with_audio):
        """An `<audio>` element cannot seek without range support, and on iOS
        Safari it will not begin playback at all."""
        issued = _ok(client.post(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/grant",
            headers=auth(seed["author"].xid)))
        ranged = client.get(media_url(issued), headers={"Range": "bytes=0-1023"})
        assert ranged.status_code == 206, ranged.text
        assert ranged.headers["content-range"].startswith("bytes 0-1023/")
        # And the slice is the slice asked for, which a header cannot show.
        assert ranged.content == (bytes(range(256)) * 16)[:1024]

    def test_an_expired_grant_is_refused_and_a_new_one_works(
            self, client, seed, with_audio):
        """The failure the screen is built around.

        A grant that ran out while the page sat open produces no exception and no
        rejected promise — the element simply refuses to load, and the report
        that reaches support is "the audio does not work". So the server's answer
        is checked here, and the screen's answer to it is a control that asks for
        another one.
        """
        from app.platform import grants

        issued = _ok(client.post(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/grant",
            headers=auth(seed["author"].xid)))
        stale = grants.issue(
            user_xid=str(seed["author"].xid), media_xid=issued["media_xid"],
            purpose="authoring", ttl_seconds=120,
            now=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10))
        refused = client.get(media_url({**issued, "grant": stale}))
        assert refused.status_code == 403
        assert refused.json()["code"] == "grant_expired"

        again = _ok(client.post(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/grant",
            headers=auth(seed["author"].xid)))
        assert client.get(media_url(again)).status_code == 200

    def test_a_grant_does_not_open_another_object(self, client, seed, with_audio):
        """Why the screen must never reuse a held grant for a second track: the
        binding is to one object, and the refusal is indistinguishable from an
        expiry to anyone reading the player."""
        from app.platform import grants

        issued = _ok(client.post(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/grant",
            headers=auth(seed["author"].xid)))
        elsewhere = grants.issue(user_xid=str(seed["author"].xid),
                                 media_xid=str(_uuid.uuid4()), purpose="authoring")
        response = client.get(media_url({**issued, "grant": elsewhere}))
        assert response.status_code == 403
        assert response.json()["code"] == "grant_wrong_media"

    def test_the_player_works_in_redirect_delivery_too(
            self, client, seed, with_audio):
        """Two delivery modes, chosen by config, and the same URL serves both.

        `proxy` streams the bytes through this process; `redirect` answers 302 to
        a short-TTL presigned object URL. A media element follows the redirect
        itself, so the player needs no branch — but a player that had hardcoded
        "expect 200 and bytes" would break silently the day a centre's deployment
        turned egress costs down.
        """
        from app.platform.config import settings

        issued = _ok(client.post(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/grant",
            headers=auth(seed["author"].xid)))
        os.environ["MEDIA_DELIVERY"] = "redirect"
        settings.cache_clear()
        try:
            bounced = client.get(media_url(issued), follow_redirects=False)
            assert bounced.status_code == 302, bounced.text
            assert bounced.headers["location"]
            assert bounced.headers["cache-control"] == "private, no-store"
        finally:
            os.environ.pop("MEDIA_DELIVERY", None)
            settings.cache_clear()
        assert settings().media_delivery == "proxy"

    def test_a_student_at_the_same_centre_cannot_get_one(
            self, client, seed, with_audio):
        """The reason this endpoint requires EDIT rather than read scope. Read
        scope admits every member of the owning organization, which would hand a
        student the audio of the paper they are about to sit."""
        refused = client.post(
            f"/api/v1/audio-tracks/{seed['audio_track'].xid}/grant",
            headers=auth(seed["student"].xid))
        assert refused.status_code in (403, 404), refused.text

    def test_a_track_with_nothing_uploaded_says_so(self, client, db, seed):
        """An ordinary state for the screen, not an error: a draft track exists
        before its bytes do."""
        from app.modules.content.models import AudioTrack

        empty = AudioTrack(org_id=seed["org"].id, owner_user_id=seed["author"].id,
                           title="Nothing uploaded yet", status="draft")
        db.add(empty)
        db.flush()
        refused = client.post(f"/api/v1/audio-tracks/{empty.xid}/grant",
                              headers=auth(seed["author"].xid))
        assert refused.status_code == 404
        assert refused.json()["code"] == "track_has_no_media"


class TestPreviewingAListeningPaper:
    """`Preview` → `SectionControls`: enter the section, then ask for the audio."""

    @pytest.fixture
    def previewing(self, client, db, seed, with_audio):
        """A preview attempt on a paper whose listening section is play-once."""
        seed["section"].play_once = True
        db.flush()
        attempt = _ok(client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/preview",
            headers=auth(seed["author"].xid)), 201)
        assert attempt["mode"] == "preview"
        return attempt

    def test_entering_starts_the_sections_clock(self, client, seed, previewing):
        entered = _ok(client.post(
            f"/api/v1/attempts/{previewing['xid']}/sections/1/enter",
            headers=auth(seed["author"].xid)))
        assert entered["entered_at"] is not None
        assert entered["audio_locked"] is False

        # Written once and never moved. An author who presses the control twice
        # must not restart a clock a student would not get to restart.
        again = _ok(client.post(
            f"/api/v1/attempts/{previewing['xid']}/sections/1/enter",
            headers=auth(seed["author"].xid)))
        assert again["entered_at"] == entered["entered_at"]

    def test_an_author_can_listen_more_than_once(self, client, seed, previewing):
        """The claim the screen makes in front of the author, checked rather than
        assumed.

        `audio_grant` computes `play_once = section.play_once and attempt.mode ==
        'exam'`. This section IS play-once and this attempt is `preview`, so the
        lock is never taken. An author who could hear their own paper once could
        not check it — and the comment on the screen saying so is only worth
        writing because this passes.
        """
        first = _ok(client.post(
            f"/api/v1/attempts/{previewing['xid']}/sections/1/audio-grant",
            headers=auth(seed["author"].xid)))
        second = _ok(client.post(
            f"/api/v1/attempts/{previewing['xid']}/sections/1/audio-grant",
            headers=auth(seed["author"].xid)))
        assert first["plays_remaining"] is None
        assert second["plays_remaining"] is None
        assert client.get(media_url(second)).status_code == 200

    def test_a_real_sitting_gets_one_play_and_the_second_is_refused(
            self, client, db, seed, with_audio, entitled, published):
        """The other half of the same rule, and the reason preview's freedom is a
        property of the SERVER rather than a favour the console does itself."""
        seed["section"].play_once = True
        db.flush()
        sitting = _ok(client.post(
            "/api/v1/attempts", headers=auth(seed["student"].xid),
            json={"test_version_xid": str(seed["test_version"].xid)}), 201)
        assert sitting["mode"] == "exam"

        first = _ok(client.post(
            f"/api/v1/attempts/{sitting['xid']}/sections/1/audio-grant",
            headers=auth(seed["student"].xid)))
        assert first["plays_remaining"] == 0
        refused = client.post(
            f"/api/v1/attempts/{sitting['xid']}/sections/1/audio-grant",
            headers=auth(seed["student"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "audio_already_played"

    def test_the_grant_names_the_object_the_payload_does_not(
            self, client, seed, previewing):
        """Why the player reads `media_xid` off the grant and never off the paper.

        The snapshot's field was CALLED `media_xid` and held the audio TRACK's
        xid, so building a media URL from it failed grant verification with
        `grant_wrong_media` — which reads to a user as an expiry and to a
        developer as nothing. It is `track_xid` now, which is what it is; the
        object to fetch still comes only from the grant, because that is the
        one place the delivery asset is named.
        """
        paper = _ok(client.get(f"/api/v1/attempts/{previewing['xid']}/payload",
                               headers=auth(seed["author"].xid)))
        declared = paper["sections"][0]["audio"]["track_xid"]
        issued = _ok(client.post(
            f"/api/v1/attempts/{previewing['xid']}/sections/1/audio-grant",
            headers=auth(seed["author"].xid)))

        assert declared == str(seed["audio_track"].xid)
        assert issued["media_xid"] != declared
        misled = client.get(media_url({**issued, "media_xid": declared}))
        assert misled.status_code == 403

    def test_a_section_that_is_not_in_the_paper_is_a_404(
            self, client, seed, previewing):
        """The screen only renders positions the payload gave it, so this is a
        guard against a stale page rather than a state a user can reach."""
        refused = client.post(
            f"/api/v1/attempts/{previewing['xid']}/sections/9/enter",
            headers=auth(seed["author"].xid))
        assert refused.status_code == 404

    def test_somebody_elses_attempt_is_not_enterable(self, client, seed, previewing):
        """`enter` and `audio-grant` are guarded by `_attempt`, which admits the
        sitter and nobody else — teaching staff read an outcome, they do not act
        on a live sitting."""
        refused = client.post(
            f"/api/v1/attempts/{previewing['xid']}/sections/1/audio-grant",
            headers=auth(seed["student"].xid))
        assert refused.status_code == 404


@pytest.fixture
def assigned(client, db, seed, published):
    """Work this centre SET, sat and submitted by a student.

    Built through the API the console uses rather than by inserting rows, because
    the thing being tested downstream is who may read the result — and that answer
    comes from the assignment, so an assignment that did not go through the same
    gate would prove nothing.
    """
    from app.modules.billing.entitlements import SEAT_BUNDLE
    from app.modules.billing.models import EntitlementRow
    from app.modules.identity.models import OrgMembership, User

    boss = User(phone=f"+9989{_uuid.uuid4().int % 10**8:08d}", given_name="Rustam",
                date_of_birth=dt.date(1985, 1, 1))
    db.add(boss)
    db.flush()
    db.add(OrgMembership(org_id=seed["org"].id, user_id=boss.id,
                         role="centre_admin", status="active"))
    started = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                          feature="org.assignments", source_kind="order",
                          starts_at=started))
    db.add(EntitlementRow(subject_kind="org", subject_id=seed["org"].id,
                          feature=sorted(SEAT_BUNDLE)[0], source_kind="seat",
                          quantity=5, starts_at=started))
    db.flush()

    admin = auth(boss.xid)
    cohort = _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/cohorts",
                             headers=admin, json={"name": "Evening"}), 201)
    _ok(client.post(f"/api/v1/cohorts/{cohort['xid']}/members", headers=admin,
                    json={"user_xids": [str(seed["student"].xid)]}))
    _ok(client.post(f"/api/v1/orgs/{seed['org'].xid}/seats", headers=admin,
                    json={"user_xids": [str(seed["student"].xid)]}))

    now = dt.datetime.now(dt.UTC)
    assignment = _ok(client.post("/api/v1/assignments", headers=admin, json={
        "test_version_xid": str(published["test_version"].xid),
        "target_kind": "cohort", "cohort_xid": cohort["xid"],
        "opens_at": (now - dt.timedelta(hours=1)).isoformat(),
        "closes_at": (now + dt.timedelta(days=7)).isoformat(),
        "mode": "exam", "allow_review_after": "submit", "max_attempts": 1}), 201)

    sitter = auth(seed["student"].xid)
    attempt = _ok(client.post("/api/v1/attempts", headers=sitter,
                              json={"assignment_xid": assignment["xid"]}), 201)
    _ok(client.post(f"/api/v1/attempts/{attempt['xid']}/submit", headers=sitter))
    return {"assignment": assignment, "attempt": attempt, "admin": boss}


class TestTheScoreSummary:
    """`Results`: progress gives the attempt, the result gives the band."""

    def test_the_sequence_the_screen_runs(self, client, seed, assigned):
        progress = _ok(client.get(
            f"/api/v1/assignments/{assigned['assignment']['xid']}/progress",
            headers=auth(seed["author"].xid)))
        row = progress["students"][0]
        assert row["attempt_xid"], "the only handle staff ever get on a sitting"

        result = _ok(client.get(f"/api/v1/attempts/{row['attempt_xid']}/result",
                                headers=auth(seed["author"].xid)))
        # Every field the summary renders. `band` is nullable and the screen
        # prints an em dash for it; the rest are what a teacher is answering
        # "why did this change" with.
        assert result["attempt_xid"] == row["attempt_xid"]
        assert set(result) >= {"raw_score", "max_raw", "band", "scored_at",
                               "regraded", "engine_version", "late_by_ms"}
        assert result["regraded"] is False
        assert result["scored_at"]

    def test_a_staff_read_is_written_to_the_audit_log(self, client, db, seed,
                                                      assigned):
        """These are minors' exam responses, and "who looked at my child's paper"
        needs an answer rather than an assurance. The disclosure IS the event, so
        it is logged on the READ."""
        db.execute(text("DELETE FROM audit_log"))
        db.flush()
        _ok(client.get(f"/api/v1/attempts/{assigned['attempt']['xid']}/result",
                       headers=auth(seed["author"].xid)))
        actions = [r[0] for r in db.execute(text("SELECT action FROM audit_log"))]
        assert "exam.result_read" in actions

    def test_a_student_reading_their_own_result_writes_nothing(
            self, client, db, seed, assigned):
        """It is their paper. A log that records reads nobody performed is worse
        than no log."""
        db.execute(text("DELETE FROM audit_log"))
        db.flush()
        _ok(client.get(f"/api/v1/attempts/{assigned['attempt']['xid']}/result",
                       headers=auth(seed["student"].xid)))
        rows = db.execute(text(
            "SELECT count(*) FROM audit_log WHERE action = 'exam.result_read'"
        )).scalar()
        assert rows == 0

    def test_a_regraded_result_says_so(self, client, db, seed, assigned):
        """`regraded` reads the run's own provenance — `reason != 'initial'` —
        rather than counting rows, so a staged dry run is not mistaken for one.

        This is the line a teacher stands behind in front of a parent: the band
        moved because somebody moved it, and the original run is still there.
        """
        from app.modules.exam.models import ScoreRun

        attempt_xid = assigned["attempt"]["xid"]
        attempt_id = db.scalar(text(
            "SELECT id FROM attempts WHERE xid = CAST(:x AS uuid)"
        ).bindparams(x=attempt_xid))
        # What `regrade.apply()` does: supersede rather than overwrite, with
        # `reason = 'regrade_key'` — the default that function passes, and one of
        # the five the `score_runs_reason_check` constraint admits. Built through
        # the ORM so the row is one the application could actually have written;
        # a hand-rolled INSERT gets past neither that constraint nor the NOT NULL
        # on `key_versions`, which is the column that makes a score reproducible
        # years later.
        db.execute(text("UPDATE score_runs SET is_current = false "
                        "WHERE attempt_id = :a").bindparams(a=attempt_id))
        db.add(ScoreRun(attempt_id=attempt_id, reason="regrade_key", raw_score=3,
                        max_raw=3, band=7.0, key_versions={},
                        per_section={"reading": {"raw": 3.0, "band": 7.0}},
                        is_current=True))
        db.flush()

        result = _ok(client.get(f"/api/v1/attempts/{attempt_xid}/result",
                                headers=auth(seed["author"].xid)))
        assert result["regraded"] is True
        assert result["band"] == 7.0
        # The CURRENT run, so the number beside the flag is already the new one.
        assert result["raw_score"] == 3.0

    def test_an_unscored_attempt_is_an_ordinary_empty_state(self, client, seed):
        """404, and the screen prints a sentence rather than a red error. Marking
        is synchronous on submit, so this means the student is still sitting."""
        attempt = _ok(client.post(
            f"/api/v1/test-versions/{seed['test_version'].xid}/preview",
            headers=auth(seed["author"].xid)), 201)
        response = client.get(f"/api/v1/attempts/{attempt['xid']}/result",
                              headers=auth(seed["author"].xid))
        assert response.status_code == 404

    def test_practice_a_student_chose_has_no_staff_reader(
            self, client, db, seed, entitled, published):
        """Self-serve work carries no assignment, and the centre's claim comes
        from having set the work. 404 rather than 403, so a probe cannot use the
        difference to learn that an attempt exists — which is why the screen
        treats both the same way."""
        own = _ok(client.post("/api/v1/attempts", headers=auth(seed["student"].xid),
                              json={"test_version_xid":
                                    str(published["test_version"].xid)}), 201)
        _ok(client.post(f"/api/v1/attempts/{own['xid']}/submit",
                        headers=auth(seed["student"].xid)))
        refused = client.get(f"/api/v1/attempts/{own['xid']}/result",
                             headers=auth(seed["author"].xid))
        assert refused.status_code == 404

    def test_a_voided_attempt_has_no_band_to_show(self, client, db, seed, assigned):
        """A band is a claim arising from the sitting an operator invalidated.
        409, and the screen says what happened rather than showing a score nobody
        stands behind."""
        db.execute(text("""
            UPDATE attempts SET status = 'voided' WHERE xid = CAST(:x AS uuid)
        """).bindparams(x=assigned["attempt"]["xid"]))
        db.flush()
        response = client.get(
            f"/api/v1/attempts/{assigned['attempt']['xid']}/result",
            headers=auth(seed["author"].xid))
        assert response.status_code == 409
        assert response.json()["code"] == "attempt_voided"


class TestCancellingAnUpload:
    """`upload.ts`: the abort that a cancelled upload used to skip."""

    def _open(self, client, seed):
        return _ok(client.post("/api/v1/audio-tracks", headers=auth(seed["author"].xid),
                               json={"title": "Section 4 draft", "filename": "s.wav",
                                     "bytes": 4096, "content_type": "audio/wav",
                                     "attestation": {"claim": "original",
                                                     "statement_version": "1"}}), 201)

    def test_the_multipart_is_closed_rather_than_abandoned(
            self, client, db, seed, store):
        """Walking away leaves an open multipart, and storage bills for the parts
        already held whether or not anything ever assembles them."""
        created = self._open(client, seed)
        upload_xid = created["upload"]["xid"]
        assert client.delete(f"/api/v1/uploads/{upload_xid}",
                             headers=auth(seed["author"].xid)).status_code == 204

        row = db.execute(text("""
            SELECT u.status AS upload_status, a.status AS asset_status
            FROM media_uploads u JOIN media_assets a ON a.id = u.media_asset_id
            WHERE u.xid = CAST(:x AS uuid)
        """).bindparams(x=upload_xid)).mappings().first()
        assert row["upload_status"] == "aborted"
        assert row["asset_status"] == "removed"

    def test_aborting_twice_is_not_an_error(self, client, seed, store):
        """The cancel path can fire twice — a dropped response, a double click —
        and a second 204 is what lets the client retry without inventing a
        special case."""
        upload_xid = self._open(client, seed)["upload"]["xid"]
        headers = auth(seed["author"].xid)
        assert client.delete(f"/api/v1/uploads/{upload_xid}",
                             headers=headers).status_code == 204
        assert client.delete(f"/api/v1/uploads/{upload_xid}",
                             headers=headers).status_code == 204

    def test_an_upload_nobody_opened_is_a_404(self, client, seed, store):
        refused = client.delete(f"/api/v1/uploads/{_uuid.uuid4()}",
                                headers=auth(seed["author"].xid))
        assert refused.status_code == 404

    def test_the_track_row_outlives_the_cancelled_upload(
            self, client, seed, store):
        """Stated because the screen has to say it.

        `POST /audio-tracks` creates the TRACK and opens the upload in one
        request, and aborting the upload does not remove the track. It stays in
        the library reading `processing`, and there is no endpoint that removes
        one — so the console tells the teacher that rather than letting them
        wonder why their cancelled file is still listed.
        """
        created = self._open(client, seed)
        assert client.delete(f"/api/v1/uploads/{created['upload']['xid']}",
                             headers=auth(seed["author"].xid)).status_code == 204
        # The track used to survive as `processing` forever, in a library with
        # no way to clear it — and the console polls processing rows every four
        # seconds, so each abandoned upload left a permanent poller. Aborting
        # now retires the track it was for, and only one that never produced a
        # delivery asset, so a re-abort cannot retire a transcoded track.
        track = _ok(client.get(
            f"/api/v1/audio-tracks/{created['audio_track']['xid']}",
            headers=auth(seed["author"].xid)))
        assert track["status"] == "failed"


class TestStagingARegradeByHand:
    """`Regrades`: a change with no key fix behind it, into the same dry run."""

    def _stage(self, client, seed, **overrides):
        body = {"trigger": "band_map_change", "subject_type": "band_map_version",
                "subject_xid": str(seed["band_map_version"].xid),
                "reason": "Listening curve retuned after the January calibration."}
        body.update(overrides)
        return client.post("/api/v1/regrades", headers=auth(seed["author"].xid),
                           json=body)

    def test_a_band_map_change_stages_a_dry_run_and_nothing_else(
            self, client, seed):
        staged = _ok(self._stage(client, seed), 201)
        assert staged["dry_run"] is True
        assert staged["status"] == "planning"
        # The impact the screen renders once the planner lands. It is present and
        # zeroed rather than absent, so the report has a shape from the first
        # render.
        assert set(staged["impact"]) >= {"attempts_total", "scores_changed",
                                         "bands_changed", "students_to_notify"}

    def test_it_appears_in_the_same_listing_the_screen_already_reads(
            self, client, seed):
        staged = _ok(self._stage(client, seed), 201)
        listed = _ok(client.get("/api/v1/regrades", headers=auth(seed["author"].xid)))
        assert [job["xid"] for job in listed] == [staged["xid"]]

    def test_it_cannot_be_applied_until_the_planner_has_finished(
            self, client, seed):
        """The whole point of staging by hand rather than regrading by hand.

        A job is `planning` until a worker computes band movement, and `apply`
        refuses anything that is not `ready` — so manual staging lands in the same
        read-the-numbers-then-decide flow as a key fix. There is no path from this
        form to a rescored exam without somebody reading what it moves.
        """
        staged = _ok(self._stage(client, seed), 201)
        refused = client.post(f"/api/v1/regrades/{staged['xid']}/apply",
                              headers=auth(seed["author"].xid))
        assert refused.status_code == 409
        assert refused.json()["code"] == "regrade_not_ready"

    def test_a_test_version_is_a_subject_too(self, client, seed, published):
        """The picker offers published versions only. A draft has never been sat,
        so a job against one plans across nothing and reports an impact of zero —
        which reads as "this changes nobody" rather than "you chose a paper
        nobody sat"."""
        staged = _ok(self._stage(
            client, seed, trigger="engine_fix", subject_type="test_version",
            subject_xid=str(published["test_version"].xid),
            reason="Scorer 1.0.1 fixes the number-word normalizer."), 201)
        assert staged["trigger"] == "engine_fix"

    def test_a_subject_that_does_not_exist_is_named(self, client, seed):
        """The message the screen shows when a picker and the server disagree —
        a band map deleted in another tab, most plainly."""
        refused = self._stage(client, seed, subject_xid=str(_uuid.uuid4()))
        assert refused.status_code == 404
        assert "band map version" in refused.json()["title"].lower()

    def test_a_student_cannot_stage_one(self, client, seed):
        """Rescoring finished exams is not a student action, and the refusal
        names the role rather than hiding behind a 404 — this endpoint is not one
        whose existence is a secret."""
        refused = client.post("/api/v1/regrades", headers=auth(seed["student"].xid),
                              json={"trigger": "manual",
                                    "subject_type": "band_map_version",
                                    "subject_xid": str(seed["band_map_version"].xid),
                                    "reason": "let me"})
        assert refused.status_code == 403
        assert refused.json()["code"] == "regrade_not_permitted"

    def test_the_pickers_read_from_endpoints_that_answer(self, client, seed):
        """Both selects on the form. `/band-maps` is a bare array of maps whose
        `current_version.xid` is the subject; `/tests` is a page whose
        `current_published_version_xid` is."""
        maps = _ok(client.get("/api/v1/band-maps", headers=auth(seed["author"].xid)))
        assert any(m.get("current_version", {}).get("xid") for m in maps)
        tests = _ok(client.get("/api/v1/tests?limit=100",
                               headers=auth(seed["author"].xid)))
        assert "items" in tests
