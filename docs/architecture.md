# Architecture — Murmur

**Run ID:** murmur-2026-06-11

---

## Thread model (ADR-1)

Murmur is one process with five threads. Each thread has exclusive ownership of its hardware boundary.

```
MAIN THREAD (T0)
  rumps.App (menubar) — pulls StatusSnapshot via 1 s timer
  or: headless wait loop (murmur run --headless)
         |
         | menu actions, hotkey callbacks → Controller
         v
  Controller / StateStore (thread-safe, threading.Lock)
         ^
         | pause/resume state, status updates
         |
CAPTURE ENGINE THREAD (T2)           TRANSCRIPTION WORKER THREAD (T4)
  Owns: InputStream lifecycle          Owns: spool consumption,
  device poll (2 s),                   mlx-whisper,
  watchdog (3 s stall → reopen),       hallucination guard,
  disk gate (60 s),                    transcript writes,
  mic-permission gate (60 s),          WAV deletion, retries,
  Silero VAD, Segmenter, spool writes  API fallback
         ^                                    ^
         | frames_q (Queue, maxsize=100)       | work_q (Queue, maxsize=256)
         |                                    |
  PORTAUDIO CALLBACK (T1)              spool dir ~/Transcripts/spool/*.wav
  (owned by sounddevice)                       |
  → frames_q.put_nowait                        | success
  (drops silently on queue.Full)               v
                                     ~/Transcripts/YYYY-MM-DD.md
                                     (atomic rewrite + os.replace)

PYNPUT GLOBALHOTKEYS (T5)
  Own thread (pynput-managed)
  callback: controller.toggle_pause() only
```

**Ownership rules (load-bearing):**

- Only T0 touches rumps/AppKit objects. Status is read by T0 from a `StatusSnapshot` polled by a timer.
- Only the capture engine thread touches the `InputStream` object and the Silero VAD model.
- Only the worker thread writes transcript files and deletes spool WAVs.
- The PortAudio callback does exactly one thing: `frames_q.put_nowait`. No other instance state.
- `Controller`, `StateStore`, and `StatusBoard` are the only cross-thread mutable objects; each uses a single `threading.Lock`.
- `OsascriptNotifier` is safe to call from any thread (fire-and-forget daemon subprocess).

---

## Data flow: mic to Markdown

```
Microphone (16 kHz mono, PortAudio)
  → 512-sample frames → frames_q
  → Capture engine: Silero VAD per frame
      silence → ring buffer (pre-roll); never reaches disk (R-2)
      speech onset → segment starts (with ≤ 320 ms pre-roll from ring buffer)
      1.5 s trailing silence → segment closed (+ 320 ms post-roll)
      OR 120 s max duration → force-split at quietest frame (min RMS)
  → write_segment_wav: float32 → 16-bit PCM, tmp+fsync+os.replace
  → ~/Transcripts/spool/YYYYMMDD-HHMMSS-NNN.wav
  → work_q.put_nowait (WAV path); or logged warning + disk recovery if queue full

  → Worker: load_wav (stdlib wave, no ffmpeg)
  → mlx_whisper.transcribe(audio_array, language="en", initial_prompt=...)
  → hallucination guard: drop segments where
      (no_speech_prob > 0.6 AND avg_logprob < -1.0)
      OR compression_ratio > 2.4
      OR normalized text in FILLER_PHRASES
      OR empty
  → TranscriptWriter.append(start_at, text)
      reads existing file → appends line → tmp+fsync+os.replace → ~/Transcripts/YYYY-MM-DD.md
  → delete WAV (unless keep_audio=true)
```

---

## Durability story (ADR-2)

**Spool as durable queue.** WAV files are the unit of work between capture and transcription. They are written atomically (tmp + fsync + os.replace — no partial WAV is ever visible in the spool directory). The spool directory survives crashes; at next launch the startup scanner processes all existing spool WAVs in chronological order (FIFO by filename) before opening the microphone stream.

**At-least-once transcript semantics.** The ordering is: write transcript line → delete WAV. A crash in the millisecond between those two operations results in the segment being transcribed again on the next launch (duplicate line in the Markdown file). This is the accepted failure mode — the alternative (delete-then-write) would cause data loss. For a personal transcript, a rare duplicate line is preferable to a lost utterance.

**Transcript file durability.** `TranscriptWriter.append` always: reads the full existing file, appends the new line, writes the result to a temporary file in the same directory, calls `fsync`, then calls `os.replace`. The Markdown file is never observable in a partially-written state (R-9).

**In-process work queue (bounded, non-durable).** The `work_q` between the capture engine and worker is bounded at 256 entries (~8+ minutes of segments at normal rate). It is an optimisation layer only — not the durable store. If it fills (worker in backoff, or slow transcription), the capture engine logs a warning and drops the enqueue; the WAV remains on disk and is recovered at next launch via the startup scan. The disk-low gate (pause at < 2 GiB free) independently limits spool growth.

---

## State

