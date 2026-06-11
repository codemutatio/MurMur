# Spec: Murmur — Personal Always-On Voice Transcription for macOS

**Run ID:** murmur-2026-06-11 · **Source:** runs/murmur-2026-06-11/prd.md · **Date:** 2026-06-11

---

## Problem

Jay speaks English with a Nigerian accent. Every commercial dictation and transcription tool he has purchased mis-transcribes him at a rate that makes the output unusable. He needs a persistent, local, zero-cost transcription layer that captures what he actually says throughout his workday and makes it available for later use in LLM workflows. No existing off-the-shelf tool meets this need without recurring cost, cloud dependency, or accuracy failure on his accent.

## Goal

After this ships, Jay has a background macOS process that starts at login, listens to his microphone all day, transcribes his speech accurately using Whisper large-v3 on his M4 Pro, and appends every utterance — timestamped — to a daily Markdown file he can paste into Claude or read back. The process costs nothing to run, stores nothing in the cloud, and is togglable in under one second.

---

## Requirements

| ID | Requirement |
|----|-------------|
| R-1 | The system captures audio from the macOS default input device at 16 kHz mono, continuously, without gaps longer than 100 ms during normal operation. |
| R-2 | Silero VAD gates all writes to disk; silence is discarded in memory and never written to the spool. |
| R-3 | Speech segments are closed at 1.5 s trailing silence or 120 s max duration, whichever comes first, with 0.3 s pre-roll and post-roll padding applied to each segment boundary. |
| R-4 | The transcription worker processes spool segments in FIFO order using mlx-whisper with `language="en"` and a user-configured `vocab_hints` list supplied as Whisper's `initial_prompt`. |
| R-5 | A hallucination guard inspects each transcription result and drops segments whose `no_speech_prob` or compression ratio matches known Whisper artifact patterns (repeated phrases, known filler strings). |
| R-6 | Successfully transcribed WAV files are deleted from spool by default (`keep_audio = false`). |
| R-7 | On repeated local transcription failure, if `api_fallback = true` and `OPENAI_API_KEY` is set, the segment is retried via the configured OpenAI API model; otherwise the WAV remains in spool and an error count is surfaced in the menubar. |
| R-8 | Each transcribed segment is appended to `~/Transcripts/YYYY-MM-DD.md` in the format `**[HH:MM]** <text>`, using the segment start time in local time. The file is created on first segment of the day and is never created for a silent day. |
| R-9 | All file appends are crash-safe: written to a temporary location and flushed before being renamed/appended to the target file. |
| R-10 | The transcription worker sustains ≥2× real-time throughput on the target machine (Apple M4 Pro) so the spool drains within the same calendar day it was filled. |
| R-11 | A menubar icon reflects current state: recording (●), paused (⏸), or error (⚠) with queue depth shown in the dropdown. |
| R-12 | The menubar dropdown exposes: Pause/Resume; Pause for 30 min; Pause for 1 h; Pause for rest of day; Open today's transcript; Open transcripts folder; Quit. ("Generate notes now" is present but disabled in v1; it is a v2 item.) |
| R-13 | A global hotkey (default `⌃⌥⌘M`) toggles pause/resume from any application context and fires a macOS notification confirming the new state. |
| R-14 | Pause state persists across restarts: if paused "for rest of day", a relaunch before midnight does not resume capture. |
| R-15 | The app launches at login via a launchd user agent (`~/Library/LaunchAgents/com.jay.murmur.plist`) and starts automatically. |
| R-16 | On launch, any WAV files already in spool from a prior run are transcribed before fresh capture begins. |
| R-17 | The app refuses to start a second instance: it detects an existing PID/lockfile and exits cleanly. |
| R-18 | The app detects missing Microphone permission and displays clear, actionable instructions rather than failing silently. |
| R-19 | The app detects missing Accessibility permission (required for global hotkey) and displays clear, actionable instructions. |
| R-20 | If free disk space falls below 2 GB, capture pauses and a notification is fired; capture resumes automatically when space recovers above threshold. |
| R-21 | On wake from sleep, audio capture resumes on the current default input device automatically, without user intervention. |
| R-22 | On default input device change (AirPods connect/disconnect, any plug/unplug event), the audio stream is closed and reopened on the new default device without crashing or requiring restart. |
| R-23 | A `murmur cat today` CLI command prints the current day's transcript to stdout. A `murmur copy today` command copies the current day's transcript to the macOS clipboard. |
| R-24 | If model weights for the configured local model are absent at first run, they are downloaded with progress surfaced in the menubar; the checksum of downloaded weights is verified before use. |
| R-25 | All configuration is read from a single `config.toml` file in the app directory. API keys are sourced from environment variables or macOS Keychain — never from config files or source code. |
| R-26 | A `retention_days` config value (default 365) prunes transcript files older than the configured age on launch; `0` means keep forever. |
| R-27 | A standalone `bakeoff.py` script records 10 minutes of audio and transcribes the same recording through all four configured engines, outputting results side-by-side in a single Markdown file for manual accuracy evaluation. |

