# Changelog

All notable changes to this project will be documented in this file.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

---

## [Unreleased]

### Changed

- **Server transcription now runs in a recyclable subprocess** (`SubprocessTranscriber`) instead of an in-process `MlxWhisperTranscriber`. The MLX model's Metal buffer pool grew to the peak working set and was never released, pinning ~4.5 GB in the always-on launchd server for its whole lifetime. The model now lives in a child process torn down after 120 s idle, so memory is reclaimed between requests; the server process stays lean. Inside the child: `set_cache_limit(0)` + `clear_cache()` after each segment, and `set_memory_limit(~70% RAM)` as a hard ceiling. The transcriber is closed on server shutdown so no child outlives a launchd restart.

## [1.0.0] — 2026-06-14

### Added

- **Murmur Server** (`src/murmur/server/`) — FastAPI HTTP service wrapping the existing `MlxWhisperTranscriber` for use by Murmur Mobile (Android).
  - `GET /health` — bearer-token-gated liveness and readiness check. Returns `{"status","model_loaded","model","version"}`. Never acquires the transcription lock; responds < 500 ms regardless of in-progress transcription (AC-1 through AC-3, AC-13).
  - `POST /transcribe` — multipart upload (m4a/aac/wav/mp3/ogg/opus, max 25 MB). Decodes audio via `ffmpeg` subprocess to float32 mono 16 kHz PCM, transcribes with `MlxWhisperTranscriber` (large-v3), applies `build_initial_prompt()` vocab hints, runs `clean_result()` hallucination guard, returns JSON `{text, segments, language, duration_sec, model, engine}` (AC-4 through AC-18).
  - Single-flight `asyncio.Lock` with atomic acquire — concurrent `POST /transcribe` receives immediate 503 `model_busy`; no waiter queue (AC-14, ADR-2, R3-1 fix).
  - 10-code error registry: `unauthorized`, `model_busy`, `model_not_loaded`, `missing_file`, `empty_file`, `file_too_large`, `unsupported_media_type`, `audio_decode_failed`, `transcription_failed`, `no_speech_detected`.
  - Bearer token read from `MURMUR_SERVER_TOKEN` env or macOS Keychain (`murmur-server-token`); constant-time `hmac.compare_digest` comparison; token never logged (AC-15, AC-17, R-7).
  - `server.toml` config: `[server]`, `[stt]`, `[clean]` (parsed and ignored); missing/malformed → defaults + warning, no crash (AC-16).
  - launchd user agent (`com.jay.murmur-server.plist`): `KeepAlive=true`, `RunAtLoad=true`, `caffeinate -i` power assertion, single-instance PID-file belt at `/tmp/murmur-server.pid` (AC-19, R-16, R-17).
  - Server binds `127.0.0.1` only; exposed via `tailscale serve --bg https / http://127.0.0.1:8080` (R-8, R-9).
  - Structured logs to `~/Library/Logs/murmur-server.log`; audio bytes and transcript text never logged (R-10, R-18).
  - 358 tests (including 3 added in post-escalation R3-1 fix); 86.76% coverage on new server code; ruff and mypy clean.

### Changed

- Bumped package version `0.1.0` → `1.0.0` to reflect the first production-ready release shipping the server component. The `/health` `version` field reports `"1.0.0"`.

---

## [0.1.0] — 2026-06-12

Initial release.

### Added

