# Spec: Murmur Mobile — Android Voice Capture + Self-Hosted Transcription API

**Run ID:** murmur-mobile-2026-06-14 · **Source:** runs/murmur-mobile-2026-06-14/prd.md · **Date:** 2026-06-14

---

## Problem

Jay's accent is consistently mis-transcribed by commercial dictation tools on his Pixel phone. The Whisper large-v3 model already running on his MacBook Pro M4 Pro (via the existing Murmur macOS app) produces accurate transcripts for his accent, as does OpenAI's `gpt-4o-transcribe`. There is no way to reach that quality from his phone today: on-device Whisper is constrained to small models that degrade on accented speech, and no existing mobile app routes audio to his Mac's local model. He loses voice notes or must re-dictate them on the Mac.

## Goal

After this ships, Jay can tap Record on his Pixel, speak a voice note of any length up to 25 minutes, and receive an accurate transcript (his accent correctly handled) inside the app within seconds — delivered by his Mac's large-v3 model over Tailscale when the Mac is awake, or by OpenAI gpt-4o-transcribe as automatic fallback when it is not. The recording is never lost: capture works fully offline, and transcription completes automatically once a reachable engine is available. The transcript can be copied or shared directly into the Claude app for follow-up.

---

## Scope Decision

**v1 = M0 + M1 + M2** (Component A: Murmur Server; Component B: Murmur Mobile core + resilience/fallback).

M3 (`/clean` endpoint, "Clean with Claude" button, long-recording silence-chunking, notes history search, retention-days prune) is **out of scope for this run** and listed in Non-Goals. The PRD's own v1 acceptance criteria do not include cleanup or search. This is the recommended cut; no justification for a different cut exists.

---

## Acceptance Criteria

> Verification method tags: `[server-pytest]` = automated in existing pytest harness in Component A repo; `[android-unit]` = headless JVM unit test in Component B; `[android-robolectric]` = headless Robolectric test in Component B; `[human-on-device]` = verified manually on physical Pixel + live Mac + Tailscale.

### Component A — Murmur Server

| ID | Criterion | Verification |
|----|-----------|-------------|
| AC-1 | `GET /health` with a valid bearer token returns HTTP 200 and a JSON body matching `{"status": "ok", "model_loaded": <bool>, "model": "large-v3", "version": "<semver>"}` within 500 ms (measured against a fake/mocked transcriber so the model lock is not held). | `[server-pytest]` |
| AC-2 | `GET /health` with a missing or incorrect bearer token returns HTTP 401 and does not expose server internals in the response body. | `[server-pytest]` |
| AC-3 | `GET /health` returns HTTP 200 and `model_loaded: false` when the transcriber has not yet completed initialization, without blocking on the model lock. | `[server-pytest]` |
| AC-4 | `POST /transcribe` with a valid token and a valid audio file (m4a/wav/mp3/ogg) returns HTTP 200 and a JSON body containing non-empty `text`, a `segments` array, `language`, `duration_sec` (numeric > 0), `model: "large-v3"`, and `engine: "mac"`. (Tested with a mocked ffmpeg subprocess and a `FakeTranscriber`.) | `[server-pytest]` |
| AC-5 | `POST /transcribe` invokes `ffmpeg` as a subprocess to convert the uploaded audio to 16 kHz mono PCM WAV before calling `MlxWhisperTranscriber.transcribe`. The numpy array passed to `transcribe` has `dtype == float32` and sample rate == 16000. (Verified by capturing the ffmpeg subprocess call and the transcriber call args in tests.) | `[server-pytest]` |
| AC-6 | `POST /transcribe` passes `language` (from the form field, default `"en"`) and an `initial_prompt` built from `vocab_hints` via the existing `build_initial_prompt()` function to the transcriber. When `vocab_hints` is absent, the server uses its configured default vocab hints. | `[server-pytest]` |
| AC-7 | `POST /transcribe` applies the existing `clean_result()` hallucination guard to the `TranscriptionResult` before building the response. If `clean_result()` returns `None`, the server returns HTTP 422 with error code `"no_speech_detected"`. | `[server-pytest]` |
| AC-8 | `POST /transcribe` with a valid token but a file exceeding 25 MB returns HTTP 413 with a JSON error body containing `"detail"` before any ffmpeg or transcriber invocation. The size limit is read from `server.toml` `[server] max_upload_mb`. | `[server-pytest]` |
| AC-9 | `POST /transcribe` with a missing or incorrect bearer token returns HTTP 401 before any ffmpeg or transcriber invocation. | `[server-pytest]` |
| AC-10 | `POST /transcribe` with a file whose MIME type or extension is not in `{m4a, aac, wav, mp3, ogg, opus}` returns HTTP 415. | `[server-pytest]` |
| AC-11 | When `ffmpeg` subprocess exits with a non-zero return code or times out, `POST /transcribe` returns HTTP 422 with error code `"audio_decode_failed"` and no audio data or transcript is retained on disk. | `[server-pytest]` |
| AC-12 | When the transcriber raises `TranscriptionError`, `POST /transcribe` returns HTTP 422 with error code `"transcription_failed"` and deletes the temporary audio file. | `[server-pytest]` |
| AC-13 | While a transcription is in progress (model lock held), a concurrent `GET /health` request completes within 500 ms without waiting for the transcription to finish. (Simulated with a slow `FakeTranscriber` holding a lock and a concurrent health-check request.) | `[server-pytest]` |
| AC-14 | While a transcription is in progress, a concurrent `POST /transcribe` request receives HTTP 503 with error code `"model_busy"` after a configurable busy-wait timeout (default 0 s — immediate 503 on contention; single-flight queue). | `[server-pytest]` |
| AC-15 | The bearer token is compared using a constant-time comparison function. A test verifies that the comparison function is `hmac.compare_digest` or equivalent (not `==`). | `[server-pytest]` |
| AC-16 | Server startup loads config from `server.toml` (TOML format); if the file is missing, all defaults apply and a warning is logged, with no crash. (Test: start the server with no config file; assert 200 on `/health`.) | `[server-pytest]` |
| AC-17 | The bearer token is never written to log output at any log level. A test verifies that logging a request with a valid token produces no log record containing the token string. | `[server-pytest]` |
| AC-18 | Temporary audio files written during `POST /transcribe` are deleted from disk regardless of whether transcription succeeds or fails (verified in both success and error paths). | `[server-pytest]` |
| AC-19 | The launchd plist `com.jay.murmur-server.plist` installs as a user agent, sets `KeepAlive = true` (restart on crash), includes `caffeinate -i` or equivalent power assertion in the program arguments, and points to the correct Python interpreter and server entry point. (Verified by loading and inspecting the plist with `plutil -lint` and key-value assertions in a test.) | `[server-pytest]` |
| AC-20 | `GET /health` and `POST /transcribe` on a live server reached over `https://<machine>.<tailnet>.ts.net` using a valid bearer token return 200 and an accurate transcript for a real 30-second voice note recorded by Jay on his Pixel. | `[human-on-device]` |