---

## Acceptance Criteria

> Key: **[AUTO]** = automatable in the test suite; **[MANUAL]** = requires execution on the target machine by a human following the stated procedure. All manual procedures define an unambiguous pass condition.

| ID | Criterion |
|----|-----------|
| AC-1 | **[AUTO]** Given `keep_audio = false` (default), when a WAV segment is successfully transcribed, then no WAV file for that segment remains in the spool directory. |
| AC-2 | **[AUTO]** Given `keep_audio = true`, when a WAV segment is successfully transcribed, then the WAV file for that segment remains in the spool directory. |
| AC-3 | **[AUTO]** Given a segment whose Whisper output matches a known hallucination pattern (e.g., `no_speech_prob > 0.6`, or output is a known filler string such as "Thank you for watching"), when the hallucination guard runs, then the segment text is not appended to the transcript file. |
| AC-4 | **[AUTO]** Given a transcript line is appended, when the line is read back from the file, then it matches the format `**[HH:MM]** <text>` where HH:MM is the local-time hour and minute of the segment start. |
| AC-5 | **[AUTO]** Given a day with no speech detected, then `~/Transcripts/YYYY-MM-DD.md` for that date is not created. |
| AC-6 | **[AUTO]** Given the app is already running (lockfile/PID present), when a second instance is launched, then the second instance exits within 2 s and logs a human-readable error; the first instance continues running unaffected. |
| AC-7 | **[AUTO]** Given `api_fallback = false` (default) and a local transcription error on a segment, when the worker encounters the error, then the WAV is left in spool and the menubar error count increments by 1; no outbound network call is made. |
| AC-8 | **[AUTO]** Given `api_fallback = true` and `OPENAI_API_KEY` set, and a local transcription failure on a segment, when the worker encounters the error, then the segment is submitted to the configured OpenAI API endpoint (verifiable via mock/intercept in test). |
| AC-9 | **[AUTO]** Given `retention_days = 7`, when the app launches and `~/Transcripts/` contains a file dated 8 or more days before today, then that file is deleted; files dated 7 or fewer days before today are not deleted. |
| AC-10 | **[AUTO]** Given `retention_days = 0`, when the app launches, then no transcript files are deleted regardless of age. |
| AC-11 | **[AUTO]** Given a transcript file exists for today, when `murmur cat today` is run, then the full contents of that file are written to stdout and the process exits 0. |
| AC-12 | **[AUTO]** Given a transcript file exists for today, when `murmur copy today` is run, then the macOS clipboard contains the full contents of that file and the process exits 0. |
| AC-13 | **[AUTO]** Given an audio segment of exactly 121 s of continuous speech (no silence), when the VAD/segmenter processes it, then it produces at least two segments, each ≤ 120 s, with the split occurring at the quietest detected point within the segment. |
| AC-14 | **[AUTO]** Given an audio stream with a 1.6 s silence gap between two speech bursts, when the segmenter processes it, then each burst produces a separate WAV file in spool. |
| AC-15 | **[AUTO]** Given an audio stream, when a speech segment WAV is written, then it includes 0.3 s of audio before the first detected speech onset and 0.3 s after the last detected speech offset (to within ±50 ms tolerance given 16 kHz frame boundaries). |
| AC-16 | **[AUTO]** Given a paused state of type "rest of day", when the app process is killed and relaunched before midnight of the same calendar day, then the app starts in paused state and does not begin capturing audio. |
| AC-17 | **[AUTO]** Given a paused state of type "rest of day", when the app process is killed and relaunched after midnight (next calendar day), then the app starts in recording state. |
| AC-18 | **[AUTO]** Given `vocab_hints = ["Rivy", "BigQuery"]`, when a segment is submitted for transcription, then the `initial_prompt` passed to mlx-whisper contains both strings (verifiable via mock or call inspection). |
| AC-19 | **[AUTO]** Given disk free space drops below 2 GB (simulated in test), when the capture loop checks disk space, then capture pauses, the menubar state changes to ⚠, and a notification payload is emitted; capture resumes when free space is restored above 2 GB. |
| AC-20 | **[MANUAL — Throughput]** Procedure: record a continuous 60-minute audio file of normal speech (or use a representative sample from the bake-off recording, looped to 60 min). Feed the file to the transcription worker in isolation (bypass live mic). Measure wall-clock time from first segment enqueued to spool-empty. Pass condition: elapsed time ≤ 30 minutes (i.e., ≥2× real-time). |
| AC-21 | **[MANUAL — 8-hour unattended run]** Procedure: start `murmur run` (M1 CLI) at the beginning of a normal workday on the target MacBook Pro. Speak naturally throughout the day. At end of day, inspect the system. Pass conditions (all must hold): (a) the process is still running; (b) the spool directory contains zero WAV files; (c) Activity Monitor shows Murmur CPU usage below 5% during periods of confirmed silence (as evidenced by no speech in those time windows); (d) the MacBook fan did not audibly spin up during silence periods (human judgment). |
| AC-22 | **[MANUAL — AirPods recovery]** Procedure: with Murmur running and recording, disconnect AirPods (or any connected headset) so the default input device changes. Wait 10 s. Speak a test phrase. Pass condition: the test phrase appears in today's transcript within 30 s of speaking, and the app has not crashed or entered an error state. |
| AC-23 | **[MANUAL — Hotkey latency]** Procedure: with Murmur recording, press `⌃⌥⌘M`. Pass condition: a macOS notification confirming "Paused" appears within 1 s of keypress, and audio capture stops (confirmed by speaking and verifying nothing is appended to the transcript for at least 10 s). Press the hotkey again; pass condition: a notification confirming "Recording" appears within 1 s, and speech is again transcribed. |
| AC-24 | **[MANUAL — Accent accuracy]** Procedure: Jay reads the day's transcript generated during AC-21 and rates it against his reference experience with ChatGPT voice input. Pass condition: Jay judges the Murmur transcript to be of equal or better accuracy than a comparable ChatGPT voice input session for the same vocabulary and accent. This is a go/no-go human gate; if it fails, the `local_model` config choice must be revisited using bake-off data. |
| AC-25 | **[MANUAL — Sleep/wake recovery]** Procedure: with Murmur running and recording, close the MacBook lid and wait at least 60 s, then open the lid. Speak a test phrase. Pass condition: the test phrase appears in today's transcript within 60 s of speaking, with no crash or manual restart required. |
| AC-26 | **[MANUAL — Bake-off output]** Procedure: run `bakeoff.py` after recording 10 minutes of Jay's normal work speech. Pass condition: the script completes without error and produces a single Markdown file containing four labeled transcript sections (mlx large-v3, mlx large-v3-turbo, gpt-4o-mini-transcribe, gpt-4o-transcribe) for side-by-side comparison. |
| AC-27 | **[MANUAL — Missing mic permission]** Procedure: revoke Microphone permission for Murmur in System Settings, then launch Murmur. Pass condition: the menubar shows ⚠, a notification appears with text that includes actionable instructions to grant Microphone permission in System Settings, and no crash occurs. |
| AC-28 | **[MANUAL — Model weight download]** Procedure: delete cached model weights for the configured local model, then launch Murmur for the first time (or in a clean state). Pass condition: the menubar displays a download-progress indicator, download completes without error, checksum verification passes (logged), and transcription begins normally. |
| AC-29 | **[AUTO]** Given the app is configured for launchd autostart and the plist is installed, when the plist is validated with `launchctl load` in a test environment, then no error is returned and the plist is syntactically valid. |
| AC-30 | **[AUTO]** Given a `config.toml` with `silence_split_s = 1.5` and `max_segment_s = 120`, when the config is loaded, then the runtime values for silence threshold and max segment duration exactly match the configured values. |

