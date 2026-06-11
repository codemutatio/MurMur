# Plan: Murmur — Personal Always-On Voice Transcription for macOS

**Run ID:** murmur-2026-06-11 · **Spec:** runs/murmur-2026-06-11/spec.md · **Date:** 2026-06-11 · **Size:** L

## Approach

Build a single Python 3.11 package (`murmur`) managed with `uv`, exposing one CLI (`murmur run | cat | copy | bench | install-agent | uninstall-agent`) plus a standalone `scripts/bakeoff.py`. The runtime is one process with a fixed set of threads: a PortAudio callback feeding a bounded frame queue, a capture-engine thread (stream lifecycle + Silero VAD + segmentation + spool writes), a transcription worker thread (mlx-whisper, FIFO over spool), a pynput listener thread, and the main thread running rumps (or a headless wait loop). Every hardware/OS boundary — audio source, VAD, transcriber, notifier, clock, disk-free probe, permissions — is a small injectable interface, so the entire core pipeline runs in the default test suite with fakes: no microphone, no GUI, no model downloads. The spool directory doubles as the durable work queue (filenames encode segment start time), which gives crash recovery for free. Verified environment realities baked into this design: machine has Python 3.11.2 + uv 0.11.11, **no ffmpeg** (so audio is always passed to mlx-whisper as numpy arrays, never file paths), macOS 26.4 arm64, and PortAudio caches its device list at init (so stream reopen must reinitialize PortAudio).

## Architecture

```
                       MAIN THREAD (T0)
        rumps.App (menubar) ── 1 s rumps.Timer pulls StatusBoard
        or headless wait loop          │ menu actions → Controller
                                       ▼
   ┌────────────── Controller / StateStore (thread-safe) ◄── pynput GlobalHotKeys (T5, own thread)
   │                                   ▲
   ▼ pause/resume/effective state      │ status updates
CAPTURE ENGINE THREAD (T2+T3 merged)   │
  owns: InputStream lifecycle, device poll (2 s),         TRANSCRIPTION WORKER THREAD (T4)
  watchdog (3 s stall → reopen), disk gate (60 s),          owns: spool consumption, mlx-whisper,
  mic-permission gate (60 s recheck),                       hallucination guard, transcript writes,
  Silero VAD + Segmenter + spool WAV writes                 WAV deletion, retries, API fallback
       ▲ frames_q (queue.Queue, maxsize 100 ≈ 3.2 s)            ▲ work_q (queue.Queue[Path]) + startup scan
       │                                                        │
  PortAudio callback (T1, owned by sounddevice)            spool dir  ~/Transcripts/spool/*.wav
  → frames_q.put_nowait, drop-counter on full              (filenames = segment start timestamps)
                                                                │ success
                                                                ▼
                                                  ~/Transcripts/YYYY-MM-DD.md (atomic rewrite+replace)
```

**Thread/ownership rules (load-bearing, see ADR-1):**
- Only T0 touches rumps/AppKit objects. Menubar reads a `StatusSnapshot` via `rumps.Timer`; it never receives callbacks from worker threads.
- Only the capture-engine thread touches the `InputStream` object and the VAD model (Silero is not used from any other thread; `torch.set_num_threads(1)`).
- Only the worker thread writes transcript files and deletes spool WAVs.
- The PortAudio callback thread does exactly one thing: `frames_q.put_nowait`.
- `Controller`, `StateStore`, `StatusBoard` are the only cross-thread mutable objects; each guards its state with a single `threading.Lock`.
- Notifications go through `Notifier` (osascript subprocess, fire-and-forget) — safe from any thread.