### Component B — Murmur Mobile (Android)

#### Settings & Secrets

| ID | Criterion | Verification |
|----|-----------|-------------|
| AC-21 | The Settings screen persists Mac URL, bearer token, OpenAI key, fallback-enabled toggle, and keep-audio toggle to `EncryptedSharedPreferences`. After process death and re-launch, all values are restored with no data loss. | `[android-robolectric]` |
| AC-22 | Bearer token and OpenAI key are retrieved exclusively from `EncryptedSharedPreferences` / Android Keystore and are never written to Logcat at any log level. A test verifies that after saving a key and performing a transcription dispatch, no log record contains the key string. | `[android-unit]` |
| AC-23 | "Test connection" button in Settings triggers `GET /health` against the configured Mac URL with the stored bearer token and displays "Connected — large-v3 loaded" on HTTP 200 `model_loaded: true`, "Server reachable but model not loaded" on HTTP 200 `model_loaded: false`, "Auth failed — check token" on HTTP 401, and "Unreachable" on connection timeout or network error. | `[android-unit]` |

#### Audio Recording & Foreground Service

| ID | Criterion | Verification |
|----|-----------|-------------|
| AC-24 | Starting a recording launches a foreground service with `foregroundServiceType="microphone"` and a persistent notification containing Pause, Resume, and Stop actions. The service is running and the notification is posted within 2 seconds of the user tapping Record. | `[android-robolectric]` |
| AC-25 | A recording session that is started from the foreground continues capturing audio when the user switches to another app or locks the screen, for the duration of the session. (Verified by starting a recording, simulating app-background via Robolectric lifecycle events, and asserting the foreground service is still running.) | `[android-robolectric]` |
| AC-26 | Audio is recorded as AAC in m4a container, mono, 16 kHz sample rate. The output file has a `.m4a` extension, is non-empty, and `MediaMetadataRetriever` reports a sample rate of 16000 Hz and audio channel count of 1. | `[android-robolectric]` |
| AC-27 | When audio focus is lost due to an incoming phone call (simulated `AudioManager.AUDIOFOCUS_LOSS`), the recording service finalizes and saves the current recording to the Outbox with status `pending` without discarding any already-captured audio. The service stops; no audio data is lost. | `[android-unit]` |
| AC-28 | When RECORD_AUDIO permission is denied at runtime, the app displays a rationale dialog and does not crash or start a recording session. | `[android-robolectric]` |
| AC-29 | When POST_NOTIFICATIONS permission is denied (API 33+), the app still starts the recording foreground service and captures audio, and displays a non-fatal UI warning that notifications are unavailable. | `[android-robolectric]` |
| AC-30 | On Stop, the app writes the m4a file to app-private storage and inserts a Room Outbox row with status `pending`, `created_at` timestamp, `duration_sec` (> 0), and `audio_path` pointing to the file, atomically (either both succeed or neither persists). | `[android-unit]` |

#### Outbox / Room State Machine

