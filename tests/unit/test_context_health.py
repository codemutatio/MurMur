"""Tests for call/media context tagging and the health heartbeat."""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from murmur.audio.context import AudioContextMonitor, SegmentContext
from murmur.audio.segmenter import Segment
from murmur.audio.spool import write_segment_wav
from murmur.config import load_config
from murmur.control import StatusBoard
from murmur.mac.audio_activity import AudioProcess, classify
from murmur.output.health import HealthReporter
from murmur.output.reader import TranscriptReader
from murmur.output.writer import TranscriptWriter
from murmur.transcribe.base import TranscriptionError
from murmur.transcribe.worker import TranscribeWorker
from tests.fakes import FakeTranscriber, FixedClock

SELF = 1000
# The software-factory's timestamp regex — tags must not change what it sees.
FACTORY_STAMP = re.compile(r"^\*\*\[(\d{2}):(\d{2})\]\*\*\s*(.*)$")


# --- classify -----------------------------------------------------------------


def _p(pid: int, bid: str, *, i: bool = False, o: bool = False) -> AudioProcess:
    return AudioProcess(pid=pid, bundle_id=bid, input=i, output=o)


def test_classify_ignores_own_mic() -> None:
    assert classify([_p(SELF, "com.jay.murmur", i=True)], SELF) == (None, None)


def test_classify_media_when_app_plays_audio() -> None:
    procs = [_p(SELF, "com.jay.murmur", i=True), _p(7, "com.google.Chrome", o=True)]
    assert classify(procs, SELF) == ("media", "com.google.Chrome")


def test_classify_siri_listener_on_mic_is_not_a_call() -> None:
    # Regression: com.apple.CoreSpeech ("Hey Siri") holds the mic almost
    # constantly — speech from the speakers was tagged "call" because of it.
    procs = [
        _p(SELF, "com.jay.murmur", i=True),
        _p(793, "com.apple.CoreSpeech", i=True),
        _p(900, "", o=True),  # `say` playing through the speakers
    ]
    assert classify(procs, SELF) == ("media", "pid:900")


def test_classify_dictation_tool_on_mic_is_ignored() -> None:
    procs = [_p(SELF, "com.jay.murmur", i=True), _p(5, "com.anthropic.claudefordesktop.helper", i=True)]
    assert classify(procs, SELF) == (None, None)


def test_classify_call_audio_from_speakers_stays_a_call() -> None:
    # On a Meet call the other people's voices come out of the speakers (output),
    # but Chrome also holds the mic — that must read as a call, never media.
    procs = [
        _p(SELF, "com.jay.murmur", i=True),
        _p(11, "com.google.Chrome.helper", i=True, o=True),
    ]
    assert classify(procs, SELF) == ("call", "com.google.Chrome.helper")


def test_classify_call_beats_media() -> None:
    procs = [
        _p(SELF, "com.jay.murmur", i=True),
        _p(7, "com.apple.Music", o=True),
        _p(8, "us.zoom.xos", i=True, o=True),
    ]
    assert classify(procs, SELF) == ("call", "us.zoom.xos")


# --- monitor majority labelling -------------------------------------------------


def _monitor(samples: list[tuple[str | None, str | None]]) -> AudioContextMonitor:
    it = iter(samples)
    t = iter(range(100, 100 + len(samples)))
    m = AudioContextMonitor(
        stop=threading.Event(), probe=lambda: next(it), clock=lambda: float(next(t))
    )
    for _ in samples:
        m.sample_once()
    return m


def test_label_majority_media() -> None:
    m = _monitor([("media", "com.google.Chrome")] * 8 + [(None, None)] * 2)
    assert m.label(100, 110) == SegmentContext("media", "com.google.Chrome")


def test_label_brief_blip_is_ignored() -> None:
    m = _monitor([(None, None)] * 9 + [("media", "com.apple.systemsound")])
    assert m.label(100, 110) is None


def test_label_call_outranks_media_at_half() -> None:
    m = _monitor([("call", "us.zoom.xos")] * 5 + [("media", "com.apple.Music")] * 5)
    assert m.label(100, 110) == SegmentContext("call", "us.zoom.xos")


def test_label_no_samples_in_window_is_none() -> None:
    m = _monitor([("media", "x")] * 3)
    assert m.label(5000, 6000) is None


def test_probe_failure_does_not_raise() -> None:
    def boom() -> tuple[str | None, str | None]:
        raise OSError("no CoreAudio")

    m = AudioContextMonitor(stop=threading.Event(), probe=boom)  # type: ignore[arg-type]
    m.sample_once()
    assert m.label(0, 1e12) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreAudio only on macOS")
def test_live_probe_runs_on_this_mac() -> None:
    from murmur.mac.audio_activity import list_audio_processes

    procs = list_audio_processes()
    assert isinstance(procs, list)


# --- writer / parsers -----------------------------------------------------------


