"""Tests for speaker recognition: voice library, learning, turn tagging, CLI."""

from __future__ import annotations

import json
import os
import queue
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from murmur.audio.context import SegmentContext
from murmur.audio.segmenter import Segment
from murmur.audio.spool import write_segment_wav
from murmur.cli import main as cli_main
from murmur.config import load_config
from murmur.control import StatusBoard
from murmur.output.writer import TranscriptWriter
from murmur.transcribe.base import SegmentScore, TranscriptionResult
from murmur.transcribe.worker import TranscribeWorker
from murmur.voices.embedder import model_dir
from murmur.voices.store import Thresholds, VoiceStore, wipe
from murmur.voices.tagger import UNKNOWN, SpeakerTagger

DIM = 8
SR = 16_000


def vec(i: int, noise: float = 0.0) -> np.ndarray:
    """Unit vector for 'speaker i' (orthogonal across speakers)."""
    v = np.zeros(DIM, np.float32)
    v[i] = 1.0
    if noise:
        v[(i + 1) % DIM] = noise
    return v / np.linalg.norm(v)


def store_at(tmp_path: Path) -> VoiceStore:
    return VoiceStore(tmp_path / "voices" / "voices.db")


# --- matching -----------------------------------------------------------------


def test_first_voice_is_created_and_then_matched(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    a = s.identify(vec(0), ts=1, dur=3)
    assert a.voice_id is not None and a.label == f"voice-{a.voice_id}"
    b = s.identify(vec(0, noise=0.1), ts=2, dur=3)
    assert b.voice_id == a.voice_id and b.sim > 0.9


def test_different_voice_gets_new_id(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    a = s.identify(vec(0), ts=1, dur=3)
    b = s.identify(vec(1), ts=2, dur=3)
    assert a.voice_id != b.voice_id
    assert len(s.voices()) == 2


def test_undecided_zone_is_not_labelled(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    s.identify(vec(0), ts=1, dur=3)
    mid = vec(0) * 0.45 + vec(1) * np.sqrt(1 - 0.45**2)  # cosine 0.45 to voice 0
    r = s.identify(mid, ts=2, dur=3)
    assert r.voice_id is None and r.label is None
    assert len(s.voices()) == 1  # no junk voice created


def test_short_clip_does_not_start_a_new_voice(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    assert s.identify(vec(0), ts=1, dur=1.0).voice_id is None
    assert s.voices() == []


def test_thresholds_are_configurable(tmp_path: Path) -> None:
    s = VoiceStore(tmp_path / "v.db", Thresholds(match=0.4, new=0.3))
    s.identify(vec(0), ts=1, dur=3)
    mid = vec(0) * 0.45 + vec(1) * np.sqrt(1 - 0.45**2)
    assert s.identify(mid, ts=2, dur=3).voice_id is not None


# --- naming / privacy -------------------------------------------------------------


def test_name_merge_forget(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    a = s.identify(vec(0), ts=1, dur=3).voice_id
    b = s.identify(vec(1), ts=2, dur=3).voice_id
    s.name(f"voice-{a}", "Tolu Abimbola")
    assert s.resolve("tolu abimbola") == a
    # Naming a second voice with an existing name merges them (same person).
    assert s.name(b, "Tolu Abimbola") == a
    assert len(s.voices()) == 1
    assert s.forget("Tolu Abimbola") is True
    assert s.voices() == []


def test_library_is_owner_only(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    s.close()
    assert stat.S_IMODE(os.stat(tmp_path / "voices").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "voices" / "voices.db").st_mode) == 0o600


def test_export_has_names_but_no_voiceprints(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    vid = s.identify(vec(0), ts=time.time(), dur=3).voice_id
    s.name(vid, "Dami")
    out = tmp_path / ".voices.json"
    s.export_json(out)
    data = json.loads(out.read_text())
    assert data["voices"][0]["name"] == "Dami"
    assert "centroid" not in out.read_text() and "embedding" not in out.read_text()


def test_wipe_removes_everything(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    s.identify(vec(0), ts=1, dur=3)
    s.close()
    assert wipe(tmp_path / "voices" / "voices.db") is True
    assert not (tmp_path / "voices" / "voices.db").exists()


# --- learning ---------------------------------------------------------------------


def test_bootstrap_self_names_dominant_solo_voice(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    for i in range(250):  # 750 s of the user alone
        s.identify(vec(0), ts=i, dur=3, context=None)
    for i in range(20):  # someone on a call — doesn't count
        s.identify(vec(1), ts=1000 + i, dur=3, context="call")
    vid = s.bootstrap_self("Jay")
    assert vid is not None and s.resolve("Jay") == vid
    assert s.bootstrap_self("Jay") is None  # only once


def test_bootstrap_self_needs_enough_talk(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    for i in range(10):
        s.identify(vec(0), ts=i, dur=3)
    assert s.bootstrap_self("Jay") is None


def test_one_on_one_hint_names_the_other_voice(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    me = s.enroll([vec(0)], "Jay", source="self")
    day = datetime(2026, 10, 5, 14, 30).astimezone()
    t0 = day.timestamp()
    for i in range(10):
        s.identify(vec(0), ts=t0 + i * 10, dur=4, context="call")
        s.identify(vec(2), ts=t0 + i * 10 + 5, dur=4, context="call")
    hints = tmp_path / ".speaker-hints"
    hints.mkdir()
    (hints / "2026-10-05.json").write_text(json.dumps({"hints": [
        {"kind": "one_on_one", "start": "14:30", "end": "15:00", "person": "Dami Olawoye"},
    ]}))
    assert s.apply_hint_files(hints, "Jay") == 1
    dami = s.resolve("Dami Olawoye")
    assert dami is not None and dami != me
    assert s.apply_hint_files(hints, "Jay") == 0  # same file isn't re-applied


def test_hint_does_not_rename_an_already_named_voice(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    t0 = datetime(2026, 10, 5, 14, 30).astimezone().timestamp()
    vid = s.identify(vec(2), ts=t0, dur=30).voice_id
    s.name(vid, "Tolu")
    assert s.apply_one_on_one(t0 - 10, t0 + 100, "Dami", "Jay") is None
    assert s.resolve("Tolu") == vid


# --- turn tagging ---------------------------------------------------------------------


class FakeEmbedder:
    """Speaker id is encoded as the slice's constant amplitude (k * 0.01)."""

    def __init__(self) -> None:
        self.fail = False

    def embed(self, audio: np.ndarray) -> np.ndarray:
        if self.fail:
            raise RuntimeError("model missing")
        return vec(int(round(float(np.median(audio)) * 100)))


def clip(*parts: tuple[int, float]) -> np.ndarray:
    """Concatenate (speaker k, seconds) runs."""
    return np.concatenate([np.full(int(sec * SR), k * 0.01, np.float32) for k, sec in parts])


def seg(text: str, start: float, end: float) -> SegmentScore:
    return SegmentScore(text, 0.0, 1.0, -0.2, start=start, end=end)


def tagger(tmp_path: Path, emb: FakeEmbedder | None = None) -> SpeakerTagger:
    return SpeakerTagger(store_at(tmp_path), emb or FakeEmbedder(), self_name="Jay")  # type: ignore[arg-type]


def test_turns_split_by_speaker_and_merge_runs(tmp_path: Path) -> None:
    t = tagger(tmp_path)
    t.store.enroll([vec(1)], "Jay", source="self")
    audio = clip((1, 4), (2, 5), (2, 3))
    turns = t.turns(audio, 0.0, [seg("Hi team.", 0, 4), seg("Revenue is flat.", 4, 9), seg("Sorry.", 9, 12)], "call")
    assert [x.speaker for x in turns or []] == ["Jay", "voice-2"]
    assert turns is not None and turns[1].text == "Revenue is flat. Sorry." and turns[1].offset_s == 4


def test_short_segment_inherits_previous_speaker(tmp_path: Path) -> None:
    t = tagger(tmp_path)
    t.store.enroll([vec(1)], "Jay", source="self")
    audio = clip((1, 4), (2, 0.5))
    turns = t.turns(audio, 0.0, [seg("Let's ship it.", 0, 4), seg("Okay.", 4, 4.5)], None)
    assert turns is not None and len(turns) == 1 and turns[0].speaker == "Jay"


def test_no_timings_returns_none(tmp_path: Path) -> None:
    t = tagger(tmp_path)
    assert t.turns(np.zeros(SR, np.float32), 0.0, [seg("Hello there", 0, 0)], None) is None


def test_guard_dropped_segments_are_excluded(tmp_path: Path) -> None:
    t = tagger(tmp_path)
    turns = t.turns(clip((1, 4)), 0.0, [seg("Thanks for watching", 0, 4)], None)
    assert turns == []


def test_embedder_failure_degrades_to_unknown(tmp_path: Path) -> None:
    emb = FakeEmbedder()
    emb.fail = True
    t = tagger(tmp_path, emb)
    turns = t.turns(clip((1, 4)), 0.0, [seg("Still transcribed.", 0, 4)], None)
    assert turns is not None and turns[0].speaker == UNKNOWN and turns[0].text == "Still transcribed."


def test_maintenance_exports_and_learns(tmp_path: Path) -> None:
    out = tmp_path / ".voices.json"
    t = SpeakerTagger(store_at(tmp_path), FakeEmbedder(), self_name="Jay", export_path=out)  # type: ignore[arg-type]
    t.maintenance(force=True)
    assert json.loads(out.read_text())["voices"] == []


# --- worker integration -------------------------------------------------------------


class _Scripted:
    def __init__(self, result: TranscriptionResult) -> None:
        self.result = result

    def transcribe(self, audio, *, language, initial_prompt):  # type: ignore[no-untyped-def]
        return self.result


class _Label:
    def label(self, start: float, end: float) -> SegmentContext | None:
        return SegmentContext("call", "us.zoom.xos")


def test_worker_writes_one_entry_per_speaker_turn(tmp_path: Path) -> None:
    spool, transcripts = tmp_path / "spool", tmp_path / "Transcripts"
    spool.mkdir()
    start = datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC)
    write_segment_wav(spool, Segment(samples=clip((1, 4), (2, 5)), start_at=start))
    t = tagger(tmp_path)
    t.store.enroll([vec(1)], "Jay", source="self")
    result = TranscriptionResult("x", (seg("Hi team.", 0, 4), seg("Revenue is flat this month.", 4, 9)))
    clock = lambda: datetime(2026, 6, 12, 9, 1, tzinfo=UTC)  # noqa: E731
    stop, drained = threading.Event(), threading.Event()
    w = TranscribeWorker(
        spool_dir=spool, work_q=queue.Queue(), transcriber=_Scripted(result), fallback=None,
        writer=TranscriptWriter(transcripts, clock), status=StatusBoard(), keep_audio=False,
        language="en", initial_prompt=None, backlog_drained=drained, stop=stop,
        context=_Label(), speakers=t,
    )
    w.start()
    drained.wait(5)
    stop.set()
    w.join(5)
    text = next(transcripts.glob("*.md")).read_text()
    assert '<!-- murmur: call app=us.zoom.xos speaker="Jay" -->' in text
    assert '<!-- murmur: call app=us.zoom.xos speaker="voice-2" -->' in text
    assert text.count("**[") == 2


# --- config / CLI -----------------------------------------------------------------------


def test_config_voices_section(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text('[voices]\nenabled = false\nself_name = "Faith"\nmatch_threshold = 0.6\n')
    cfg, warnings = load_config(p)
    assert cfg.voices.enabled is False and cfg.voices.self_name == "Faith"
    assert cfg.voices.match_threshold == 0.6
    assert not [w for w in warnings if "Unknown" in w]


def test_cli_name_list_forget_wipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MURMUR_HOME", str(home))
    (home).mkdir()
    (home / "config.toml").write_text(f'[storage]\ntranscripts_dir = "{tmp_path / "T"}"\n')
    s = VoiceStore(home / "voices" / "voices.db")
    vid = s.identify(vec(3), ts=time.time(), dur=5).voice_id
    s.close()
    assert cli_main(["voices", "name", f"voice-{vid}", "Moses"]) == 0
    assert cli_main(["voices", "list"]) == 0
    assert "Moses" in capsys.readouterr().out
    assert json.loads((tmp_path / "T" / ".voices.json").read_text())["voices"][0]["name"] == "Moses"
    assert cli_main(["voices", "forget", "Moses"]) == 0
    assert cli_main(["voices", "wipe", "--yes"]) == 0
    assert not (home / "voices" / "voices.db").exists()


# --- real model (only where it's downloaded) ---------------------------------------------


@pytest.mark.skipif(not (model_dir() / "hyperparams.yaml").exists(), reason="speaker model not downloaded")
def test_real_embedder_separates_voices(tmp_path: Path) -> None:
    import subprocess

    from murmur.voices.embedder import SpeakerEmbedder

    def say(voice: str, text: str) -> np.ndarray:
        aiff, raw = tmp_path / f"{voice}.aiff", tmp_path / f"{voice}.f32"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), "-ar", "16000",
                        "-ac", "1", "-f", "f32le", str(raw)], check=True)
        return np.fromfile(raw, dtype=np.float32)

    emb = SpeakerEmbedder()
    a1 = emb.embed(say("Daniel", "We need to review the loan book numbers before Thursday."))
    a2 = emb.embed(say("Daniel", "Let's cut the onboarding flow down and measure conversion."))
    b = emb.embed(say("Karen", "We need to review the loan book numbers before Thursday."))
    assert float(a1 @ a2) > 0.6 > float(a1 @ b)