| ID | Criterion | Verification |
|----|-----------|-------------|
| AC-31 | An Outbox note transitions through exactly these states: `pending` → `transcribing` → `done` on success, or `pending` → `transcribing` → `pending` on retryable failure, or `pending` → `transcribing` → `failed` on non-retryable failure (401 auth error). No other state transitions are valid. | `[android-unit]` |
| AC-32 | A note in `done` state has non-null `raw_text`, non-null `engine` (`"mac"` or `"openai"`), and null `error_message`. A note in `failed` state has non-null `error_message` and null `raw_text`. | `[android-unit]` |
| AC-33 | If the app process is killed while a note is in `transcribing` state, on next launch WorkManager re-queues the note as `pending` (i.e., the dispatcher resets stale `transcribing` rows to `pending` at startup). | `[android-unit]` |

#### Transcription Dispatcher — Engine Selection

| ID | Criterion | Verification |
|----|-----------|-------------|
| AC-34 | When `mac_url` is configured and `GET {mac_url}/health` returns HTTP 200 with `model_loaded: true` within 2500 ms, the dispatcher sends `POST {mac_url}/transcribe` and does NOT call the OpenAI API. On success, `engine` is stored as `"mac"`. | `[android-unit]` |
| AC-35 | When `mac_url` is configured and `GET {mac_url}/health` returns HTTP 200 with `model_loaded: false`, the dispatcher treats the Mac as temporarily unavailable and falls through to the fallback logic (same as an unreachable Mac). | `[android-unit]` |
| AC-36 | When `GET {mac_url}/health` times out (> 2500 ms) or returns a 5xx, the dispatcher treats the Mac as unreachable and, if fallback is enabled and an OpenAI key is present, calls the OpenAI API. On success, `engine` is stored as `"openai"`. | `[android-unit]` |
| AC-37 | When `mac_url` is not configured (empty), the dispatcher skips the Mac health check entirely and proceeds directly to fallback logic. | `[android-unit]` |
| AC-38 | When fallback is disabled in Settings or no OpenAI key is stored, and the Mac is unreachable, the dispatcher leaves the note in `pending` state without calling the OpenAI API or marking the note `failed`. | `[android-unit]` |
| AC-39 | When the Mac returns HTTP 401, the dispatcher marks the note `failed` with `error_message` = `"Auth failed — check token in Settings"` and does NOT retry or call the OpenAI fallback. | `[android-unit]` |
| AC-40 | When the OpenAI API returns HTTP 401, the dispatcher marks the note `failed` with `error_message` = `"OpenAI auth failed — check API key in Settings"` and does NOT retry. | `[android-unit]` |
| AC-41 | The dispatcher sends vocab hints (from Settings, or the default list if Settings is empty) to the Mac as the `vocab_hints` form field and to the OpenAI API as the `prompt` parameter in every transcription request. | `[android-unit]` |
| AC-42 | The dispatcher and its two engine implementations (`MacTranscriptionEngine` and `OpenAITranscriptionEngine`) satisfy a single `TranscriptionEngine` interface. A unit test verifies that both implementations can be substituted for the interface without modification to the dispatcher. | `[android-unit]` |

#### Transcription Dispatcher — Retry & WorkManager

| ID | Criterion | Verification |
|----|-----------|-------------|
| AC-43 | WorkManager dispatches pending notes only when the network constraint is satisfied (no network = no dispatch attempt, note stays `pending`). A unit test asserts that with network unavailable, the dispatcher is never invoked. | `[android-unit]` |
| AC-44 | On retryable failure (network timeout, 5xx, no engine available), WorkManager retries with exponential backoff starting at 30 seconds, capped at 5 minutes, for a maximum of 10 attempts before marking the note `failed` with `error_message` = `"Max retries exceeded"`. | `[android-unit]` |
| AC-45 | Only one WorkManager worker runs per note at a time (no duplicate concurrent transcription of the same note). | `[android-unit]` |

#### Recording Cap

| ID | Criterion | Verification |
|----|-----------|-------------|
| AC-46 | When a recording session reaches 25 minutes, the app automatically stops the recording, saves the file, and displays a non-dismissible warning: "Recording capped at 25 min — the server limit is 25 MB / 30 min. For longer sessions, stop and start a new note." | `[android-unit]` |

#### Note List & Note Detail

| ID | Criterion | Verification |
|----|-----------|-------------|
| AC-47 | The Home screen displays a list of all Outbox notes ordered by `created_at` descending. Each row shows: elapsed duration, status badge (`pending` / `transcribing` / `done` / `failed`), and a preview of `raw_text` (first 80 chars, or "Transcribing…" / "Pending…" / "Failed" if not done). | `[android-robolectric]` |
| AC-48 | The Note detail screen displays: full `raw_text`, status, `engine` tag (`Mac` or `OpenAI`), and action buttons Copy, Share, Retry (if `failed`), Delete. | `[android-robolectric]` |
| AC-49 | Tapping Copy places `raw_text` on the system clipboard. A Robolectric test asserts `ClipboardManager.getPrimaryClip().getItemAt(0).getText()` equals `raw_text`. | `[android-robolectric]` |
| AC-50 | Tapping Share opens the Android share sheet with `text/plain` MIME type and `raw_text` as the content. A Robolectric test asserts the launched Intent action is `ACTION_SEND` with `EXTRA_TEXT` = `raw_text`. | `[android-robolectric]` |
| AC-51 | Tapping Delete removes the note from Room and, if `audio_path` is non-null, deletes the audio file from app-private storage. Both succeed or an error is shown; no orphaned audio files are left if the Room row is deleted. | `[android-unit]` |
| AC-52 | When `keep_audio` is `false` (default), the audio file at `audio_path` is deleted immediately after the note transitions to `done` status. | `[android-unit]` |
| AC-53 | When `keep_audio` is `true`, the audio file is retained after transcription and is playable from the Note detail screen (verified by asserting `audio_path` is non-null and the file exists after `done` transition). | `[android-unit]` |