---

## Non-Goals

- **No system audio or meeting capture.** Murmur captures only the microphone input device. Audio from other participants in calls, from speakers, or from any system audio source is explicitly out of scope. This is flagged as v1.5.
- **No real-time on-screen transcription display.** There is no live captions window, floating overlay, or inline text insertion. Transcripts exist only in the daily Markdown file.
- **No speaker diarization.** All speech in a segment is attributed to a single unnamed speaker. Multiple-speaker detection and labeling is not in scope.
- **No multi-user support, cloud sync, or mobile app.** This is a single-user, local-only tool. There is no account system, no remote storage, and no companion mobile application.
- **No fine-tuning or adaptation of the Whisper model.** The model is used as-is. No training, fine-tuning, LoRA, or prompt-engineering beyond `initial_prompt` vocabulary hints is in scope.
- **No v2 notes generation (FR6 / M3).** The Claude Haiku nightly summarization feature is explicitly deferred to v2. The menubar item for it is present but disabled in v1.
- **No search across transcripts.** `murmur grep` or any cross-file search capability is backlog, not v1.
- **No direct Claude Desktop / MCP integration.** The workflow is copy-paste from the Markdown file; no plugin, integration, or API connection to Claude Desktop is in scope.

---

## Edge Cases

| # | Case | Expected behavior |
|---|------|-------------------|
| E-1 | Microphone permission revoked while the app is running | Capture thread catches the permission error, sets state to ⚠, fires a notification with fix instructions, stops attempting to open the stream; resumes automatically if permission is restored on next stream-open attempt (periodic retry). |
| E-2 | Accessibility permission absent at launch | Global hotkey registration fails gracefully; app starts and functions normally except for the hotkey; menubar shows a persistent notice that Accessibility permission is needed for hotkey. |
| E-3 | Default input device changes mid-stream (AirPods disconnect, headset unplug) | Current stream is closed without error propagation; a new stream is opened on the updated default device within 2 s; any partial segment already in the ring buffer is flushed and queued before the stream switch. |
| E-4 | Default input device changes to a device with no microphone (e.g., display audio with no input) | Stream open fails; app retries on next device-change event or after a 5 s interval; menubar shows ⚠ with a "No input device" notice. |
| E-5 | Machine wakes from sleep | Audio subsystem may need re-initialization; capture loop detects stream error, closes and reopens the stream on the current default device; no gap longer than 5 s in capture after wake (speech spoken more than 5 s after wake is captured). |
| E-6 | Spool contains WAVs from a prior crash at launch | Before opening the mic stream, the worker transcribes all existing spool WAVs in chronological order; only then does live capture begin. |
| E-7 | Whisper returns empty string for a segment (e.g., very short utterance, cough) | Empty transcription is treated as no-speech; the WAV is deleted (if `keep_audio = false`) and nothing is appended to the transcript. |
| E-8 | A transcription result is entirely a hallucination-guard match | Segment is silently dropped (not appended); WAV deleted per `keep_audio` setting; no error count increment (this is normal operation, not an error). |
| E-9 | Local transcription fails repeatedly for a single segment (define "repeated" as ≥ 3 consecutive failures) | If `api_fallback = false`: WAV left in spool, error count in menubar incremented, segment skipped so the worker can continue with subsequent segments. If `api_fallback = true`: segment retried via API on the third local failure. |
| E-10 | OpenAI API call fails (network error, rate limit, auth failure) | Failure is logged; WAV left in spool; error count incremented; app continues; no retry storm (back-off applied). |
| E-11 | `~/Transcripts/` directory does not exist | Created automatically on first run, including any necessary intermediate directories. |
| E-12 | `~/Transcripts/YYYY-MM-DD.md` is deleted mid-day while the app is running | App recreates the file and appends subsequent segments normally; previously transcribed segments from the same day that were already deleted from spool are not recoverable (expected data loss; not an error state). |
| E-13 | Disk free space falls below 2 GB | Capture pauses immediately; notification fired; a disk-check loop polls every 60 s and resumes capture when free space exceeds 2 GB. |
| E-14 | Disk completely full (0 B free) | WAV write to spool fails; segment discarded with error logged; capture continues; error count incremented; notification fired. |
| E-15 | Second app instance launched while first is running | Second instance detects lockfile/PID, logs "Murmur already running (PID: N)", exits with code 1 within 2 s; first instance is unaffected. |
| E-16 | `config.toml` is missing or unparseable | App starts with all defaults hardcoded; logs a clear warning that config was not loaded and specifies the expected path; does not crash. |
| E-17 | `config.toml` contains an unrecognized key | Unknown key is ignored with a logged warning; all recognized keys are applied normally. |
| E-18 | `local_model` weight files fail checksum verification after download | Download is discarded; error surfaced in menubar; app does not attempt to use corrupt weights; user is prompted to retry. |
| E-19 | Segment exceeds 120 s (force-split condition) | Split is performed at the quietest detected audio point within the segment; both sub-segments are queued as separate WAVs; no audio data is discarded. |
| E-20 | Speech onset detected but segment is < 0.1 s of actual speech (e.g., a single click or pop) | Segment is processed; if `no_speech_prob` threshold classifies it as non-speech, hallucination guard drops it; otherwise it is transcribed normally. |
| E-21 | `OPENAI_API_KEY` is not set but `api_fallback = true` | At startup, log a warning that API fallback is enabled but no key is available; treat `api_fallback` as effectively false for that session; surface a notice in the menubar. |
| E-22 | `retention_days` pruning deletes a file that is currently open by another process | Deletion attempt is caught; error is logged; file is skipped; no crash. |
| E-23 | `bakeoff.py` run without a previously recorded reference file | Script records fresh audio for the configured duration (10 min) before beginning transcription; user is prompted before recording starts. |
| E-24 | App launched on a machine that is not Apple Silicon | mlx-whisper is unavailable or non-functional on non-Apple-Silicon hardware; app detects the condition at startup and logs a clear, actionable error; it does not silently fall back to a CPU path that may exhaust the machine. |