**Startup sequence (`murmur run`):** acquire lockfile (first action, before heavy imports — AC-6's 2 s bound) → load config + state → retention prune → start worker thread (ensure model w/ progress → scan spool backlog → drain → set `backlog_drained` event → live loop) → start capture engine (blocks on `backlog_drained` before first stream open — R-16/E-6) → start hotkey listener (if Accessibility granted) → run rumps on main thread (or headless wait).

**Pause path (AC-23 ≤ 1 s):** hotkey/menu → `Controller.toggle_pause()` → persists to state.json → fires notification → capture engine notices on next loop tick (≤ 0.5 s read timeout) → flushes partial segment to spool → closes stream (mic indicator off, CPU idle).

**Device change / sleep-wake (R-21, R-22, E-3–E-5):** one mechanism covers all: (a) poll default-device signature every 2 s, (b) watchdog: no frame for > 3 s while stream open and not paused. Either trigger → flush partial segment → close stream → reinitialize PortAudio (`sd._terminate()` / `sd._initialize()`, required because PortAudio snapshots the device list at init) → reopen on current default. Open failure → ⚠ "No input device", retry every 5 s (E-4). Wake gap stays under E-5's 5 s budget (3 s watchdog + ~1 s reopen).

### Repository layout (greenfield — this plan defines it)

```
pyproject.toml            # uv-managed; [project.scripts] murmur = "murmur.cli:main"
.python-version           # 3.11
config.example.toml
launchd/com.jay.murmur.plist.template
scripts/bakeoff.py        # standalone (R-27); reuses murmur.transcribe.*; excluded from coverage gate
src/murmur/
  __init__.py  __main__.py  cli.py  config.py  paths.py  log.py
  lock.py                 # single-instance flock
  control.py              # Controller, StateStore, PauseState, StatusBoard, IconState
  runtime.py              # Runtime supervisor: wiring, start/stop, headless loop
  audio/  source.py  vad.py  segmenter.py  spool.py
  transcribe/  base.py  prompt.py  local.py  api.py  guard.py  worker.py  models.py
  output/  writer.py  retention.py
  mac/  notify.py  permissions.py  clipboard.py  launchd.py  keychain.py
  app/  menubar.py  hotkey.py
tests/
  conftest.py  fakes.py   # FakeAudioSource, AmplitudeVAD, FakeTranscriber, RecordingNotifier, FixedClock
  unit/  integration/
docs/  manual-verification.md  README.md (repo root)
```

## Contracts

### Constants (single module `murmur/audio/__init__.py` and `murmur/transcribe/guard.py`)

```python
SAMPLE_RATE = 16_000                 # Hz, mono (R-1)
FRAME_SAMPLES = 512                  # Silero v5+ requires exactly 512-sample windows at 16 kHz → 32 ms frames
PRE_ROLL_S = 0.3                     # → 10 frames = 320 ms (within AC-15 ±50 ms tolerance)
POST_ROLL_S = 0.3
WATCHDOG_STALL_S = 3.0
DEVICE_POLL_S = 2.0
DISK_MIN_FREE_BYTES = 2 * 1024**3    # R-20
DISK_POLL_S = 60.0
PERMISSION_RECHECK_S = 60.0
MAX_LOCAL_FAILURES = 3               # E-9

# guard.py (OQ-2 — named constants, not user config in v1; see Plan Notes re: corrected semantics)
NO_SPEECH_PROB_MAX = 0.6             # drop when no_speech_prob > 0.6 AND avg_logprob < AVG_LOGPROB_MIN
AVG_LOGPROB_MIN = -1.0
COMPRESSION_RATIO_MAX = 2.4          # drop when compression_ratio > 2.4 (repetition hallucination)
FILLER_PHRASES: frozenset[str]       # normalized: "thank you for watching", "thanks for watching",
                                     # "subtitles by the amara.org community", "you", "thank you.", …
```

### Config (`murmur/config.py`) — tomllib (stdlib), frozen dataclasses

```python
@dataclass(frozen=True)
class CaptureConfig:  device: str = "default"; vad_threshold: float = 0.5
                      silence_split_s: float = 1.5; max_segment_s: float = 120.0
@dataclass(frozen=True)
class SttConfig:      engine: Literal["local","api"] = "local"; local_model: str = "large-v3"
                      api_model: str = "gpt-4o-transcribe"; api_fallback: bool = False
                      language: str = "en"; vocab_hints: tuple[str, ...] = (PRD §5 defaults)
@dataclass(frozen=True)
class StorageConfig:  transcripts_dir: Path = ~/Transcripts (expanded); spool_dir: Path = transcripts_dir/"spool"
                      keep_audio: bool = False; retention_days: int = 365
@dataclass(frozen=True)
class HotkeyConfig:   toggle: str = "ctrl+alt+cmd+m"
@dataclass(frozen=True)
class NotesConfig:    enabled: bool = False        # v2 — parsed, ignored, never acted on
@dataclass(frozen=True)
class Config:         capture; stt; storage; hotkey; notes

def load_config(path: Path | None) -> tuple[Config, list[str]]
# Missing/unparseable file → all defaults + warning naming the expected path (E-16). Never raises.
# Unknown keys → ignored with logged warning per key (E-17). Type errors on known keys → default + warning.
```

### Paths (`murmur/paths.py`) — everything injectable for tests

```python
@dataclass(frozen=True)
class AppPaths:
    home: Path          # default ~/Library/Application Support/Murmur; overridden by $MURMUR_HOME
    config_file: Path   # home/config.toml unless --config given
    state_file: Path    # home/state.json (OQ-3 default)
    lock_file: Path     # home/murmur.lock
    log_file: Path      # home/murmur.log

def resolve_paths(env: Mapping[str, str], config_override: Path | None) -> AppPaths  # mkdir -p home
```

### API keys (`murmur/mac/keychain.py`) — R-25; launchd agents do NOT inherit shell env

```python
def get_openai_api_key(env: Mapping[str, str], run=subprocess.run) -> str | None
# Order: env["OPENAI_API_KEY"] → `security find-generic-password -s murmur-openai -w` (rc≠0 → None).
# Never logged. Keychain item documented in README: security add-generic-password -s murmur-openai -a "$USER" -w
```

### State & control (`murmur/control.py`)

```python
class PauseMode(StrEnum): MANUAL="manual"; TIMED="timed"; REST_OF_DAY="rest_of_day"

@dataclass(frozen=True)
class PauseState: mode: PauseMode | None = None; until: datetime | None = None  # tz-aware local
# JSON schema state.json: {"version": 1, "pause": {"mode": "rest_of_day", "until": "2026-06-12T00:00:00+01:00"}}
# REST_OF_DAY stores until = next local midnight at set time → AC-16/17 are pure now-vs-until comparisons.
# MANUAL: until = None (pause until explicit resume). Corrupt/missing file → not paused (recording).

class StateStore:
    def __init__(self, path: Path, now: Callable[[], datetime]) -> None
    def load(self) -> PauseState          # never raises
    def save(self, s: PauseState) -> None # tmp+fsync+os.replace

def is_pause_active(s: PauseState, now: datetime) -> bool
# MANUAL → True; until set → now < until; expired/none → False

class IconState(StrEnum): RECORDING="●"; PAUSED="⏸"; ERROR="⚠"   # OQ-4: unicode title strings

@dataclass(frozen=True)
class StatusSnapshot:
    icon: IconState; queue_depth: int; error_count: int
    pause_until: datetime | None; notice: str | None         # e.g. "No input device", "Accessibility needed"
    download: tuple[int, int] | None                          # (bytes_done, bytes_total) during model fetch

class StatusBoard:   # thread-safe; sole cross-thread status object
    def snapshot(self) -> StatusSnapshot
    def set_icon/set_queue_depth/increment_errors/set_notice/set_download(...) -> None

class Controller:    # thread-safe; the ONLY entry point for pause state changes
    def __init__(self, store: StateStore, notifier: Notifier, now: Callable[[], datetime]) -> None
    def toggle_pause(self) -> bool                       # returns new paused?; fires "Paused"/"Recording" notification (R-13)
    def pause_for(self, seconds: float) -> None          # TIMED, until = now + seconds (30 min / 1 h menu items)
    def pause_rest_of_day(self) -> None                  # until = next local midnight (R-14)
    def resume(self) -> None
    def effective_paused(self) -> bool                   # is_pause_active(store.load(), now()) — auto-expiry, no timers
```

### Audio source (`murmur/audio/source.py`)

```python
class AudioSourceError(Exception): ...

class AudioSource(Protocol):
    def open(self, device: str) -> None                  # raises AudioSourceError
    def close(self) -> None                              # idempotent
    def read(self, timeout: float) -> np.ndarray | None  # one float32 frame shape (512,), None on timeout
    def default_device_signature(self) -> str            # opaque id of current default input ("" if none)

class SounddeviceSource(AudioSource):
    # open(): sd._terminate(); sd._initialize() first (PortAudio device-list cache), then
    # InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=512, callback=cb).start()
    # cb: frames_q.put_nowait(indata.copy().reshape(-1)); queue.Full → dropped_frames += 1
    # default_device_signature(): name of sd.query_devices(kind="input") after refresh; "" on PortAudioError
```

`FakeAudioSource` (tests/fakes.py): scripted frame sequences, scripted signature changes, scripted stalls (read returns None), scripted open failures — drives all capture-engine unit tests.

### VAD (`murmur/audio/vad.py`)

```python
class VoiceDetector(Protocol):
    def speech_prob(self, frame: np.ndarray) -> float    # 0..1 for one 512-sample frame
    def reset(self) -> None

class SileroVAD(VoiceDetector):
    # silero-vad pip package (model bundled in wheel — no network in tests/runtime):
    # from silero_vad import load_silero_vad; model = load_silero_vad()  # torch JIT, CPU
    # torch.set_num_threads(1)  ← keeps idle CPU < 5% (AC-21c)
    # speech_prob: model(torch.from_numpy(frame), 16000).item(); reset: model.reset_states()
    # Thread affinity: constructed and used only on the capture-engine thread.
```

`AmplitudeVAD` (tests/fakes.py): `1.0 if rms(frame) > thresh else 0.0` — makes segmenter tests deterministic.

### Segmenter (`murmur/audio/segmenter.py`) — pure logic, no I/O

```python
@dataclass(frozen=True)
class SegmenterConfig:
    vad_threshold: float; silence_split_s: float; max_segment_s: float
    pre_roll_s: float = PRE_ROLL_S; post_roll_s: float = POST_ROLL_S
    offset_hysteresis: float = 0.15   # frame is "silence" only when prob < vad_threshold - 0.15

@dataclass(frozen=True)
class Segment:
    samples: np.ndarray               # float32 mono 16 kHz, pre/post-roll included
    start_at: datetime                # wall clock of first included sample (drives the **[HH:MM]** stamp)

class Segmenter:
    def __init__(self, cfg: SegmenterConfig, vad: VoiceDetector, now: Callable[[], datetime]) -> None
    def push(self, frame: np.ndarray) -> list[Segment]   # 0..n completed segments
    def flush(self) -> Segment | None                    # close active segment (pause/shutdown/device-change)
# State machine: IDLE (ring buffer keeps last 10 frames for pre-roll; silence discarded — R-2)
#   → onset (prob ≥ threshold): segment starts at ring-buffer content (≤ 320 ms pre-roll; less if unavailable — spec assumption)
#   → ACTIVE: close after ≥ 47 consecutive silence frames (1.504 s ≥ silence_split_s); content ends at
#     last-speech-frame + 10 post-roll frames; trailing silence beyond post-roll discarded (R-3)
#   → force-split when active buffer ≥ max_segment_s: split at min-RMS frame in active region; part 1 emitted,
#     remainder stays ACTIVE; no samples discarded (E-19, AC-13)
```

### Spool (`murmur/audio/spool.py`) — the durable queue

```python
def write_segment_wav(spool_dir: Path, seg: Segment) -> Path
# 16-bit PCM mono 16 kHz via stdlib wave; name f"{start_at:%Y%m%d-%H%M%S}-{millis:03d}.wav";
# write tmp + fsync + os.replace (no partial WAVs ever visible); OSError propagates (caller: E-14)
def parse_segment_start(path: Path) -> datetime          # filename → tz-aware local dt (crash recovery keeps timestamps)
def list_spool(spool_dir: Path) -> list[Path]            # *.wav sorted by name == chronological (R-16 FIFO)
def load_wav(path: Path) -> np.ndarray                   # stdlib wave → float32; NO ffmpeg on this machine
```

### Transcription (`murmur/transcribe/`)

```python
# base.py
class TranscriptionError(Exception): ...
@dataclass(frozen=True)
class SegmentScore: text: str; no_speech_prob: float; compression_ratio: float; avg_logprob: float
@dataclass(frozen=True)
class TranscriptionResult: text: str; segments: tuple[SegmentScore, ...]
class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray, *, language: str, initial_prompt: str | None) -> TranscriptionResult
    # raises TranscriptionError on failure

# prompt.py
def build_initial_prompt(vocab_hints: Sequence[str]) -> str | None
# None when empty; else "Glossary: Rivy, Yassir, …." — every hint appears verbatim (AC-18)

# local.py
class MlxWhisperTranscriber(Transcriber):
    def __init__(self, model_ref: str) -> None           # local snapshot path or HF repo id from ModelManager
    # mlx_whisper.transcribe(audio, path_or_hf_repo=model_ref, language=…, initial_prompt=…,
    #                        condition_on_previous_text=False, verbose=None)
    # audio passed as float32 np.ndarray ALWAYS (no ffmpeg installed). Maps result["segments"] → SegmentScore.
def assert_apple_silicon() -> None                       # platform.machine()=="arm64" and sys.platform=="darwin",
                                                         # else raise with actionable message (E-24); called before model init

# api.py
class OpenAITranscriber(Transcriber):
    def __init__(self, model: str, api_key: str, client_factory: Callable[..., Any] = OpenAI) -> None
    # encodes np.ndarray → in-memory 16 kHz WAV → client.audio.transcriptions.create(model=…, file=…, language=…, prompt=…)
    # API results get no_speech_prob=0.0/compression_ratio=1.0 sentinels (guard still checks filler phrases)

# guard.py
def clean_result(r: TranscriptionResult) -> str | None
# Per Whisper-segment: drop if (no_speech_prob > 0.6 and avg_logprob < -1.0) or compression_ratio > 2.4
# or normalized text ∈ FILLER_PHRASES or text empty. Join survivors with " ".
# Returns None when nothing survives → caller treats as no-speech: no append, no error count (E-7, E-8, E-20, AC-3).

# models.py
MODEL_REPOS = {"large-v3": "mlx-community/whisper-large-v3-mlx",
               "large-v3-turbo": "mlx-community/whisper-large-v3-turbo"}
class ChecksumError(Exception): ...
class ModelManager:
    def __init__(self, cache_dir: Path | None = None, api: HfApi | None = None,
                 progress: Callable[[int, int], None] | None = None) -> None
    def ensure(self, model: str) -> str
    # snapshot_download(repo, tqdm_class=hook→progress) → for each LFS file, sha256(local) vs
    # HfApi().model_info(repo, files_metadata=True) lfs.sha256 (huggingface_hub does NOT reliably verify itself —
    # confirmed open issues) → write {home}/model-verified-{model}.json marker → return snapshot path.
    # Marker present → skip re-hash (fast startup). Mismatch → delete snapshot dir, raise ChecksumError (E-18).
    # File lacking lfs metadata → size check + logged warning (honest degradation).

# worker.py
class TranscribeWorker(threading.Thread):
    def __init__(self, *, spool_dir, work_q: queue.Queue[Path], transcriber: Transcriber,
                 fallback: Transcriber | None, writer: TranscriptWriter, status: StatusBoard,
                 keep_audio: bool, language: str, initial_prompt: str | None,
                 backlog_drained: threading.Event, stop: threading.Event,
                 max_local_failures: int = 3, backoff_s: float = 30.0) -> None
# Loop: startup scan via list_spool (R-16, E-6) then live work_q, strict FIFO (R-4).
# Per WAV: load → transcribe (local). Failure → retry; 3rd consecutive local failure → fallback.transcribe()
#   if provided (AC-8, E-9) else leave WAV, status.increment_errors(), skip to next (AC-7). Fallback failure →
#   leave WAV, increment errors, backoff_s sleep (E-10). NO network object is ever constructed when fallback is None.
# Success → clean_result → text? writer.append(start_at, text) : silently skip → delete WAV unless keep_audio
#   (AC-1, AC-2, R-6). Append-then-delete = at-least-once semantics (accepted; see Risks).
# Throughput: logs per-segment RTF (R-10 evidence; consumed by `murmur bench` for AC-20).
```

### Output (`murmur/output/`)

```python
# writer.py
class TranscriptWriter:
    def __init__(self, transcripts_dir: Path, now: Callable[[], datetime]) -> None
    def append(self, start_at: datetime, text: str) -> Path
# Target {dir}/{start_at:%Y-%m-%d}.md. Created on first append only (AC-5) with header "# Transcript — YYYY-MM-DD\n\n".
# Line: f"**[{start_at:%H:%M}]** {text}\n\n" (AC-4, local time).
# Durability (ADR-2): read existing content (missing → recreate, E-12) + new line → tmp file in same dir →
# flush + fsync → os.replace. File is never observable in a partial state (R-9).

# retention.py
def prune_transcripts(transcripts_dir: Path, retention_days: int, today: date) -> list[Path]
# 0 → no-op (AC-10). Else delete files matching ^\d{4}-\d{2}-\d{2}\.md$ whose filename date < today - retention_days
# (strictly older than the window: 8-day-old deleted, 7-day-old kept at retention 7 — AC-9). OSError → log, skip (E-22).
```

### Capture engine (`murmur/audio/engine.py`)

```python
class CaptureEngine(threading.Thread):
    def __init__(self, *, source: AudioSource, segmenter: Segmenter, spool_dir: Path,
                 work_q: queue.Queue[Path], controller: Controller, status: StatusBoard,
                 notifier: Notifier, mic_permission: Callable[[], PermissionStatus],
                 disk_free: Callable[[], int], now: Callable[[], datetime],
                 backlog_drained: threading.Event, stop: threading.Event,
                 device: str = "default") -> None
# Tick loop (≤ 0.5 s): wait backlog_drained once. Gates, in order:
#   paused (controller.effective_paused) → ensure closed, icon ⏸
#   mic permission ≠ GRANTED → ensure closed, icon ⚠ + one-shot notification w/ System Settings instructions,
#     recheck 60 s; auto-recover on grant (E-1, R-18)
#   disk_free() < 2 GiB → flush, close, icon ⚠, one-shot notification; recheck 60 s; auto-resume + notification
#     on recovery (R-20, E-13, AC-19)
# Open stream when all gates clear; icon ●. read(0.5) → segmenter.push → write_segment_wav → work_q.put;
#   spool write OSError → drop segment, log, increment_errors, notify once (E-14).
# Watchdog: open ∧ no frame > 3 s → flush, close, reopen (E-5 sleep/wake; E-3 device vanish).
# Device poll 2 s: signature change → flush, close, reopen (R-22, E-3). Open failure → icon ⚠ "No input device",
#   retry 5 s (E-4). All timings via injected now()/monotonic for testability.
```

### macOS adapters (`murmur/mac/`)

```python
# notify.py — osascript, NOT rumps.notification (broken in venvs: requires Info.plist hack, deprecated API)
class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None
class OsascriptNotifier(Notifier):
    # daemon-thread subprocess.run(["osascript", "-e", 'display notification "msg" with title "title"'],
    # timeout=5, check=False); quotes escaped; NEVER raises, NEVER blocks caller (constraint: silent when denied)

# permissions.py
class PermissionStatus(StrEnum): GRANTED; DENIED; UNDETERMINED
def microphone_permission() -> PermissionStatus    # AVFoundation AVCaptureDevice.authorizationStatusForMediaType_("soun")
def request_microphone_access() -> None            # triggers TCC prompt when UNDETERMINED (fire-and-forget)
def accessibility_trusted() -> bool                # ApplicationServices.AXIsProcessTrusted() (R-19, E-2)

# clipboard.py
def copy_to_clipboard(text: str, run=subprocess.run) -> None   # pbcopy via stdin; CalledProcessError propagates

# launchd.py
PLIST_LABEL = "com.jay.murmur"
def render_plist(murmur_bin: Path, log_path: Path, *, run_at_load: bool = True,
                 program_args: list[str] | None = None) -> str
# Keys: Label, ProgramArguments=[murmur_bin, "run"], RunAtLoad, KeepAlive=false (Quit must stick),
# StandardOutPath/StandardErrorPath=log_path, ProcessType=Interactive, LimitLoadToSessionType=Aqua.
# program_args override exists so tests can load an inert variant (/usr/bin/true) — see AC-29 strategy.
def install_agent(plist_dir: Path = ~/Library/LaunchAgents) -> Path   # render+write+`launchctl bootstrap gui/$UID`
def uninstall_agent(plist_dir: Path = ...) -> None                    # `launchctl bootout` + rm
```

### App layer (`murmur/app/`)

```python
# hotkey.py
def to_pynput_spec(toggle: str) -> str        # "ctrl+alt+cmd+m" → "<ctrl>+<alt>+<cmd>+m"; ValueError on unknown token
class HotkeyListener:
    def __init__(self, toggle: str, on_toggle: Callable[[], None]) -> None
    def start(self) -> bool                   # False when not accessibility_trusted() or pynput raises →
                                              # caller sets persistent menubar notice, app continues (E-2, R-19)
    def stop(self) -> None
# pynput.keyboard.GlobalHotKeys runs its own thread; callback only calls controller.toggle_pause() (non-blocking)

# menubar.py — main thread only
class MurmurApp(rumps.App):
    def __init__(self, controller: Controller, status: StatusBoard, runtime: Runtime,
                 transcripts_dir: Path) -> None
# title = snapshot.icon.value, refreshed by rumps.Timer(1.0) — pull model, no cross-thread rumps calls (R-11)
# Menu (R-12): Pause/Resume (label flips) | Pause for 30 min | Pause for 1 h | Pause for rest of day |
#   Open today's transcript (`open` cmd; absent file → notification) | Open transcripts folder |
#   Generate notes now (callback=None → renders disabled, v1 no-op) | Quit (runtime.stop() then rumps.quit_application())
# Dropdown header lines: "Queue: N · Errors: M" + notice/download-progress line when set
```

### Runtime & CLI (`murmur/runtime.py`, `murmur/cli.py`)

```python
class AlreadyRunningError(Exception): pid: int

class SingleInstanceLock:                      # murmur/lock.py
    def acquire(self) -> None                  # fcntl.flock(LOCK_EX|LOCK_NB) on lock_file; writes own PID;
                                               # held for process lifetime; raises AlreadyRunningError(pid) (stale-safe: flock dies with process)
    def release(self) -> None

class Runtime:
    def __init__(self, config: Config, paths: AppPaths, *,
                 source: AudioSource | None = None, vad: VoiceDetector | None = None,
                 transcriber: Transcriber | None = None, fallback: Transcriber | None = None,
                 notifier: Notifier | None = None, now: Callable[[], datetime] = local_now,
                 disk_free: Callable[[], int] | None = None,
                 mic_permission: Callable[[], PermissionStatus] | None = None,
                 model_manager: ModelManager | None = None) -> None
    # None → production adapter; tests inject fakes. local_now() = datetime.now().astimezone().
    def start(self) -> None        # lock → state → prune retention → threads per startup sequence above
    def stop(self, timeout: float = 10.0) -> None   # flush partial segment, join threads, release lock
    def wait(self) -> None         # headless: block on stop event / SIGINT / SIGTERM

# cli.py — argparse (stdlib). Exit codes: 0 ok; 1 error (incl. AlreadyRunningError → log
# "Murmur already running (PID: N)" to stderr, exit 1 — AC-6, E-15); 2 usage.
# murmur run [--headless] [--config PATH]   # lock acquired BEFORE importing torch/mlx (lazy imports) — AC-6 2 s bound
# murmur cat today [--config PATH]          # full file → stdout, exit 0; missing → stderr msg, exit 1 (AC-11)
# murmur copy today [--config PATH]         # pbcopy, exit 0 (AC-12)
# murmur bench FILE.WAV [--config PATH]     # 16 kHz mono WAV → SileroVAD+Segmenter offline → temp spool →
#                                           # worker w/ real mlx → prints: audio s, wall s, RTF, segment count;
#                                           # exit 0 iff RTF ≥ 2.0. The one-command harness for MV-20 (AC-20).
# murmur install-agent | uninstall-agent    # R-15
```

### bakeoff (`scripts/bakeoff.py`) — standalone (R-27, AC-26, E-23)

```
usage: uv run python scripts/bakeoff.py [--input FILE.wav] [--duration 600] [--output bakeoff-<date>.md]
No --input → interactive confirmation prompt, then records N s @16 kHz mono via sounddevice to bakeoff-<date>.wav (E-23).
Transcribes the SAME audio through: mlx large-v3, mlx large-v3-turbo, OpenAI gpt-4o-mini-transcribe, gpt-4o-transcribe
(reusing MlxWhisperTranscriber/OpenAITranscriber; arrays, no ffmpeg). Output: ONE markdown file, four labeled
"## <engine>" sections + per-engine wall-time. Missing OPENAI_API_KEY → API sections contain "SKIPPED: …"
(script completes; manual pass for AC-26 requires the key set — stated in runbook).
```

### Configuration summary (new env/config surface)

| Item | Default | Read by |
|---|---|---|
| `config.toml` (full schema above, PRD §5) | see dataclass defaults | `load_config` at startup; `cat/copy/bench` read storage section |
| `$MURMUR_HOME` | `~/Library/Application Support/Murmur` | `resolve_paths` (test seam + relocatability) |
| `$MURMUR_CONFIG` / `--config` | `$MURMUR_HOME/config.toml` | CLI |
| `$OPENAI_API_KEY` → Keychain `murmur-openai` | unset | `get_openai_api_key` (E-21: fallback on + no key → warn, treat as off, menubar notice) |
| `state.json` | not paused | StateStore |
| `retention_days` | 365 (`0` = forever) | prune at launch (R-26) |

## Task Breakdown

| # | Task | Satisfies | Done when | Proved by (tests) |
|---|------|-----------|-----------|-------------------|
| T-1 | Scaffold: uv project (`pyproject.toml`, `.python-version` 3.11, src layout), pytest+coverage config with `hardware` marker excluded by default, ruff + mypy (strict, missing-stub overrides for rumps/pynput/mlx_whisper/silero_vad/sounddevice/AVFoundation), `paths.py`, `log.py`, `.gitignore`, README stub | — (foundation; enables all) | `uv run pytest`, `uv run ruff check .`, `uv run mypy src` all green on a smoke test; `murmur --help` runs via entry point | `tests/unit/test_paths.py::test_murmur_home_env_override`, `test_smoke.py` |
| T-2 | Config loader: dataclasses, defaults, missing/unparseable→defaults+warning, unknown-key warnings, key sourcing (env→Keychain) | AC-30; R-25; E-16, E-17, E-21 | `load_config` returns exact configured values for `silence_split_s`/`max_segment_s`; all edge behaviors logged not raised; no key material ever in Config repr/logs | `test_config.py::test_values_applied [AC-30]`, `::test_missing_file_defaults [E-16]`, `::test_unknown_key_warns [E-17]`, `test_keychain.py::test_env_then_keychain_order`, `::test_fallback_disabled_without_key [E-21]` |
| T-3 | Segmenter + `VoiceDetector` protocol + spool WAV I/O: ring buffer, pre/post-roll, 1.5 s silence close, 120 s force-split at min-RMS, timestamp filenames, tmp+rename WAV writes (riskiest pure logic — first) | AC-13, AC-14, AC-15; R-2, R-3; E-19, E-20 | `Segmenter.push/flush` pass all boundary tests with `AmplitudeVAD`; silence never reaches disk; spool round-trips timestamps | `test_segmenter.py::test_force_split_at_quietest_point_121s [AC-13]`, `::test_1_6s_gap_two_segments [AC-14]`, `::test_pre_post_roll_within_50ms [AC-15]`, `::test_silence_never_buffered [R-2]`, `::test_short_blip_emitted [E-20]`, `test_spool.py::test_wav_roundtrip_and_filename_timestamp` |
| T-4 | `SileroVAD` adapter (torch backend, bundled model, `set_num_threads(1)`) + integration test on `say`-generated speech fixture | R-2 (real VAD conformance); underpins AC-21, AC-24 | Adapter satisfies protocol; real speech detected / digital silence rejected on a fixture generated at test time by macOS `say` (no binary fixtures, no downloads) | `tests/integration/test_silero.py::test_detects_say_speech`, `::test_rejects_silence` |
| T-5 | `TranscriptWriter` (atomic rewrite+replace, header, line format, lazy file creation, mid-day deletion recovery) + retention pruning | AC-4, AC-5, AC-9, AC-10; R-8, R-9, R-26; E-11, E-12, E-22 | Append durable per ADR-2; silent day creates nothing; pruning math exact at the 7/8-day boundary | `test_writer.py::test_line_format_local_time [AC-4]`, `::test_no_file_until_first_segment [AC-5]`, `::test_recreates_deleted_file [E-12]`, `::test_creates_dirs [E-11]`, `test_retention.py::test_prunes_8d_keeps_7d [AC-9]`, `::test_zero_keeps_forever [AC-10]`, `::test_skips_locked_file [E-22]` |
| T-6 | Hallucination guard with OQ-2 constants + filler list + empty-result handling | AC-3; R-5; E-7, E-8 | `clean_result` drops no-speech/filler/repetition per contract; survivors joined; total drop → None | `test_guard.py::test_drops_high_no_speech_prob [AC-3]`, `::test_drops_filler_phrase [AC-3]`, `::test_drops_high_compression_ratio`, `::test_empty_text_is_no_speech [E-7]`, `::test_partial_drop_keeps_rest [E-8]` |
| T-7 | Transcriber protocol + `build_initial_prompt` + `MlxWhisperTranscriber` (array input, score mapping) + `assert_apple_silicon` + `FakeTranscriber` | AC-18; R-4; E-24; underpins AC-24 | mlx call receives `language`, `initial_prompt` containing every hint verbatim (mocked `mlx_whisper.transcribe`); arm64 gate raises actionable error elsewhere | `test_prompt.py::test_hints_verbatim [AC-18]`, `test_local.py::test_initial_prompt_passed [AC-18]`, `::test_array_not_path_passed`, `::test_non_arm64_actionable_error [E-24]` |
| T-8 | `OpenAITranscriber`: WAV encoding, injected client factory, error mapping | R-7 (API leg); E-10; underpins AC-8 | Fake client receives correct model/file/language/prompt; API errors → `TranscriptionError` | `test_api.py::test_request_shape`, `::test_error_maps_to_transcription_error [E-10]` |
| T-9 | `TranscribeWorker`: startup scan, FIFO, 3-failure policy, fallback hand-off, backoff, keep_audio delete, error counts, RTF logging | AC-1, AC-2, AC-7, AC-8; R-4, R-6, R-7, R-16; E-6, E-9, E-10 | All worker policies pass with `FakeTranscriber`; zero network objects constructed when fallback off | `test_worker.py::test_success_deletes_wav [AC-1]`, `::test_keep_audio_retains_wav [AC-2]`, `::test_fallback_off_leaves_wav_increments_error_no_api [AC-7]`, `::test_third_failure_invokes_fallback [AC-8]`, `::test_startup_scan_fifo_before_live [E-6]`, `::test_api_failure_backoff [E-10]` |
| T-10 | `scripts/bakeoff.py` (early per OQ-1 — runnable as soon as transcribers exist, so Jay can record real data mid-build) | AC-26 (manual); R-27; E-23 | Script produces one MD file with 4 labeled sections from `--input`; record-prompt path implemented; logic reuses T-7/T-8 transcribers | `test_bakeoff.py::test_four_sections_with_fakes`, `::test_missing_key_sections_marked_skipped`; MV-26 |
| T-11 | StateStore + PauseState + Controller + StatusBoard (clock-injected pause semantics, persistence, notifications on toggle) | AC-16, AC-17; R-13 (notification part), R-14; OQ-3 | Rest-of-day pause survives "relaunch" before midnight, expires after; toggle fires notifier; state file corruption → recording | `test_state.py::test_rest_of_day_persists_before_midnight [AC-16]`, `::test_expires_after_midnight [AC-17]`, `::test_corrupt_state_defaults_to_recording`, `test_controller.py::test_toggle_fires_notification`, `::test_timed_pause_auto_expires` |
| T-12 | CaptureEngine: gate ordering (pause/permission/disk), watchdog, device-poll, reopen w/ PortAudio reinit, spool-write failure path, backlog wait | AC-19; R-1, R-20, R-21, R-22; E-1, E-3, E-4, E-5, E-13, E-14; underpins AC-21, AC-22, AC-25 | All behaviors pass against `FakeAudioSource` with scripted stalls/signature-changes/failures; `SounddeviceSource` open() does terminate/initialize/reopen | `test_engine.py::test_disk_low_pauses_then_resumes_with_notifications [AC-19]`, `::test_device_signature_change_reopens [R-22/E-3]`, `::test_watchdog_reopens_after_stall [R-21/E-5]`, `::test_open_failure_retries_5s [E-4]`, `::test_mic_denied_blocks_and_recovers [E-1]`, `::test_spool_oserror_drops_and_counts [E-14]`, `::test_pause_flushes_partial_segment` |
| T-13 | SingleInstanceLock + lock-first CLI ordering (lazy heavy imports) | AC-6; R-17; E-15 | Second `murmur run --headless` against a held lock exits 1 with "Murmur already running (PID: N)" in < 2 s; first instance unaffected | `tests/integration/test_single_instance.py::test_second_instance_exits_fast [AC-6]`, `test_lock.py::test_flock_stale_safe` |
| T-14 | CLI `cat today` / `copy today` (pbcopy) | AC-11, AC-12; R-23 | Both subcommands work against a tmp `MURMUR_HOME` + config; exit codes per contract | `tests/integration/test_cli.py::test_cat_today_stdout [AC-11]`, `::test_copy_today_pbpaste_roundtrip [AC-12]`, `test_cli_unit.py::test_cat_missing_file_exit_1` |
| T-15 | ModelManager: snapshot download w/ progress hook, explicit sha256 vs HF LFS metadata, verified-marker, corrupt-handling | R-24; E-18; underpins AC-28 (manual) | With fake HfApi/downloader: progress reported, mismatch deletes snapshot and raises ChecksumError, marker skips re-hash | `test_models.py::test_checksum_pass_writes_marker`, `::test_mismatch_deletes_and_raises [E-18]`, `::test_progress_reported`, `::test_marker_skips_rehash`; MV-28 |
| T-16 | Runtime wiring + `murmur run --headless` + `murmur bench` + signal handling + full no-hardware pipeline integration test | AC-5, AC-16 (startup honoring), R-10, R-15 (entry), R-16; harness for AC-20, AC-21 | FakeAudioSource→Segmenter→spool→worker(FakeTranscriber)→transcript end-to-end in-process; silent stream creates no file; paused state honored at startup; bench prints RTF and exits per threshold | `tests/integration/test_pipeline.py::test_speech_to_transcript_end_to_end [AC-4/AC-1 e2e]`, `::test_silent_day_no_file [AC-5]`, `::test_starts_paused_from_state [AC-16]`, `::test_backlog_drained_before_capture [R-16]`, `test_bench.py::test_bench_reports_rtf_with_fake`; MV-20, MV-21 |
| T-17 | Menubar app (rumps; first action: 5-min spike confirming rumps works on macOS 26) + OsascriptNotifier + permissions module | R-11, R-12, R-18, R-19; OQ-4; E-2 surface; underpins AC-23 (notify), AC-27 | Menu structure exact per R-12 with notes item disabled; timer pulls snapshot; notifier never raises/blocks; permission probes return correct enums (mocked AVFoundation/AX) | `test_menubar.py::test_menu_items_and_disabled_notes [R-12]`, `::test_icon_reflects_snapshot [R-11]`, `test_notify.py::test_osascript_cmd_and_never_raises`, `test_permissions.py::test_status_mapping`; MV-23, MV-27 |
| T-18 | HotkeyListener: spec parser, graceful Accessibility degradation, toggle wiring | R-13; E-2; underpins AC-23 | Parser exact; `start()` returns False without Accessibility and app continues with notice; callback → `Controller.toggle_pause` | `test_hotkey.py::test_spec_parsing`, `::test_untrusted_degrades_gracefully [E-2]`, `::test_callback_toggles`; MV-23 |
| T-19 | launchd: plist render/install/uninstall + AC-29 validation tests | AC-29; R-15 | Production render passes `plutil -lint`; inert variant (`/usr/bin/true`, RunAtLoad=false) survives `launchctl bootstrap`/`bootout` round-trip in test with guaranteed cleanup | `tests/integration/test_launchd.py::test_plist_plutil_lint [AC-29]`, `::test_launchctl_load_inert_roundtrip [AC-29]`, `test_launchd_unit.py::test_render_keys` |
| T-20 | `docs/manual-verification.md` (MV-20…MV-28 scripted procedures w/ unambiguous pass conditions, incl. one-time permission grants + Keychain setup) + README (install, config, launchd, bake-off decision rule) | AC-20…AC-28 (manual procedures); OQ-1 decision recording | Every [MANUAL] AC has a numbered step-by-step procedure matching the spec text; README commands all executed once successfully | Doc review at validation; commands smoke-run |

**AC coverage check:** All 30 ACs mapped. AUTO: AC-1…AC-19, AC-29, AC-30 → suite tests above; MANUAL: AC-20…AC-28 → harness tasks (T-10, T-12, T-15, T-16, T-17, T-18) + scripted procedures MV-20…MV-28 in T-20.

## Test Strategy

**Levels and how to run (defined by this plan; greenfield):**
- **Unit** (`tests/unit/`): pure logic with fakes from `tests/fakes.py` (FakeAudioSource, AmplitudeVAD, FakeTranscriber, RecordingNotifier, FixedClock, fake HfApi, fake subprocess runners). No hardware, no network, no model loads. The bulk of coverage.
- **Integration** (`tests/integration/`): real Silero VAD on `say`-generated fixtures (model ships in the wheel — no download), CLI subprocess tests under a tmp `$MURMUR_HOME`, pbcopy/pbpaste round-trip, plutil/launchctl inert plist, full in-process pipeline with fake source + fake transcriber. All runnable on this machine (dev machine = target machine), still no mic/GUI/model-download.
- **Hardware/manual** (`-m hardware` + `docs/manual-verification.md`): anything needing mic, mlx model weights, menubar visuals, or human judgment. Excluded from the default run via `addopts = -m "not hardware"`.

Commands: `uv run pytest` (default suite, coverage on: `--cov=src/murmur --cov-report=term-missing`) · `uv run ruff check . && uv run ruff format --check .` · `uv run mypy src` · `uv run pytest -m hardware` (opt-in). `scripts/` is excluded from the coverage gate (standalone manual-AC tooling; kept thin by reusing `murmur.transcribe`). Coverage bar: ≥ 80% on `src/murmur` (everything is new code).

| AC | Test name(s) | Level |
|----|--------------|-------|
| AC-1 | `test_worker.py::test_success_deletes_wav` | unit |
| AC-2 | `test_worker.py::test_keep_audio_retains_wav` | unit |
| AC-3 | `test_guard.py::test_drops_high_no_speech_prob`, `::test_drops_filler_phrase` | unit |
| AC-4 | `test_writer.py::test_line_format_local_time`; `test_pipeline.py::test_speech_to_transcript_end_to_end` | unit + integration |
| AC-5 | `test_writer.py::test_no_file_until_first_segment`; `test_pipeline.py::test_silent_day_no_file` | unit + integration |
| AC-6 | `test_single_instance.py::test_second_instance_exits_fast` | integration (subprocess) |
| AC-7 | `test_worker.py::test_fallback_off_leaves_wav_increments_error_no_api` | unit |
| AC-8 | `test_worker.py::test_third_failure_invokes_fallback` (+ `test_api.py::test_request_shape`) | unit |
| AC-9 | `test_retention.py::test_prunes_8d_keeps_7d` | unit |
| AC-10 | `test_retention.py::test_zero_keeps_forever` | unit |
| AC-11 | `test_cli.py::test_cat_today_stdout` | integration (subprocess) |
| AC-12 | `test_cli.py::test_copy_today_pbpaste_roundtrip` | integration (subprocess) |
| AC-13 | `test_segmenter.py::test_force_split_at_quietest_point_121s` | unit |
| AC-14 | `test_segmenter.py::test_1_6s_gap_two_segments` | unit |
| AC-15 | `test_segmenter.py::test_pre_post_roll_within_50ms` | unit |
| AC-16 | `test_state.py::test_rest_of_day_persists_before_midnight`; `test_pipeline.py::test_starts_paused_from_state` | unit + integration |
| AC-17 | `test_state.py::test_expires_after_midnight` | unit |
| AC-18 | `test_prompt.py::test_hints_verbatim`; `test_local.py::test_initial_prompt_passed` | unit |
| AC-19 | `test_engine.py::test_disk_low_pauses_then_resumes_with_notifications` | unit |
| AC-20 | MV-20 via `murmur bench` (de-risked by `test_bench.py::test_bench_reports_rtf_with_fake`) | manual |
| AC-21 | MV-21 (de-risked by `test_pipeline.py` e2e + engine gate tests) | manual |
| AC-22 | MV-22 (de-risked by `test_engine.py::test_device_signature_change_reopens`) | manual |
| AC-23 | MV-23 (de-risked by `test_hotkey.py::test_callback_toggles`, `test_controller.py::test_toggle_fires_notification`) | manual |
| AC-24 | MV-24 go/no-go (informed by bake-off output, T-10) | manual |
| AC-25 | MV-25 (de-risked by `test_engine.py::test_watchdog_reopens_after_stall`) | manual |
| AC-26 | MV-26 (de-risked by `test_bakeoff.py::test_four_sections_with_fakes`) | manual |
| AC-27 | MV-27 (de-risked by `test_permissions.py::test_status_mapping`, `test_engine.py::test_mic_denied_blocks_and_recovers`) | manual |
| AC-28 | MV-28 (de-risked by `test_models.py::*` checksum/progress suite) | manual |
| AC-29 | `test_launchd.py::test_plist_plutil_lint`, `::test_launchctl_load_inert_roundtrip` | integration |
| AC-30 | `test_config.py::test_values_applied` | unit |

## New Dependencies

Greenfield — everything is new; runtime picks are PRD §3-decided unless noted. Verified current as of 2026-06.

| Package | Why | License | Maintenance |
|---|---|---|---|
| `mlx-whisper` | PRD-decided local STT (Apple Silicon/Metal) | MIT | Active (Apple ml-explore) |
| `silero-vad` | PRD-decided VAD; pip wheel bundles model (no test-time downloads) | MIT | Active |
| `torch` | silero-vad backend per PRD ("torch, CPU"); pinned CPU usage, 1 thread | BSD-3 | Active |
| `sounddevice` | PRD-decided mic capture (PortAudio) | MIT | Active |
| `numpy` | Audio buffers (also transitive via mlx) | BSD-3 | Active |
| `rumps` | PRD-decided menubar | BSD-3 | **Stale (~2023)** — see Risk 1 |
| `pynput` | PRD-decided global hotkey | **LGPL-3.0** | Slow but alive — license fine for an unpublished personal tool; noting it explicitly |
| `openai` | API fallback (R-7) + bake-off engines (R-27) | Apache-2.0 | Active |
| `huggingface_hub` | Already transitive via mlx-whisper; made direct for `files_metadata` sha256s (R-24 checksum — hub does NOT reliably self-verify, confirmed open issues #2364/#3643) | Apache-2.0 | Active |
| `pyobjc-framework-AVFoundation` | Mic permission status/request (R-18) — pyobjc-core/Cocoa already required by rumps | MIT | Active |
| `pyobjc-framework-ApplicationServices` | `AXIsProcessTrusted` for Accessibility detection (R-19) | MIT | Active |
| dev: `pytest`, `pytest-cov`, `ruff`, `mypy` | Test/coverage/lint/typecheck stack for the quality bars | MIT/MIT/MIT/MIT | Active |

Deliberately **not** added: ffmpeg (not installed on target; avoided by passing numpy arrays — stdlib `wave` everywhere), `click`/`typer` (argparse suffices), `tomli` (3.11 has tomllib), `terminal-notifier` (osascript suffices), keyring lib (`security` CLI suffices).

## Risks

| Risk | Likelihood | Impact | Mitigation (or "accepted: <why>") |
|------|------------|--------|------------------------------------|
| 1. `rumps` (last release ~2023) breaks on macOS 26.4 | Medium | High (M2 UI blocked) | T-17 starts with a 5-minute spike (`rumps.App` visible in menubar). UI is one thin module behind Controller/StatusBoard; documented fallback is a direct pyobjc `NSStatusItem` shim with the same surface. If the spike fails → flag to human before proceeding, not after. |
| 2. GIL starvation: mlx-whisper decode loop on the worker thread delays VAD/segmentation during heavy transcription | Medium | Medium (capture gaps > R-1's 100 ms) | Callback is a C-thread doing only `put_nowait` into a 3.2 s buffer; Metal/torch kernels release the GIL; segmenter tolerates burst catch-up. Escape hatch designed in (ADR-1): worker → subprocess, spool is already the process-safe boundary. Verified empirically in MV-21. |
| 3. Device-change recovery relies on semi-private `sd._terminate()/_initialize()` (PortAudio caches device list at init) | Medium | Medium (AC-22/AC-25 fail without restart) | Isolated to `SounddeviceSource.open()`; engine logic fully unit-tested via fake; behavior pinned by MV-22/MV-25 on the real machine. Accepted residual: sounddevice private API may shift — single call site, trivial to patch. |
| 4. Throughput: large-v3 below 2× real-time on M4 Pro (would violate the AC-20 hard floor) | Low–Medium | High (model swap required) | `murmur bench` makes measurement a one-command check, early; bake-off (T-10, early per OQ-1) provides the sanctioned fallback `large-v3-turbo` (~4×) — a one-line config change, no code change. |
| 5. osascript notifications attributed to "Script Editor"/osascript and silently dropped until permission granted once | High (first run) | Low (notifications only; spec mandates silent degradation) | One-time grant step in MV runbook; Notifier never blocks/raises by contract. Accepted: correct app-attributed notifications require a signed .app bundle — out of v1 scope (PRD: py2app is "optional polish"). |
| 6. Silero VAD threshold 0.5 clips quiet speech onsets for this voice/mic | Medium | Medium (lost words → AC-24 risk) | 320 ms pre/post-roll absorbs onset lag; `vad_threshold` is user config; bake-off recording doubles as tuning data; MV-21/MV-24 are the empirical gates. |
| 7. HF checksum source: some repo files may lack LFS sha256 metadata (Xet storage) | Low | Low | Verify sha256 where metadata exists; size-check + logged warning where it doesn't; corrupt weights still caught by mlx load failure → E-18 path. Honest degradation, logged. |
| 8. Append-then-delete gives at-least-once transcription: crash in the ms between transcript append and WAV delete → duplicate block next launch | Low | Low (cosmetic duplicate line) | Accepted: exactly-once needs a journal; not worth it for a personal transcript. Ordering (append before delete) chosen so the failure mode is duplication, never loss. |
| 9. TCC permissions (mic/Accessibility) attach to the venv python binary; launchd-spawned process may need separate grants from the terminal-granted ones | Medium | Medium (hotkey/mic dead under launchd despite working in dev) | Permissions probes + ⚠ notices make it visible, never silent (R-18/R-19); MV runbook includes granting for the venv binary specifically; launchd plist uses the absolute venv `murmur` path so the grant target is stable. |

## Plan Notes for Reviewer

Things to settle or confirm at the checkpoint (plan currently follows each OQ's recommended default):

1. **OQ-2 correction (needs explicit sign-off):** the spec's recommended compression-ratio condition ("drop if < 2.4 or > 4.5") is inverted relative to standard Whisper guidance — normal English text has gzip compression ratios ~1.3–2.3, so "drop < 2.4" would discard nearly all legitimate speech. The plan implements the standard semantics: **drop when compression_ratio > 2.4** (repetition hallucination), plus `no_speech_prob > 0.6 ∧ avg_logprob < −1.0`, plus the filler-phrase list. AC-3's stated examples (`no_speech_prob > 0.6`, filler strings) are unaffected.
2. **OQ-1 timing reality:** the single checkpoint precedes implementation, so bake-off results cannot be reviewed at checkpoint (the script doesn't exist yet). Plan: default `local_model = "large-v3"`, build bakeoff early (T-10) so Jay can run it mid-build; the model decision is recorded at MV-24/MV-26 and is a one-line config flip. A different checkpoint answer (e.g., "default to turbo now") changes only the config default.
3. **OQ-3/OQ-5 defaults adopted:** state at `$MURMUR_HOME/state.json` (`~/Library/Application Support/Murmur/`), spool at `~/Transcripts/spool/` (configurable `storage.spool_dir`). Different answers change only `paths.py`/`config.py` defaults.
4. **OQ-4 adopted:** Unicode ●/⏸/⚠ as the menubar title. Choosing SF Symbols instead would add template-image assets to T-17 (small effort delta).
5. **AC-29 interpretation:** the auto test must not launch the real app, so it validates the production plist with `plutil -lint` and round-trips `launchctl bootstrap`/`bootout` on an inert variant (ProgramArguments=`/usr/bin/true`, RunAtLoad=false) rendered by the same code path. True load-with-app is exercised by `murmur install-agent` in the MV runbook.
6. **Scope additions (tooling, flagged not silent):** `murmur bench` exists solely to make AC-20's manual procedure a one-command check; `murmur install-agent`/`uninstall-agent` make R-15/AC-29 reproducible. Both are thin wrappers over planned components.
7. **Notifications via osascript, not `rumps.notification`:** rumps' notification API requires an Info.plist hack inside the venv and uses a deprecated macOS API; osascript needs zero deps and degrades exactly as the spec's notification constraint requires. Trade-off: notifications aren't attributed to a "Murmur" app identity (see Risk 5).
8. **launchd `KeepAlive=false`:** menu Quit must actually stop the app; crash-relaunch is sacrificed. If always-on matters more than Quit semantics, flip to `KeepAlive={SuccessfulExit:false}` — say so at checkpoint.
9. **API keys under launchd:** launchd agents don't inherit shell env, so R-25's Keychain leg is load-bearing, not optional — implemented via the `security` CLI (item `murmur-openai`), documented in README.
10. **Environment pins:** machine has Python 3.11.2 exactly (`requires-python = ">=3.11"`, `.python-version` 3.11) and **no ffmpeg** — the array-only audio path is a hard design rule, not a preference.