#### End-to-End System Acceptance

| ID | Criterion | Verification |
|----|-----------|-------------|
| AC-54 | Record a 2-minute voice note on the Pixel with the Mac awake and Tailscale connected. The note reaches `done` status in-app within 30 seconds of stopping the recording, with `engine = "mac"` and `raw_text` containing an accurate transcript of Jay's spoken content (accent correctly rendered, at minimum: proper nouns from the vocab hints list spelled correctly). | `[human-on-device]` |
| AC-55 | With the Mac asleep (Tailscale health check times out), record a 30-second note. The note eventually reaches `done` status with `engine = "openai"` and the correct transcript, without any manual intervention. | `[human-on-device]` |
| AC-56 | Enable airplane mode before recording. Record a 30-second note. Note appears in the list as `pending`. Disable airplane mode. Note automatically transitions to `done` without manual intervention, using whichever engine is reachable. | `[human-on-device]` |
| AC-57 | Start a recording on the Pixel, lock the screen, wait 60 seconds, unlock — recording is still active (foreground service running, elapsed time advancing). Stop the recording; a complete note is created in the Outbox. | `[human-on-device]` |
| AC-58 | Start a recording, then switch to another app (e.g., Chrome) for 60 seconds. Return to Murmur; recording is still active. Stop the recording; a complete note is created. | `[human-on-device]` |
| AC-59 | During an active recording, receive a real incoming phone call. After the call, the note is in the Outbox as `pending` (not discarded) with a non-zero duration. | `[human-on-device]` |
| AC-60 | From the Note detail screen, tap Share → select the Claude app → confirm that the full `raw_text` appears in the Claude app's compose field. | `[human-on-device]` |
| AC-61 | The Mac server is reachable only via Tailscale (`https://<machine>.<tailnet>.ts.net`). `curl` from a non-Tailscale device (e.g., a different WiFi client not on the tailnet) cannot reach port 8080 or the Tailscale HTTPS URL. Port 8080 is bound to `127.0.0.1` only. | `[human-on-device]` |

---

## Non-Goals

The following are explicitly out of scope for this run (v1). Any implementation that touches these areas without an approved scope change is a deviation.

1. **`/clean` endpoint and "Clean with Claude" button.** The Anthropic `claude-haiku-4-5` cleanup flow (M3) is deferred to v1.5. The `[clean]` section in `server.toml` may be parsed and ignored but must not be implemented or exercised.
2. **Long-recording silence-chunking.** Automatic splitting of recordings that exceed 25 MB / 30 min into sub-chunks for sequential transcription is v1.5. v1 caps a single note at 25 minutes and warns the user (AC-46).
3. **Notes history search and full-text indexing.** No search UI, no Room FTS table, no tagging or folder grouping.
4. **On-device Whisper transcription on the phone.** The phone never runs a Whisper model locally. Transcription always routes off-device (Mac or OpenAI).
5. **Always-on / ambient recording.** v1 is intentional capture only: recording starts from an explicit user action.
6. **Real-time / streaming transcription.** All transcription is batch: record fully, then POST, then receive response.
7. **Widget, quick-settings tile, or Assistant integration** for starting a recording without opening the app.
8. **Play Store distribution, multi-user support, or sync of notes to the Mac desktop app.**
9. **Speaker diarization.**
10. **Retention-days auto-prune.** The Room schema may include the `retention_days` config field but automatic pruning is not implemented in v1.
11. **Re-implementing any existing transcription logic.** The server wraps `MlxWhisperTranscriber`, `build_initial_prompt()`, `clean_result()`, and `ModelManager` from `src/murmur/transcribe/`. No duplication of these.

---

## Edge Cases