---

## Constraints

- **Target hardware:** Apple MacBook Pro, Apple M4 Pro chip, macOS (current version as of build date). mlx-whisper depends on the Apple Neural Engine / Metal Performance Shaders; the app is not required to work on Intel Macs or non-Apple hardware.
- **Python version:** 3.11 or higher. No compatibility requirement for older Python versions.
- **Throughput floor:** transcription worker must sustain ≥2× real-time on the target machine (AC-20). This is not advisory; it is a hard requirement. If the chosen model does not meet it, the model choice must change (bake-off informs this).
- **Zero cloud cost by default:** all data processing happens on-device when using default config. API fallback is off by default; notes generation is off by default. The default configuration must never make an outbound network call during normal operation.
- **No persistent raw audio:** default config deletes WAV files after successful transcription. This is a privacy constraint, not a preference.
- **API keys out of config:** API keys must be sourced from environment variables or macOS Keychain. They must never appear in `config.toml`, in source code, or in any file committed to the repository.
- **Spool directory data integrity:** the spool is not a permanent store. WAV files in spool may be deleted on successful transcription. The system must never leave a transcript in a partially-written state; the daily Markdown file is always in a consistent, readable state.
- **macOS notification permission:** the app uses macOS UserNotifications for state-change alerts. If notification permission is denied, the app functions normally but silently (no crash, no blocking wait for permission).