- **Always-on microphone capture** at 16 kHz mono via PortAudio (sounddevice). Continuous capture with no gaps > 100 ms during normal operation.
- **Silero VAD** gates all disk writes — silence is discarded in memory and never spooled.
- **Speech segmenter** closes segments at 1.5 s trailing silence or 120 s max duration, with 0.3 s pre/post-roll padding. Force-splits at the quietest audio point (min RMS) when max duration is reached.
- **Durable spool** at `~/Transcripts/spool/` — WAV filenames encode segment start time; crash recovery at next launch processes prior-run WAVs in FIFO order before opening the mic.
- **Local transcription** via mlx-whisper large-v3 (configurable to large-v3-turbo) on Apple Silicon / Metal. Audio is always passed as a numpy array — no ffmpeg dependency.
- **Hallucination guard** drops segments with `no_speech_prob > 0.6 AND avg_logprob < -1.0`, `compression_ratio > 2.4`, known filler phrases, or empty text.
- **Vocabulary hints** passed to Whisper as `initial_prompt` — configurable list of domain terms.
- **Atomic transcript writes** — daily Markdown files at `~/Transcripts/YYYY-MM-DD.md` in format `**[HH:MM]** <text>`. Files are written tmp+fsync+os.replace; never in a partial state.
- **Retention pruning** — transcript files older than `retention_days` (default 365; `0` = forever) are deleted at startup.
- **API fallback** — on 3 consecutive local transcription failures, optionally retries via OpenAI API (`gpt-4o-transcribe` by default). Off by default. API key sourced from env or macOS Keychain, never config.
- **macOS menubar** (rumps) with icons `●` / `⏸` / `⚠`. Dropdown: Pause/Resume, timed pauses (30 min, 1 h, rest of day), open transcript, open folder, Quit.
- **Global hotkey** `⌃⌥⌘M` (pynput) — toggles pause/resume from any application; fires osascript notification confirming state. Requires Accessibility permission; degrades gracefully if not granted.
- **Pause persistence** — "rest of day" pause persists to `state.json` and survives restart until midnight.
- **Disk guard** — capture pauses automatically when free disk space falls below 2 GiB; resumes when space recovers. Icon shows `⚠` during pause.
- **Device change recovery** — mic stream is reopened on the new default input device within ~3 s of an AirPods disconnect or any plug/unplug event.
- **Sleep/wake recovery** — capture resumes on wake via a 3 s watchdog stall detector; no manual intervention required.
- **Single-instance enforcement** — second `murmur run` detects existing lock, prints "Murmur already running (PID: N)" to stderr, exits 1 within 2 s.
- **Microphone permission handling** — detects macOS TCC denial, shows ⚠ with actionable instructions, rechecks every 60 s and recovers automatically.
- **Model weight download + checksum** — `ModelManager` downloads weights via huggingface_hub, verifies sha256 against HF LFS metadata, writes a verified-marker for fast subsequent starts.
- **CLI** — `murmur run [--headless]`, `murmur cat today`, `murmur copy today`, `murmur bench FILE.WAV`, `murmur install-agent`, `murmur uninstall-agent`.
- **launchd user agent** — `murmur install-agent` writes and bootstraps `~/Library/LaunchAgents/com.jay.murmur.plist`; `KeepAlive = false` (Quit sticks).
- **Bake-off script** (`scripts/bakeoff.py`) — transcribes the same audio through mlx large-v3, mlx large-v3-turbo, gpt-4o-mini-transcribe, and gpt-4o-transcribe; outputs one Markdown file for side-by-side accuracy comparison.
- **`murmur bench`** — one-command throughput check against a WAV file; exits 0 if RTF ≥ 2.0×.
- **`$MURMUR_HOME` env var** — relocates the app support directory (default `~/Library/Application Support/Murmur`); used for test isolation.
- **Full test suite** — 233 unit and integration tests, 84.72% coverage on `src/murmur`. All hardware/model tests gated behind `-m hardware` marker.

### Configuration

All config in a single TOML file. See `config.example.toml` and `README.md` for the full reference.

### Known limitations

- WAV files with non-conforming filenames strand in spool permanently (manual removal required).
- WAVs dropped from the in-process queue during the current run are recovered only at next launch.
- Notifications are attributed to "Script Editor" (osascript) rather than "Murmur" until notification permission is granted once.
- Model checksum re-verification trusts the marker file on subsequent launches.
- `launchctl bootstrap` integration test is xfail outside an Aqua GUI session (platform constraint; plist syntax passes `plutil -lint`).

[0.1.0]: https://github.com/jay/murmur/releases/tag/v0.1.0