| # | Case | Expected behavior |
|---|------|-------------------|
| E-1 | Audio upload to `POST /transcribe` is exactly 25 MB (at the limit). | Accepted; processed normally. |
| E-2 | Audio upload to `POST /transcribe` is 25 MB + 1 byte (over limit). | HTTP 413; no ffmpeg invocation; no temp file retained. |
| E-3 | Audio file is a valid container but contains only silence (all segments filtered by `clean_result()`). | HTTP 422 with `error_code: "no_speech_detected"`; temp file deleted. |
| E-4 | Audio file is syntactically corrupt (ffmpeg cannot decode it). | HTTP 422 with `error_code: "audio_decode_failed"`; temp file deleted. |
| E-5 | Audio file extension is `.m4a` but MIME type is `application/octet-stream` (phone sends generic MIME). | Server accepts based on file extension as well as MIME; processes normally. |
| E-6 | Audio file claims to be `.txt` (wrong extension, wrong MIME). | HTTP 415; rejected before ffmpeg. |
| E-7 | `vocab_hints` form field contains an empty string or whitespace-only entries. | `build_initial_prompt()` strips empty entries; if all are empty, `initial_prompt=None` is passed (per existing behavior). |
| E-8 | `vocab_hints` form field is absent entirely. | Server uses its configured default vocab hints list from `server.toml` `[stt] vocab_hints`. |
| E-9 | `language` form field is absent. | Defaults to `"en"`. |
| E-10 | A second `POST /transcribe` arrives while one is in progress (model lock held). | Second request receives HTTP 503 with `error_code: "model_busy"` immediately (single-flight queue, no queuing of waiters). |
| E-11 | `GET /health` arrives while `POST /transcribe` is in progress. | Health responds within 500 ms with HTTP 200; `model_loaded` reflects readiness but health check does NOT block on the model lock. |
| E-12 | Bearer token in `Authorization` header is present but incorrect (wrong value). | HTTP 401; constant-time compare used; no timing oracle. |
| E-13 | `Authorization` header is missing entirely. | HTTP 401. |
| E-14 | `Authorization` header value is `"Bearer "` (empty token after prefix). | HTTP 401. |
| E-15 | `server.toml` is missing at server startup. | Server starts with all defaults; logs a warning; `/health` returns 200. |
| E-16 | `server.toml` is present but malformed (invalid TOML). | Server starts with all defaults; logs a warning; `/health` returns 200. No crash. |
| E-17 | `server.toml` contains unknown keys. | Unknown keys are logged as warnings and ignored; known keys applied normally. |
| E-18 | `MURMUR_SERVER_TOKEN` environment variable is not set and no Keychain entry exists at startup. | Server fails to start with a clear error message: "Bearer token not configured. Set MURMUR_SERVER_TOKEN env var or add to Keychain." |
| E-19 | ffmpeg is not installed on the Mac at server startup. | Server starts; `/health` returns 200. `POST /transcribe` returns HTTP 422 with `error_code: "audio_decode_failed"` and a log message indicating ffmpeg is missing. |
| E-20 | mlx-whisper model weights are not cached locally at startup. | Server starts; `/health` returns `{"status": "ok", "model_loaded": false, ...}` until the model is loaded. Transcription requests return HTTP 503 with `error_code: "model_not_loaded"`. |
| E-21 | Mac is on the Tailscale network but the server process is not running. | Android health check times out (connection refused); dispatcher falls through to OpenAI fallback (if enabled). |
| E-22 | Tailscale is disconnected on the Mac (no tailnet route). | Android health check times out; dispatcher falls through to fallback. |
| E-23 | Tailscale is disconnected on the Pixel (no network to tailnet). | Health check times out (network error); dispatcher falls through to fallback if enabled; note stays `pending` if fallback disabled. |
| E-24 | OpenAI API returns HTTP 429 (rate limited). | Dispatcher treats as retryable failure; WorkManager retries with exponential backoff. Note stays `pending`. |
| E-25 | OpenAI API returns HTTP 401 (invalid key). | Dispatcher marks note `failed` with `error_message = "OpenAI auth failed — check API key in Settings"`. No retry. |
| E-26 | Both Mac and OpenAI are unreachable, fallback enabled, with a valid OpenAI key. | Note stays `pending`; WorkManager retries on next connectivity event. No data lost. |
| E-27 | Both Mac and OpenAI are unreachable, fallback disabled. | Note stays `pending`; WorkManager retries on next connectivity event. No data lost. |
| E-28 | Phone is in airplane mode when Stop is tapped. | Note is created in Outbox as `pending` immediately. No dispatch attempt is made (WorkManager network constraint unsatisfied). |
| E-29 | Incoming phone call interrupts an active recording. | Recording is finalized and saved with whatever audio was captured. Outbox row inserted. No data discarded. |
| E-30 | RECORD_AUDIO permission is revoked while a recording is in progress (mid-session). | Recording service catches the audio failure, finalizes and saves whatever was captured, stops the service, and shows a notification: "Recording stopped — microphone permission revoked." |
| E-31 | POST_NOTIFICATIONS permission denied (Android 13+). | Foreground service still starts. Recording proceeds. A non-fatal in-app warning is displayed. (Notification may not appear; service is not aborted.) |
| E-32 | App-private storage is full when the recording is finalized (disk write fails). | Note is NOT inserted into Outbox (partial data not retained). User sees an error: "Could not save recording — storage full." |
| E-33 | Room DB write (Outbox insert) succeeds but audio file write fails. | Note row is rolled back or deleted; no orphaned DB row referencing a missing file. |
| E-34 | Note is in `transcribing` state when the app process is killed (WorkManager worker crash). | On next WorkManager execution or app launch, the stale `transcribing` row is reset to `pending` and re-queued. |
| E-35 | User taps Delete on a `done` note where `audio_path` is non-null but the file has already been deleted by another code path. | Room row is deleted; missing file is logged as a warning (not an error). No crash. |
| E-36 | User taps Retry on a `failed` note that has a valid audio file. | Note is reset to `pending` and re-queued for dispatch. Error message cleared. |
| E-37 | User taps Retry on a `failed` note whose audio file has been deleted (e.g., keep_audio=false but transcription failed before deletion). | Retry is disabled or shows "Audio file no longer available — cannot retry." Note is not re-queued. |
| E-38 | `mac_url` is configured with a trailing slash (e.g., `https://mac.ts.net/`). | App normalizes the URL by stripping the trailing slash before constructing endpoint paths. |
| E-39 | `mac_url` is configured with a non-HTTPS URL (e.g., `http://...`). | App warns in Settings: "Mac URL should use HTTPS. HTTP connections are not allowed over Tailscale." Request is blocked. |
| E-40 | A `pending` note with zero-byte audio file (recording failed silently). | Dispatcher detects empty file; marks note `failed` with `error_message = "Audio file is empty — nothing to transcribe."` No upload attempted. |
| E-41 | Two recordings are stopped in rapid succession (< 1 second apart). | Both notes are independently inserted into Outbox with distinct `id` and `created_at`. No collision. |
| E-42 | Server receives a multipart upload with no `file` field. | HTTP 400 with `error_code: "missing_file"`. |
| E-43 | Server receives a multipart upload where `file` has zero bytes. | HTTP 400 with `error_code: "empty_file"` before any ffmpeg invocation. |