---

## Assumptions

- The `~/Transcripts/` directory hierarchy is created by the app if absent; no pre-existing directory structure is assumed.
- "Repeated local failure" for the API fallback trigger is defined as 3 consecutive failures on the same segment. This is not stated explicitly in the PRD; 3 is the assumed threshold.
- The hallucination guard thresholds (`no_speech_prob`, compression ratio) are initial values surfaced in `config.toml` or code constants; they are tunable but not required to be user-configurable fields in v1 config. The architect will decide the exposure point.
- "Quietest point" for force-splitting a 120 s segment is defined as the audio frame with the minimum RMS amplitude within the segment. The exact algorithm is the implementer's decision.
- Pre-roll and post-roll padding (0.3 s) is applied by reading back into the ring buffer before the VAD onset and extending the segment window after the VAD offset. If the ring buffer does not contain 0.3 s of pre-onset audio (e.g., the segment starts near the beginning of a stream), the available audio is used without padding without treating this as an error.
- The launchd plist targets the current user's LaunchAgents directory (`~/Library/LaunchAgents/`). No system-level plist is required.
- "Pause for rest of day" expiry is defined as midnight local time of the day on which the pause was set. A relaunch after midnight resumes recording.
- The `murmur cat today` and `murmur copy today` CLI commands are sub-commands of the same `murmur` entrypoint used for `murmur run`. No separate binary is required.
- The bake-off script (`bakeoff.py`) is a standalone script, not integrated into the main app entrypoint.
- Model weight download in AC-28 refers to mlx-whisper's model fetching mechanism; the checksum to verify against is the one published by the model source (e.g., Hugging Face model card SHA256). The architect will define the verification implementation.

