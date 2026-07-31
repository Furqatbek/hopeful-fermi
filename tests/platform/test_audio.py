"""The audio port's decisions, without ffmpeg.

**Nothing in this file may run a binary or touch a service.** It is collected by
`make test-unit`, which CI runs in the lint job — a runner with no ffmpeg, no
Postgres and no MinIO. `tests/integration/test_media.py` is where the real binary
runs, in the job that installs it. `conftest.py` here empties `PATH` so that rule
is a mechanism rather than a habit.

What is here is the part that is a decision rather than a shell-out: which uploads
get refused and what the author is told, plus the guards that only fire on a
machine or a file CI does not have — decided by a stub, so both answers are
reachable anywhere.

`validate()` is a pure function over two dataclasses, and three of its four
branches had never run — including both duration bounds. It is the function that
turns "your upload failed" into a sentence a teacher can act on.
"""

from __future__ import annotations

import subprocess

import pytest

from app.platform.audio import (
    MAX_DURATION_MS, MIN_DURATION_MS, SILENCE_LUFS, AudioError, Loudness, Probe,
    _as_float, _run, available, require, validate,
)


def probe(**kw) -> Probe:
    base = dict(duration_ms=600_000, sample_rate=48_000, channels=2, codec="pcm_s16le")
    base.update(kw)
    return Probe(**base)


def loudness(**kw) -> Loudness:
    base = dict(integrated_lufs=-20.0, true_peak_db=-3.0, lra=6.0,
                threshold=-30.0, offset=0.5)
    base.update(kw)
    return Loudness(**base)


class TestValidate:
    def test_a_sound_upload_has_no_problems(self):
        """A gate that cries wolf gets bypassed."""
        assert validate(probe(), loudness()) == []

    def test_a_file_under_a_second_is_refused(self):
        """The commonest broken export: a listening section that came out empty."""
        problems = validate(probe(duration_ms=MIN_DURATION_MS - 1), loudness())
        assert problems == ["This file is under a second long."]

    def test_exactly_the_minimum_is_allowed(self):
        assert validate(probe(duration_ms=MIN_DURATION_MS), loudness()) == []

    def test_a_file_over_the_maximum_is_refused_and_says_by_how_much(self):
        """The number matters. "Too long" sends a teacher back to re-export
        blind; "this file is 61 minutes long, the limit is 60" does not."""
        problems = validate(probe(duration_ms=MAX_DURATION_MS + 60_000), loudness())
        assert len(problems) == 1
        assert str((MAX_DURATION_MS + 60_000) // 60_000) in problems[0]
        assert str(MAX_DURATION_MS // 60_000) in problems[0]

    def test_exactly_the_maximum_is_allowed(self):
        assert validate(probe(duration_ms=MAX_DURATION_MS), loudness()) == []

    def test_a_silent_file_is_refused_with_the_usual_cause(self):
        problems = validate(probe(), loudness(integrated_lufs=SILENCE_LUFS))
        assert "silent" in problems[0]
        assert "export settings" in problems[0]

    def test_a_file_with_no_channels_is_refused(self):
        assert validate(probe(channels=0), loudness()) == [
            "This file has no audio channels."]

    def test_every_problem_is_returned_at_once(self):
        """"All findings at once, like the publish gate: a teacher on a slow
        connection should not upload three times to learn three separate
        things." Three of these four branches had never executed."""
        problems = validate(probe(duration_ms=0, channels=0),
                            loudness(integrated_lufs=-80.0))
        assert len(problems) == 3


class TestTheFfmpegGuard:
    """Both answers, decided by a stub rather than by the machine.

    An earlier version of this class asserted `available() is True`, on the
    reasoning that "under `CI=true` a skip is a failure, so this is an assertion
    about the runner". That reasoning belongs to
    `tests/integration/test_media.py`, which runs in the job that installs
    ffmpeg. Here it turned the tier's guarantee inside out: it passed on every
    developer machine and in the tests job, and failed in the lint job where
    ffmpeg is genuinely absent — the one environment a local `make ci` could not
    reproduce.
    """

    def test_both_binaries_present_means_available(self, monkeypatch):
        monkeypatch.setattr("app.platform.audio.shutil.which",
                            lambda name: f"/usr/bin/{name}")
        assert available() is True
        require()

    @pytest.mark.parametrize("missing", ["ffmpeg", "ffprobe"])
    def test_either_one_missing_is_not_available(self, monkeypatch, missing):
        """`ffprobe` ships with `ffmpeg` in every distribution package, so the
        half-installed case only arises in a hand-built image — which is exactly
        where nobody is watching."""
        monkeypatch.setattr(
            "app.platform.audio.shutil.which",
            lambda name: None if name == missing else f"/usr/bin/{name}")
        assert available() is False

    def test_a_worker_without_ffmpeg_fails_loudly(self, monkeypatch):
        """"A worker that starts without them fails loudly rather than silently
        marking uploads `failed`."

        The whole point is the difference between an operator seeing one clear
        message at boot and a centre watching every upload turn to `failed` for a
        reason that is nowhere in the response.
        """
        monkeypatch.setattr("app.platform.audio.shutil.which", lambda _: None)
        assert available() is False
        with pytest.raises(AudioError, match="not installed"):
            require()


class TestRunTranslatesSubprocessFailures:
    """`_run` is the one place a subprocess result becomes an `AudioError`, and
    an `AudioError`'s message is shown to the author verbatim."""

    def test_a_timeout_becomes_a_sentence_about_the_file(self, monkeypatch):
        """A 900-second timeout is a file nobody should have uploaded, not an
        outage — so the author is told about their file rather than being shown
        an internal error."""
        def times_out(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=900)

        monkeypatch.setattr(subprocess, "run", times_out)
        with pytest.raises(AudioError, match="too long to process") as raised:
            _run(["ffmpeg", "-i", "x"])
        assert "900" in raised.value.detail

    def test_a_missing_binary_names_the_binary(self, monkeypatch):
        """The deployment mistake, reported as one. This is the same condition
        `require()` catches at boot, on the path that hits it at run time."""
        def not_found(*args, **kwargs):
            raise FileNotFoundError

        monkeypatch.setattr(subprocess, "run", not_found)
        with pytest.raises(AudioError, match="ffmpeg is not installed"):
            _run(["ffmpeg", "-i", "x"])


class TestAsFloat:
    """ffprobe reports numbers as strings, and `loudnorm` sometimes reports
    `-inf` or nothing at all. A default is the difference between a measurement
    that reads as silence and a `TypeError` in a worker."""

    @pytest.mark.parametrize("value,expected", [
        ("-23.5", -23.5), (-23.5, -23.5), ("0", 0.0), (7, 7.0),
    ])
    def test_it_parses_what_ffmpeg_reports(self, value, expected):
        assert _as_float(value, -70.0) == expected

    @pytest.mark.parametrize("value", [None, "", "n/a", {}, []])
    def test_it_falls_back_rather_than_raising(self, value):
        assert _as_float(value, -70.0) == -70.0

    def test_minus_infinity_is_kept_rather_than_defaulted(self):
        """`loudnorm` reports `-inf` for a digitally silent file, and `float()`
        parses it. Keeping it is right: `-inf <= SILENCE_LUFS` is true, so the
        file is refused as silent — which is what it is. Defaulting to -70.0
        would land on the same verdict by accident rather than by measurement.
        """
        assert _as_float("-inf", -70.0) == float("-inf")
        assert loudness(integrated_lufs=_as_float("-inf", -70.0)).is_silent