---

## Constraints

### Performance
- R-1: `GET /health` must return within 500 ms under any server load (including while a transcription is in progress).
- R-2: A 1–2 minute voice note processed by the Mac server (large-v3 via Metal on M4 Pro) should return within 10 seconds of the POST being received. (This is an informational target, not a gated criterion, because it depends on runtime hardware. It is verified on-device in AC-54.)
- R-3: The Android dispatcher's health-check timeout is exactly 2500 ms. Any response after 2500 ms is treated as unreachable.

### Compatibility
- R-4: Component A requires Python 3.11+ (matches existing repo constraint) and Apple Silicon macOS (enforced by `assert_apple_silicon()` in the existing transcriber).
- R-5: Component B targets Android minSdk 31 (Android 12), compileSdk 35. The foreground service type `"microphone"` requires explicit declaration for API 34+ (FOREGROUND_SERVICE_MICROPHONE permission); the behavior must be correct on API 31–35.
- R-6: The Android Toolchain is pinned: JDK 21, Gradle 8.9, AGP 8.7.3, Kotlin 2.0.21, Compose BOM 2024.10.01. The build must not depend on any Android emulator or physical device. `JAVA_HOME` must be set to the JDK 21 installation on every Gradle invocation.

### Security
- R-7: The bearer token is never stored in `server.toml`, in any config file, in logs, or in source code. It is read from the `MURMUR_SERVER_TOKEN` environment variable or macOS Keychain only.
- R-8: The server binds to `127.0.0.1` only. Port is not directly reachable from outside the machine; Tailscale provides the network overlay.
- R-9: TLS is handled entirely by `tailscale serve`. The server does not terminate TLS itself and does not accept self-signed certificates from clients.
- R-10: Audio bytes and transcript text are never written to server logs, Android Logcat, or any persistent log file.
- R-11: Android secrets (bearer token, OpenAI key) are stored in `EncryptedSharedPreferences` backed by Android Keystore. They are transmitted only over HTTPS.

### Test Coverage
- R-12: New-code line coverage for Component A server module must be ≥ 80%, measured by the existing `pytest-cov` setup (`fail_under = 80` in `pyproject.toml`).
- R-13: New-code line coverage for Component B must be ≥ 80%, measured by the Android test runner with JaCoCo.
- R-14: All server tests run without `ffmpeg`, `tailscale`, or mlx-whisper model weights installed. `ffmpeg` subprocess calls must be mocked; the transcriber must be injected as a `FakeTranscriber` (matches existing `tests/fakes.py` pattern).
- R-15: All Android logic tests run headlessly without an emulator. Device-only ACs (AC-54 through AC-61) are `[human-on-device]` only.

### Process Management
- R-16: The server must run as a launchd user agent (`com.jay.murmur-server.plist`) that starts at login and restarts on crash.
- R-17: A single-instance lockfile or PID file prevents two server processes from running simultaneously.

### Logging
- R-18: Structured logs written to `~/Library/Logs/murmur-server.log` by default. Log rotation is not required in v1.

---

## Assumptions

The following trivial ambiguities have been resolved with sensible defaults and recorded here. They do not require human confirmation at the checkpoint unless specifically overridden.