def test_writer_context_comment_keeps_entry_parseable(tmp_path: Path) -> None:
    clock = FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))
    w = TranscriptWriter(tmp_path, clock)
    start = datetime(2026, 10, 6, 9, 5, tzinfo=UTC)
    path = w.append(start, "Rivy product capture: export button", context="call app=us.zoom.xos")
    lines = path.read_text().splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("**["))
    assert lines[i - 1] == "<!-- murmur: call app=us.zoom.xos -->"
    # Entry text is untouched for the factory's marker matching…
    m = FACTORY_STAMP.fullmatch(lines[i])
    assert m and m[3] == "Rivy product capture: export button"
    # …and the app's own reader still sees exactly one entry.
    entries = TranscriptReader(tmp_path).entries_for(start.astimezone().date())
    assert len(entries) == 1 and entries[0].text == "Rivy product capture: export button"


# --- worker integration ---------------------------------------------------------


class _FixedLabel:
    def __init__(self, ctx: SegmentContext | None) -> None:
        self.ctx = ctx

    def label(self, start: float, end: float) -> SegmentContext | None:
        return self.ctx


def _run(
    tmp_path: Path,
    *,
    label: SegmentContext | None,
    skip_media: bool = False,
    transcriber: FakeTranscriber | None = None,
) -> tuple[Path, Path, FakeTranscriber, HealthReporter]:
    spool, transcripts = tmp_path / "spool", tmp_path / "Transcripts"
    spool.mkdir()
    start = datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC)
    wav = write_segment_wav(spool, Segment(samples=np.zeros(1600, np.float32) + 0.01, start_at=start))
    clock = FixedClock(datetime(2026, 6, 12, 9, 1, tzinfo=UTC))
    fake = transcriber or FakeTranscriber(["We agreed to ship on Friday"])
    health = HealthReporter(transcripts / ".murmur-health.json", model="large-v3", now=clock)
    stop, drained = threading.Event(), threading.Event()
    worker = TranscribeWorker(
        spool_dir=spool, work_q=queue.Queue(), transcriber=fake, fallback=None,
        writer=TranscriptWriter(transcripts, clock), status=StatusBoard(), keep_audio=False,
        language="en", initial_prompt=None, backlog_drained=drained, stop=stop,
        backoff_s=0.01, context=_FixedLabel(label), skip_media=skip_media, health=health,
    )
    worker.start()
    drained.wait(5)
    stop.set()
    worker.join(5)
    return wav, transcripts, fake, health


def test_worker_tags_media_segment(tmp_path: Path) -> None:
    wav, transcripts, fake, health = _run(
        tmp_path, label=SegmentContext("media", "com.google.Chrome")
    )
    text = next(transcripts.glob("*.md")).read_text()
    assert "<!-- murmur: media app=com.google.Chrome -->" in text
    assert "We agreed to ship on Friday" in text
    snap = health.snapshot()
    assert snap["segments_tagged_media"] == 1 and snap["transcribed_since_start"] == 1


def test_worker_skip_media_does_not_transcribe(tmp_path: Path) -> None:
    wav, transcripts, fake, health = _run(
        tmp_path, label=SegmentContext("media", "com.netflix"), skip_media=True
    )
    assert fake.call_count == 0
    assert not wav.exists()
    assert not list(transcripts.glob("*.md"))
    assert health.snapshot()["media_segments_skipped"] == 1


def test_worker_untagged_when_no_context(tmp_path: Path) -> None:
    _, transcripts, _, _ = _run(tmp_path, label=None)
    assert "<!--" not in next(transcripts.glob("*.md")).read_text()


def test_worker_failure_reaches_health(tmp_path: Path) -> None:
    err = TranscriptionError("Repo id must be in the form 'repo_name'")
    _, _, _, health = _run(
        tmp_path, label=None, transcriber=FakeTranscriber([err, err, err])
    )
    snap = health.snapshot()
    assert snap["consecutive_errors"] == 1  # one segment, failed after 3 attempts
    assert "Repo id must be" in snap["last_error"]


# --- health reporter --------------------------------------------------------------


def test_health_states_and_file(tmp_path: Path) -> None:
    clock = FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))
    paused = {"v": False}
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "a.wav").write_bytes(b"x")
    h = HealthReporter(
        tmp_path / ".murmur-health.json", model="large-v3", now=clock,
        is_paused=lambda: paused["v"], spool_dir=spool,
    )
    assert h.snapshot()["state"] == "recording"
    for _ in range(3):
        h.record_error("boom")
    assert h.snapshot()["state"] == "failing"
    h.record_success()
    assert h.snapshot()["state"] == "recording" and h.snapshot()["consecutive_errors"] == 0
    paused["v"] = True
    assert h.snapshot()["state"] == "paused"
    h.write(stopped=True)
    data = json.loads((tmp_path / ".murmur-health.json").read_text())
    assert data["state"] == "stopped" and data["spool_backlog"] == 1 and data["model"] == "large-v3"


# --- config -----------------------------------------------------------------------


def test_config_context_keys(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[capture]\ncontext_tags = false\nskip_media = true\n")
    cfg, warnings = load_config(cfg_path)
    assert cfg.capture.context_tags is False and cfg.capture.skip_media is True
    assert not [w for w in warnings if "Unknown" in w]


def test_config_context_defaults() -> None:
    cfg, _ = load_config(None)
    assert cfg.capture.context_tags is True and cfg.capture.skip_media is False
