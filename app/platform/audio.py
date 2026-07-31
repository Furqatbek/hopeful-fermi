"""ffmpeg, as a subprocess. No Python audio library (ADR-0001 §5.5 table).

That instruction is right and worth restating: `pydub` shells out to ffmpeg
anyway while adding a dependency and a layer of surprises, and `librosa` pulls in
NumPy and SciPy to do what one command line does. This module is a thin, typed
wrapper around two binaries.

**Two-pass loudness normalisation is the reason this file is not four lines.**
IELTS listening is a timed exam: a student who has to stop and adjust the volume
between sections has lost time to our engineering. `loudnorm` in one pass is a
dynamic compressor whose output loudness is approximate; run as measure-then-
apply it is a precise linear gain. The first pass costs a few seconds of CPU and
buys a section that plays at the same level as every other section.

Target: **-16 LUFS integrated, mono**. The spoken-word streaming standard, and
the right choice for students on cheap earbuds in a noisy room — quieter than
music platforms target, because speech intelligibility beats headroom here.

Delivery: **AAC in MP4 at 64 kbps mono**. Universally decodable — iOS Safari,
Android Chrome, every desktop browser — which matters more than the ~2x saving
Opus would give, because a listening section that will not play is a refund.
A 30-minute section is about 14 MB.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger()

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

TARGET_LUFS = -16.0
TARGET_TRUE_PEAK = -1.5
TARGET_LRA = 11.0
DELIVERY_BITRATE = "64k"
DELIVERY_SAMPLE_RATE = 44_100
DELIVERY_SUFFIX = ".m4a"
DELIVERY_CONTENT_TYPE = "audio/mp4"

# An IELTS listening section is ~30 minutes. Anything past an hour is either a
# mistake or someone using us as a file host, and both should be refused before
# a minute of CPU is spent on them.
MAX_DURATION_MS = 3_600_000
MIN_DURATION_MS = 1_000
# Below this the upload is effectively silence — a failed export, or the wrong
# file. Rejecting at ingest is the only point at which anyone will look.
SILENCE_LUFS = -50.0

# Transcoding is the one CPU hog in the system (Deliverable 5 §5): a 30-minute
# WAV is 30-60 s of ffmpeg. It runs at low priority so it cannot starve the web
# workers on a shared box, and with a hard wall-clock cap so a malformed file
# cannot pin a core forever.
NICE = 10
TIMEOUT_SECONDS = 900


class AudioError(Exception):
    """Anything ffmpeg could not do. Carries text fit to show an author."""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail[:2000]


@dataclass(frozen=True, slots=True)
class Probe:
    duration_ms: int
    sample_rate: int
    channels: int
    codec: str
    bit_rate: int | None = None
    # `duration_seconds` used to live here and nothing ever called it. Every
    # caller works in milliseconds — the column, the snapshot, the section
    # timings — so the convenience was a second unit for the same quantity,
    # waiting for somebody to compare one against the other.


@dataclass(frozen=True, slots=True)
class Loudness:
    """EBU R128 measurements, as `loudnorm` reports them."""

    integrated_lufs: float
    true_peak_db: float
    lra: float
    threshold: float
    offset: float

    @property
    def is_silent(self) -> bool:
        return self.integrated_lufs <= SILENCE_LUFS


@dataclass(frozen=True, slots=True)
class Transcoded:
    path: Path
    probe: Probe
    measured: Loudness
    content_type: str = DELIVERY_CONTENT_TYPE


def available() -> bool:
    """Whether ffmpeg is on this machine.

    Checked rather than assumed so the test suite can skip the real-encode tests
    cleanly on a laptop, in the same way the integration suite skips without a
    database. A worker that starts without ffmpeg fails loudly instead.
    """
    return bool(shutil.which(FFMPEG) and shutil.which(FFPROBE))


def require() -> None:
    if not available():
        raise AudioError(
            "ffmpeg is not installed on this worker. Audio ingest cannot run.")


def probe(source: Path) -> Probe:
    """Container and stream facts. Cheap — no decode."""
    require()
    raw = _run([FFPROBE, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(source)])
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:                        # pragma: no cover
        raise AudioError("This file could not be read as audio.",
                         detail=str(exc)) from None

    stream = next((s for s in parsed.get("streams", [])
                   if s.get("codec_type") == "audio"), None)
    if stream is None:
        raise AudioError("This file contains no audio stream.",
                         detail=raw[:500])

    duration = float(parsed.get("format", {}).get("duration")
                     or stream.get("duration") or 0)
    return Probe(
        duration_ms=int(duration * 1000),
        sample_rate=int(stream.get("sample_rate") or 0),
        channels=int(stream.get("channels") or 0),
        codec=stream.get("codec_name", "unknown"),
        bit_rate=int(parsed["format"]["bit_rate"])
        if parsed.get("format", {}).get("bit_rate") else None,
    )


def measure(source: Path) -> Loudness:
    """Pass one: what IS the loudness. Prints JSON to stderr, decodes nothing."""
    require()
    stderr = _run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", str(source),
         "-af", (f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:"
                 f"LRA={TARGET_LRA}:print_format=json"),
         "-f", "null", "-"],
        capture="stderr")

    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1:
        raise AudioError("Loudness could not be measured for this file.",
                         detail=stderr[-1000:])
    parsed = json.loads(stderr[start:end + 1])
    return Loudness(
        integrated_lufs=_as_float(parsed.get("input_i"), -70.0),
        true_peak_db=_as_float(parsed.get("input_tp"), -70.0),
        lra=_as_float(parsed.get("input_lra"), 0.0),
        threshold=_as_float(parsed.get("input_thresh"), -70.0),
        offset=_as_float(parsed.get("target_offset"), 0.0),
    )


def validate(probe_result: Probe, loudness: Loudness) -> list[str]:
    """Everything wrong with this upload, in one list.

    All findings at once, like the publish gate: a teacher on a slow connection
    should not upload three times to learn three separate things.
    """
    problems: list[str] = []
    if probe_result.duration_ms < MIN_DURATION_MS:
        problems.append("This file is under a second long.")
    if probe_result.duration_ms > MAX_DURATION_MS:
        problems.append(
            f"This file is {probe_result.duration_ms // 60000} minutes long; "
            f"the limit is {MAX_DURATION_MS // 60000}.")
    if loudness.is_silent:
        problems.append(
            "This file is silent or almost silent. Check the export settings — "
            "a muted track is the usual cause.")
    if probe_result.channels == 0:
        problems.append("This file has no audio channels.")
    return problems


def transcode(source: Path, destination: Path, *,
              measured: Loudness | None = None) -> Transcoded:
    """Pass two: apply the measured values as a linear gain, then encode.

    Passing `measured` from pass one is what makes this precise rather than
    approximate. `linear=true` tells loudnorm to use the measurements instead of
    compressing dynamically, and `-ar`/`-ac` force one delivery shape so a
    student never gets a stereo file at one level and a mono file at another.
    """
    require()
    loudness = measured or measure(source)

    filters = (
        f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TRUE_PEAK}:LRA={TARGET_LRA}"
        f":measured_I={loudness.integrated_lufs}"
        f":measured_TP={loudness.true_peak_db}"
        f":measured_LRA={loudness.lra}"
        f":measured_thresh={loudness.threshold}"
        f":offset={loudness.offset}:linear=true:print_format=summary"
    )
    _run([FFMPEG, "-hide_banner", "-nostats", "-y", "-i", str(source),
          "-af", filters,
          "-ac", "1", "-ar", str(DELIVERY_SAMPLE_RATE),
          "-c:a", "aac", "-b:a", DELIVERY_BITRATE,
          # Metadata is stripped: an uploaded WAV can carry the teacher's name,
          # their software licence and sometimes a file path from their laptop,
          # and none of that should reach a student's device.
          "-map_metadata", "-1",
          # Moves the index to the front so playback can start before the whole
          # file has arrived — the difference between instant and a ten-second
          # stare on a 3G connection.
          "-movflags", "+faststart",
          str(destination)])

    if not destination.exists() or destination.stat().st_size == 0:
        raise AudioError("Transcoding produced no output.")

    result = probe(destination)
    log.info("audio_transcoded", duration_ms=result.duration_ms,
             bytes=destination.stat().st_size,
             from_lufs=round(loudness.integrated_lufs, 1), to_lufs=TARGET_LUFS)
    return Transcoded(path=destination, probe=result, measured=loudness)


def _run(command: list[str], *, capture: str = "stdout") -> str:
    try:
        completed = subprocess.run(          # noqa: S603 — fixed argv, no shell
            _niced(command), capture_output=True, timeout=TIMEOUT_SECONDS,
            check=False, text=True)
    except subprocess.TimeoutExpired:
        raise AudioError(
            "This file took too long to process and was abandoned.",
            detail=f"exceeded {TIMEOUT_SECONDS}s") from None
    except FileNotFoundError:
        raise AudioError("ffmpeg is not installed on this worker.") from None

    if completed.returncode != 0:
        raise AudioError("This file could not be processed as audio.",
                         detail=completed.stderr[-2000:])
    return completed.stdout if capture == "stdout" else completed.stderr


def _niced(command: list[str]) -> list[str]:
    """`nice`, per Deliverable 5 §5: transcode must not starve the web workers.

    Falls back to the bare command where `nice` is unavailable rather than
    failing — the priority is an optimisation, not a correctness requirement.
    """
    return [*(["nice", "-n", str(NICE)] if shutil.which("nice") else []), *command]


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