1. `server.toml` default vocab hints for the server match the PRD's §5.5 list: `["Rivy", "Yassir", "Refendr", "Chidera", "Kemi", "Vaasa", "GKE", "BigQuery", "FIN-FSA"]`. These differ from `config.py`'s existing `_DEFAULT_VOCAB_HINTS` (which are Mac-app defaults); the server has its own list as specified in the PRD.
2. The `MURMUR_SERVER_TOKEN` environment variable name is used for the bearer token on the server side. Keychain service name for the token is `murmur-server-token` (mirroring the pattern in `mac/keychain.py` which uses `murmur-openai` for the OpenAI key).
3. The server's "version" field in `/health` response matches the package version from `pyproject.toml` (currently `0.1.0`). Bumping to `1.0.0` for the server component is a decision for the architect/implementer.
4. The single-flight queue uses `asyncio.Lock` (FastAPI is async); a request arriving while the lock is held immediately receives HTTP 503 (no queuing of waiters, consistent with the PRD's "single-flight queue" language).
5. Maximum busy-wait duration before 503 on `POST /transcribe` is 0 seconds (immediate 503 on lock contention). This is not configurable in v1.
6. The `duration_sec` in the `POST /transcribe` response is derived from the ffmpeg-decoded PCM (number of samples / sample rate), not from the container metadata.
7. The OpenAI transcription endpoint used in the fallback is `https://api.openai.com/v1/audio/transcriptions` with `model=gpt-4o-transcribe`, `language=en`, and the m4a file sent directly (OpenAI accepts m4a natively; no server-side re-encoding needed on the phone).
8. The Android app sends the `Authorization: Bearer <token>` header. The server expects exactly this format.
9. Default vocab hints in Android Settings are empty (the user may fill them in). The Mac server's configured default list is applied server-side when `vocab_hints` is absent from the request (see AC-8 / AC-6).
10. The Android `TranscriptionEngine` interface is defined as a Kotlin interface (not sealed class), matching the `Transcriber` Protocol pattern in Component A.
11. The Outbox Room table is named `notes` with columns: `id` (UUID, PK), `created_at` (Long epoch ms), `duration_sec` (Double), `audio_path` (String nullable), `raw_text` (String nullable), `engine` (String nullable), `status` (String: `pending`/`transcribing`/`done`/`failed`), `error_message` (String nullable).
12. WorkManager exponential backoff: initial delay 30 s, multiplier 2×, cap 5 min, max 10 attempts. After 10 attempts, note is marked `failed` with `error_message = "Max retries exceeded"`.
13. "Retry" on a `failed` note resets attempt count to 0 and status to `pending`.
14. The Android app checks for a non-empty audio file (size > 0 bytes) before dispatching (E-40). Zero-byte = immediate `failed`.
15. Mac URL in Settings is validated as a syntactically valid HTTPS URL before saving; an invalid URL shows an inline error and is not persisted.

---

## Open Questions

Material ambiguities that change the build. These must be answered at the human checkpoint before implementation begins.

| # | Question | Recommended default |
|---|----------|---------------------|
| OQ-1 | **Exact Pixel model and Android version.** Which Pixel model does Jay use, and which Android version is it running? This affects: (a) whether `FOREGROUND_SERVICE_MICROPHONE` permission declaration is mandatory (API 34+ only), (b) whether the `AudioRecord` vs `MediaRecorder` API choice has known bugs on that device, (c) human-on-device AC verification setup. | Assume Pixel 8 or later running Android 14 (API 34). The implementation must be correct on API 31–35; pin to API 34 behavior for foreground service type declaration. |
| OQ-2 | **OpenAI fallback default-on vs default-off.** The PRD says "recommended on" but flags this as a question for Jay. Default-on means the first recording silently sends audio to OpenAI when the Mac is down; default-off means the user must explicitly enable it in Settings. This is a privacy decision: default-on sends audio off-device without an explicit user action. | **Default ON.** The PRD recommends it; Jay is the sole user; the Settings fallback toggle is visible and prominently labeled "Fallback to OpenAI (audio leaves your device)". If Jay wants it off by default, the Settings screen makes it trivial to enable. |
| OQ-3 | **Claude cleanup (/clean) in v1 or v1.5.** The PRD marks this as a question for Jay. Implementing `/clean` + the "Clean with Claude" button adds scope: Anthropic API key management on the server, additional endpoint, additional Android UI, and an additional secret in Settings. | **Defer to v1.5.** The v1 acceptance criteria in the PRD do not include cleanup. The spec non-goals list it explicitly. Recommend Jay confirm this deferral at the checkpoint. |
| OQ-4 | **Maximum recording duration cap: 25 min or 30 min?** The server's `max_duration_min` is 30 min, but the PRD §6.4 says "cap a single note at 25 min and warn." 25 min at 16 kHz mono AAC is well under 25 MB, so the file size limit is not the binding constraint — the duration limit is. Using 25 min client-side leaves a 5-min buffer before the server would reject it; using 30 min exactly risks a race if encoding produces a slightly larger file. | **25-minute client-side cap** (AC-46). The server's `max_duration_min = 30` is unchanged, providing a 5-minute safety buffer. |
| OQ-5 | **Anthropic key on the server for `/clean`.** Even if `/clean` is deferred, should the server's Keychain/env lookup include an `ANTHROPIC_API_KEY` entry for future use, or should it be entirely absent until v1.5? | **Absent from v1.** No Anthropic key lookup, no `/clean` route stub, no config section parsed-but-acted-on. The `[clean]` TOML section may be parsed and silently ignored (matches `config.py`'s pattern of parsing future sections). |
| OQ-6 | **Server entry point and package structure.** The existing repo's `murmur` package is in `src/murmur/`. Should the server live at `src/murmur/server/` (same package, new sub-package) or as a sibling `src/murmur_server/`? This determines import paths and whether the server is built/installed as part of the existing `murmur` package or a separate install target. | **`src/murmur/server/`** — same package, new sub-package. This avoids a second install target, reuses existing `pyproject.toml` configuration, and lets the server import `murmur.transcribe.*` directly without cross-package gymnastics. The architect should confirm. |
| OQ-7 | **Health check: `model_loaded` semantics when model is initializing.** During the window between server startup and the model being ready, `/health` should return `model_loaded: false`. Should a `POST /transcribe` during this window return HTTP 503 `model_not_loaded` or queue behind the model initialization? | **Immediate HTTP 503 `model_not_loaded`** — no queuing. The phone will retry via WorkManager. This is simpler and avoids unbounded wait times. |
| OQ-8 | **Single-instance enforcement mechanism.** Should the server use a PID file at a fixed path (e.g., `/tmp/murmur-server.pid`) or rely on launchd's single-instance behavior (only one plist, launchd will not start a second)? A PID file allows CLI detection; launchd alone is sufficient operationally. | **launchd only** (no separate PID file). launchd guarantees single-instance for user agents. A PID file would require cleanup logic and is redundant. If direct `python -m murmur.server` invocation outside launchd is supported, add a PID-file guard as a belt. Recommend belt for v1. |