**`state.json`** (`~/Library/Application Support/Murmur/state.json`)

Persists pause state across restarts. Written atomically (tmp+fsync+os.replace). Corrupt or missing file → not paused (recording). Schema:

```json
{
  "version": 1,
  "pause": {
    "mode": "rest_of_day",
    "until": "2026-06-12T00:00:00+01:00"
  }
}
```

`mode` values: `"manual"` (until explicit resume), `"timed"` (until timestamp), `"rest_of_day"` (until next local midnight), or `null` (recording).

**`murmur.lock`** (`~/Library/Application Support/Murmur/murmur.lock`)

`fcntl.flock(LOCK_EX|LOCK_NB)` held for the lifetime of the process. Contains the PID as ASCII text. Stale-safe: the OS releases the flock when the file descriptor closes on process death. A second instance reads the PID from the lock file and exits with "Murmur already running (PID: N)".

---

## Device change and sleep/wake

One mechanism covers both. The capture engine runs two concurrent checks:

1. **Device poll (2 s):** compares `sd.query_devices(kind="input").name` against the last known signature. On change: flush partial segment → close stream → `sd._terminate()` / `sd._initialize()` (required because PortAudio snapshots the device list at init) → reopen on current default device.

2. **Watchdog (3 s stall):** if the stream is open, not paused, and no frame has arrived in 3 s, the same flush→close→reinit→reopen sequence runs. This handles sleep/wake, where the audio subsystem may need re-initialization without a device-change event.

Open failure on reopen → icon ⚠ "No input device", retry every 5 s.

---

## Model weights and checksum

`ModelManager` downloads weights via `huggingface_hub.snapshot_download`. After download it verifies sha256 of every LFS file against the `lfs.sha256` field from `HfApi.model_info(files_metadata=True)`. Mismatch: delete snapshot dir, raise `ChecksumError`. Files without LFS metadata get a size check with a logged warning (honest degradation — see Risk 7 in plan).

A marker file (`~/.cache/huggingface/murmur-model-verified-<model>.json`) is written after successful verification. Its presence on subsequent launches skips re-hashing. Delete the marker to force re-verification.

Model repos:

| Config key | Hugging Face repo |
|------------|-------------------|
| `large-v3` | `mlx-community/whisper-large-v3-mlx` |
| `large-v3-turbo` | `mlx-community/whisper-large-v3-turbo` |

---

## Hallucination guard constants

Defined in `src/murmur/transcribe/guard.py` (named constants, not user config in v1):

| Constant | Value | Meaning |
|----------|-------|---------|
| `NO_SPEECH_PROB_MAX` | 0.6 | Combined with `avg_logprob < -1.0` to drop silence |
| `AVG_LOGPROB_MIN` | -1.0 | See above |
| `COMPRESSION_RATIO_MAX` | 2.4 | Drop repetition hallucinations (gzip ratio > 2.4) |
| `FILLER_PHRASES` | frozenset | Normalized strings like "thank you for watching", "you", etc. |

---

## Audio constants

Defined in `src/murmur/audio/__init__.py`:

| Constant | Value |
|----------|-------|
| `SAMPLE_RATE` | 16 000 Hz |
| `FRAME_SAMPLES` | 512 (Silero v5+ requirement; 32 ms per frame) |
| `PRE_ROLL_S` / `POST_ROLL_S` | 0.3 s (10 frames = 320 ms each) |
| `WATCHDOG_STALL_S` | 3.0 s |
| `DEVICE_POLL_S` | 2.0 s |
| `DISK_MIN_FREE_BYTES` | 2 GiB |
| `DISK_POLL_S` | 60.0 s |
| `PERMISSION_RECHECK_S` | 60.0 s |
| `MAX_LOCAL_FAILURES` | 3 consecutive failures before API fallback |

---

## Startup sequence

1. Acquire fcntl lock (before any heavy imports — enforces ≤ 2 s exit for a second instance)
2. Load config + resolve paths
3. Prune retention (delete transcript files older than `retention_days`)
4. Start `TranscribeWorker` — scans spool backlog in FIFO order, sets `backlog_drained` event when done
5. Start `CaptureEngine` — blocks on `backlog_drained` before opening the mic stream (ensures prior-run WAVs are processed first)
6. Start `HotkeyListener` — if Accessibility not granted, `start()` returns `False`; app continues with a menubar notice
7. Main thread: run `rumps.App` (menubar mode) or `runtime.wait()` (headless mode)

---

## Notifications

All notifications use `osascript display notification`. This works without a signed `.app` bundle but has two consequences:

- Notifications appear attributed to "Script Editor" or similar until macOS notification permission is granted once
- After permission is granted, subsequent notifications display correctly but may still show the osascript attribution rather than "Murmur" — this is a v1 accepted limitation (see plan Risk 5; correct attribution requires a py2app-built .app bundle)

`OsascriptNotifier.notify()` is fire-and-forget (daemon thread subprocess), never raises, and never blocks the caller.