---

## Open Questions

| # | Question | Recommended default |
|---|----------|---------------------|
| OQ-1 | **Bake-off model selection gate:** The PRD describes the bake-off as informing the `local_model` config choice (§8), and §3 lists `large-v3` or `large-v3-turbo` pending bake-off results. Should the spec treat `large-v3` as the build-time default with `large-v3-turbo` as an alternative the bake-off may promote, or should the bake-off be run and its result recorded before the architect begins work? The pipeline has a single human checkpoint; if the bake-off result changes the model, it would need to be run before checkpoint sign-off. | Run the bake-off (M1.5) before or in parallel with M1 implementation; default the config to `large-v3` until bake-off results are reviewed at checkpoint. The AC-26 manual verification covers bake-off completion. |
| OQ-2 | **Hallucination guard threshold values:** The PRD names `no_speech_prob` and compression ratio as guard signals but does not specify threshold values. These materially affect transcription completeness vs. noise rejection. Should threshold values be locked in the spec (making them testable) or left to the implementer with a requirement to surface them as tunable constants? | Surface as named constants (not user-facing config fields in v1) with starting values of `no_speech_prob > 0.6` and `compression_ratio < 2.4 or > 4.5` (standard Whisper guidance). AC-3 tests the guard behavior against these values; if the implementer changes them, AC-3 must be updated. |
| OQ-3 | **"Pause for rest of day" persistence mechanism:** The PRD says pause state persists across restarts but does not specify how. A state file in the app directory, a flag in `config.toml`, or a separate persistent store are all viable. The choice affects the architect's data contract. | Use a small JSON state file (e.g., `~/.murmur_state.json` or in the app directory) separate from `config.toml`, so config changes and runtime state do not conflict. |
| OQ-4 | **Menubar icon character set:** The PRD specifies ● / ⏸ / ⚠ as icon states. `rumps` uses template images or Unicode strings. Should the icon be a Unicode string in the menu title bar, or should the implementer use SF Symbols / image assets for a more native appearance? | Unicode string in the menu bar title is sufficient for a personal tool and removes an asset-management dependency. The architect decides; this is flagged because it affects what "passes" a visual check. |
| OQ-5 | **Spool directory location:** The PRD names `./spool/` in the architecture diagram (relative path) but `~/Transcripts/` for transcript output. It is ambiguous whether the spool is relative to the app's working directory, relative to `~/Transcripts/`, or a configurable path. Since the spool contains transient WAVs (not user data), it should not be inside `~/Transcripts/`. | Place spool at `~/Transcripts/spool/` (inside the transcripts dir so it shares disk-space accounting and is covered by backups) unless the architect has a strong reason to separate it. Make it a configurable path. |

---

## Context Notes

- This is a greenfield project. The repository contains only the `runs/` directory as of 2026-06-11. No existing conventions, test frameworks, or module structures are in place. The PRD's tech stack table (§3) is the sole authority on technology choices.
- FR6 (Notes generation) and M3 are explicitly v2 scope. AC table includes no acceptance criteria for FR6. The menubar item for "Generate notes now" is present in v1 but in a disabled state; its behavior when clicked in v1 is unspecified by the PRD and assumed to be a no-op.
- The PRD references `mlx-whisper` and `silero-vad` as decided dependencies. Compatibility with Apple Silicon / Metal is assumed; no Intel fallback path is required or tested.
- The PRD's §7 acceptance criteria are informal prose; they are decomposed and formalized as AC-20 through AC-26 in this spec, with manual verification procedures defined for each hardware-dependent criterion.
- "v1.5" items (system-audio capture, speaker diarization, live peek window, transcript search, Claude Desktop integration) are enumerated in §9 of the PRD. They are reproduced in Non-Goals to make the boundary explicit and testable.