---

## Context Notes

### Existing transcription module (Component A must reuse, not reimplement)

- `src/murmur/transcribe/base.py`: defines `Transcriber` Protocol, `TranscriptionResult`, `SegmentScore`, `TranscriptionError`. The server's dependency injection point is the `Transcriber` protocol.
- `src/murmur/transcribe/local.py`: `MlxWhisperTranscriber.transcribe(audio: np.ndarray, *, language: str, initial_prompt: str | None) -> TranscriptionResult`. Takes a float32 mono 16 kHz numpy array, NOT a file path. The server must decode uploaded audio via ffmpeg before calling this.
- `src/murmur/transcribe/prompt.py`: `build_initial_prompt(vocab_hints: Sequence[str]) -> str | None`. Returns `None` when hints list is empty; format is `"Glossary: hint1, hint2."`.
- `src/murmur/transcribe/guard.py`: `clean_result(r: TranscriptionResult) -> str | None`. Filters hallucination artifacts. Returns `None` when nothing survives. Thresholds: `no_speech_prob > 0.6 AND avg_logprob < -1.0`, `compression_ratio > 2.4`, known filler phrases.
- `src/murmur/transcribe/models.py`: `ModelManager.ensure(model: str) -> str`. Downloads + verifies model weights; fast local path if already cached.
- `src/murmur/config.py`: TOML config pattern: `load_config(path) -> (Config, warnings)`. Never raises; missing/malformed = defaults + warning. Unknown keys = logged warning, ignored. Bearer token must NOT be in config (matches `mac/keychain.py` precedent).
- `src/murmur/mac/keychain.py`: `get_openai_api_key(env, run)` — reads from env var first, then macOS `security` CLI. The bearer token retrieval should follow the same pattern.

### Existing test infrastructure (Component A tests must match)

- `tests/fakes.py`: `FakeTranscriber` (scripted results or raises `TranscriptionError`), `FixedClock`, `RecordingNotifier`, `FakeDiskFree`. Server tests must inject `FakeTranscriber` and mock the `ffmpeg` subprocess.
- `tests/conftest.py`: fixtures for `tmp_home`, `tmp_transcripts`, `tmp_spool`, `notifier`, `clock`, `fake_source`, `amplitude_vad`, `fake_transcriber`.
- `pyproject.toml`: pytest markers: `hardware` (deselected by default). New server tests requiring real ffmpeg, tailscale, or model weights must be marked `hardware`.
- Coverage: `fail_under = 80` enforced by `pytest-cov`. New server code must hit ≥ 80%.
- Linting: ruff (`line-length = 100`, `target-version = py311`). Type checking: mypy strict mode.

### Android toolchain (Component B)

The version matrix proven to build headlessly is recorded in `state.md`. Critical constraint: `JAVA_HOME` must point to JDK 21 (not the default JDK 22) on every Gradle invocation. All build and test commands must pass `JAVA_HOME=$(/usr/libexec/java_home -v 21)` explicitly or via `org.gradle.java.home` in `gradle.properties`.

### Separate repo

Component B is a new Android project in a sibling repo `~/Documents/MurmurMobile` (created during implementation). Run artifacts (`spec.md`, `plan.md`, etc.) remain in `/Users/faithomojola/Documents/MurMur/runs/murmur-mobile-2026-06-14/` regardless of which repo code lands in.

### Referenced systems not accessible

- Tailscale admin console (tailnet ACL, machine names): cannot be accessed. Spec uses placeholder `<machine>.<tailnet>.ts.net`. Actual tailnet URL is Jay's private configuration.
- The physical Pixel device: all `[human-on-device]` ACs require Jay's device + live Mac + Tailscale setup and cannot be automated.
