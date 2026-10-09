# Plan: Murmur Mobile — Android Voice Capture + Self-Hosted Transcription API

**Run ID:** murmur-mobile-2026-06-14 · **Spec:** runs/murmur-mobile-2026-06-14/spec.md · **Date:** 2026-06-14

## Approach

Two components shipped across two repos. **Component A — Murmur Server** is a FastAPI app added as a new sub-package `src/murmur/server/` in THIS repo (OQ-6 default), on branch `feature/murmur-mobile-2026-06-14`. It is a thin HTTP shell over the *existing, untouched* `src/murmur/transcribe/` module: it authenticates a bearer token (constant-time), decodes uploaded audio to float32/16 kHz mono PCM via an `ffmpeg` subprocess, calls `MlxWhisperTranscriber.transcribe`, runs the audio through the existing `build_initial_prompt()` and `clean_result()` functions, and returns JSON. A single-flight `asyncio.Lock` serializes transcription; `/health` never touches the lock. It runs under launchd + `caffeinate` and is exposed only via `tailscale serve` (server binds `127.0.0.1`). All of Component A is gated by the existing pytest/ruff/mypy harness with `FakeTranscriber` + a mocked ffmpeg subprocess — no ffmpeg, tailscale, or model weights needed in CI.

**Component B — Murmur Mobile** is a NEW Kotlin/Jetpack-Compose Android app in sibling repo `~/Documents/MurmurMobile` (the implementer `git init`s it), structured on the *proven* `/tmp/android-toolchain-check` skeleton with the pinned toolchain (JDK 21, Gradle 8.9, AGP 8.7.3, Kotlin 2.0.21, Compose BOM 2024.10.01, compileSdk 35 / minSdk 31). It records voice notes via a `microphone`-typed foreground service (AAC/m4a, mono, 16 kHz), persists them to a Room `notes` Outbox, and dispatches transcription via WorkManager: Mac-first (health-gated over Tailscale), OpenAI `gpt-4o-transcribe` fallback (OQ-2 default ON). Two engines (`MacTranscriptionEngine`, `OpenAITranscriptionEngine`) implement one `TranscriptionEngine` Kotlin interface so the dispatcher is engine-agnostic. Secrets live in `EncryptedSharedPreferences`. All dispatcher/outbox/engine-selection/secrets/recording-policy logic is unit/Robolectric tested headlessly (`./gradlew testDebugUnitTest`); the 9 device ACs (AC-20, AC-54..AC-61) are a manual runbook.

**Build order:** server first (the Android dispatcher's HTTP contract depends on the server's API surface) → mobile core → mobile resilience/fallback. The Android↔server contract is defined once, authoritatively, in **Contracts** below and is the linchpin both repos build against.

## Architecture

```
┌─────────────────────── Pixel (Component B, ~/Documents/MurmurMobile) ───────────────────────┐
│  RecordScreen ──tap──▶ RecordingService (FGS type=microphone)                                │
│                          │ MediaRecorder → AAC/m4a mono 16 kHz → app-private file            │
│                          ▼ on Stop / 25-min cap / audio-focus-loss                           │
│                   Room `notes` (status=pending)  ◀──── Outbox state machine                  │
│                          │                                                                    │
│                   WorkManager (network constraint, exp backoff 30s→5m, max 10, unique/note)  │
│                          ▼                                                                    │
│                   TranscriptionDispatcher                                                     │
│             ┌────────────┴───────────────┐                                                   │
│   MacTranscriptionEngine          OpenAITranscriptionEngine   (both : TranscriptionEngine)   │
│      │ GET /health (2500ms)             │ POST v1/audio/transcriptions                        │
└──────┼────────────────────────────────┼──────────────────────────────────────────────────┘
       │ HTTPS over Tailscale            │ HTTPS
       ▼                                 ▼
┌──── Mac (Component A, src/murmur/server/) ────┐   api.openai.com
│  tailscale serve (TLS) ──▶ 127.0.0.1:8080     │   model=gpt-4o-transcribe
│  FastAPI app                                  │
│   GET  /health   (no lock; <500ms)            │
│   POST /transcribe (bearer; asyncio.Lock)     │
│     → ffmpeg subprocess → float32/16k mono    │
│     → MlxWhisperTranscriber.transcribe  ◀── REUSED, unchanged                                │
│     → build_initial_prompt() / clean_result() ◀── REUSED, unchanged                          │
│  launchd (KeepAlive) + caffeinate -i          │
└───────────────────────────────────────────────┘
```

**Main path (AC-54):** tap Record → FGS starts within 2s → m4a captured → Stop → Room row `pending` → WorkManager (network OK) → dispatcher `GET {mac}/health` 200/`model_loaded:true` → `POST {mac}/transcribe` (multipart: file + vocab_hints + language) → server decodes → transcribes → cleans → JSON `{text, segments, engine:"mac",…}` → Room row `done`, `engine="mac"` → (keep_audio=false) audio deleted.

## Contracts

> **This section is authoritative for both repos.** The Android client and the server are built to the exact shapes below. No implementer invents an interface.

### A. HTTP API — Android↔server (the linchpin)

Base URL: `https://<machine>.<tailnet>.ts.net` (TLS terminated by `tailscale serve`; server itself binds `127.0.0.1:8080`, plain HTTP behind the proxy — R-8/R-9). All requests carry `Authorization: Bearer <token>`.

#### `GET /health`

- **Auth:** required. Missing/empty/wrong → **401** (AC-2, AC-9, E-12..E-14), body `{"error_code":"unauthorized","detail":"Invalid or missing bearer token"}`. No server internals leaked (AC-2).
- **Never acquires the model lock** (AC-1, AC-3, AC-13, R-1, E-11). Must return < 500 ms even mid-transcription.
- **200** body (exact keys):
  ```json
  {"status":"ok","model_loaded":true,"model":"large-v3","version":"1.0.0"}
  ```
  `model_loaded` is a plain boolean read from a non-blocking readiness flag set when `ModelManager.ensure()` + transcriber construction completes (AC-1, AC-3, E-20). `version` = server semver (see §C; bumped to `1.0.0`, Assumption #3 + ADR-1).

#### `POST /transcribe`

- **Content-Type:** `multipart/form-data`.
- **Fields:**
  | field | type | required | default | notes |
  |-------|------|----------|---------|-------|
  | `file` | file (m4a/aac/wav/mp3/ogg/opus) | yes | — | the audio |
  | `language` | text | no | `"en"` | AC-6, E-9 |
  | `vocab_hints` | text (comma-separated) OR repeated field | no | server `[stt] vocab_hints` | AC-6, E-7, E-8 |
- **Validation order (each step short-circuits before the next):**
  1. Auth → **401** before any work (AC-9), body `{"error_code":"unauthorized",…}`.
  2. Single-flight: if lock held → **503** `{"error_code":"model_busy","detail":"A transcription is already in progress"}` immediately (AC-14, E-10; 0 s busy-wait, Assumption #5).
  3. Model not yet loaded → **503** `{"error_code":"model_not_loaded",…}` (E-20, OQ-7).
  4. `file` field absent → **400** `{"error_code":"missing_file",…}` (E-42).
  5. `file` zero bytes → **400** `{"error_code":"empty_file",…}` (E-43).
  6. Size > `max_upload_mb` (default 25) → **413** `{"error_code":"file_too_large","detail":"…"}` before ffmpeg/temp-file (AC-8, E-1, E-2). Enforced by streaming/size check; 25 MB accepted, 25 MB+1 rejected.
  7. Extension AND MIME both outside `{m4a,aac,wav,mp3,ogg,opus}` → **415** `{"error_code":"unsupported_media_type",…}` (AC-10, E-5, E-6). Accept on *either* extension or MIME match (E-5: `.m4a` + `application/octet-stream` passes).
  8. Acquire lock → write temp file → ffmpeg subprocess → decode.
     - ffmpeg non-zero exit / timeout / not installed → **422** `{"error_code":"audio_decode_failed",…}`, temp file deleted (AC-11, E-4, E-19).
  9. `MlxWhisperTranscriber.transcribe(pcm_f32, language=…, initial_prompt=build_initial_prompt(hints))` (AC-5, AC-6).
     - raises `TranscriptionError` → **422** `{"error_code":"transcription_failed",…}`, temp deleted (AC-12).
  10. `clean_result(result)` → if `None` → **422** `{"error_code":"no_speech_detected",…}`, temp deleted (AC-7, E-3).
  11. Temp file deleted in a `finally` regardless of outcome (AC-18); lock released in `finally` (AC-14).
- **200** body (exact keys):
  ```json
  {"text":"<clean text>","segments":[{"text":"…","no_speech_prob":0.0,
   "compression_ratio":1.0,"avg_logprob":-0.3}, …],
   "language":"en","duration_sec":42.7,"model":"large-v3","engine":"mac"}
  ```
  `text` is `clean_result()` output (non-empty). `segments` mirrors `TranscriptionResult.segments`. `duration_sec` = `len(pcm)/16000` from decoded PCM, > 0 (Assumption #6). `engine` always `"mac"` (AC-4).
- **Error body shape (all non-2xx):** `{"error_code":"<string>","detail":"<human msg>"}`. Error-code strings are the contract; the Android `MacTranscriptionEngine` switches on HTTP status + `error_code`.

**Error-code registry (server):** `unauthorized` (401) · `model_busy` (503) · `model_not_loaded` (503) · `missing_file` (400) · `empty_file` (400) · `file_too_large` (413) · `unsupported_media_type` (415) · `audio_decode_failed` (422) · `transcription_failed` (422) · `no_speech_detected` (422).

### B. OpenAI fallback contract (Android → OpenAI, Assumption #7)

`POST https://api.openai.com/v1/audio/transcriptions`, `Authorization: Bearer <openai_key>`, multipart: `file=<the m4a, sent natively>`, `model=gpt-4o-transcribe`, `language=en`, `prompt=<vocab hints joined>` (AC-41). 200 → `{"text":…}` → store with `engine="openai"` (AC-36). 401 → `failed`, `"OpenAI auth failed — check API key in Settings"` (AC-40, E-25). 429/5xx/timeout → retryable (AC-44, E-24).

### C. `server.toml` schema (mirrors `config.py` pattern: never raises; missing/malformed → defaults+warning; unknown keys warned+ignored — AC-16, E-15..E-17)

```toml
[server]
host = "127.0.0.1"          # R-8; not user-overridable to non-loopback in v1
port = 8080
max_upload_mb = 25          # AC-8
busy_wait_s = 0             # Assumption #5 (parsed; fixed 0 in v1)
log_path = "~/Library/Logs/murmur-server.log"   # R-18

[stt]
model = "large-v3"
language = "en"
vocab_hints = ["Rivy","Yassir","Refendr","Chidera","Kemi","Vaasa","GKE","BigQuery","FIN-FSA"]  # Assumption #1

[clean]                     # OQ-3/OQ-5: parsed-and-ignored only; NO behavior in v1
enabled = false
```

New frozen dataclasses in `src/murmur/server/config.py` (separate from `murmur/config.py` to avoid coupling Mac-app config to server config): `ServerSettings`, `ServerSttSettings`, `ServerConfig`, and `load_server_config(path: Path | None) -> tuple[ServerConfig, list[str]]`. The `[clean]` table is parsed into a `dict` and never read (Non-Goal #1, OQ-5).

### D. Bearer-token retrieval (server; mirrors `mac/keychain.py` — R-7, E-18)

```python
def get_server_token(env: Mapping[str, str],
                     run: Callable[..., CompletedProcess[str]] = subprocess.run) -> str | None:
    # 1. env["MURMUR_SERVER_TOKEN"]  (Assumption #2)
    # 2. security find-generic-password -s murmur-server-token -w
    # returns None if neither present
```
Startup calls this once; if `None` → fail to start with the exact message in E-18: `"Bearer token not configured. Set MURMUR_SERVER_TOKEN env var or add to Keychain."`. Comparison uses `hmac.compare_digest` (AC-15). Token never logged (AC-17, R-7): a logging filter / explicit non-logging of the header.

### E. launchd plist `com.jay.murmur-server.plist` (AC-19, R-16)

New module `src/murmur/server/launchd_server.py` mirroring `mac/launchd.py` (`render_server_plist`, `install_server_agent`, `uninstall_server_agent`), label `com.jay.murmur-server`. Keys: `KeepAlive=true` (AC-19, R-16 — differs from the Mac app's `false`), `RunAtLoad=true`, `ProgramArguments = [caffeinate, -i, <python>, -m, murmur.server, …]` (AC-19), `StandardOutPath`/`StandardErrorPath` → `~/Library/Logs/murmur-server.log`. Validated by `plutil -lint` + key/value assertions (AC-19). Single-instance: launchd-only (OQ-8) plus a belt `SingleInstanceLock` reusing existing `src/murmur/lock.py` on `/tmp/murmur-server.pid` lockfile path (R-17).

### F. Android `TranscriptionEngine` Kotlin interface (Assumption #10, AC-42)

```kotlin
// engine/TranscriptionEngine.kt
data class TranscriptionOutcome(val text: String, val engine: String)   // engine: "mac" | "openai"

sealed interface EngineResult {
    data class Success(val outcome: TranscriptionOutcome) : EngineResult
    data class RetryableFailure(val reason: String) : EngineResult        // → AC-44 backoff
    data class PermanentFailure(val errorMessage: String) : EngineResult  // → status=failed
    data object NotApplicable : EngineResult                              // engine declined (e.g. Mac model_loaded:false, fallback disabled)
}

interface TranscriptionEngine {
    val name: String                                                     // "mac" | "openai"
    suspend fun isAvailable(): Boolean                                   // Mac: GET /health 200 & model_loaded; OpenAI: key present & fallback on
    suspend fun transcribe(audioPath: String, vocabHints: List<String>, language: String): EngineResult
}
```
`MacTranscriptionEngine` and `OpenAITranscriptionEngine` both implement this; `TranscriptionDispatcher` depends only on the interface + an ordered `List<TranscriptionEngine>` (AC-42). HTTP timeouts: health 2500 ms exactly (R-3, AC-34, AC-36).

### G. Room `notes` entity + DAO (Assumption #11)

```kotlin
@Entity(tableName = "notes")
data class NoteEntity(
    @PrimaryKey val id: String,            // UUID
    val createdAt: Long,                   // epoch ms (AC-30, AC-47 desc order)
    val durationSec: Double,               // > 0 (AC-30)
    val audioPath: String?,                // app-private file path; null after keep_audio=false delete
    val rawText: String?,                  // non-null iff done (AC-32)
    val engine: String?,                   // "mac"|"openai", non-null iff done (AC-32)
    val status: String,                    // "pending"|"transcribing"|"done"|"failed"
    val errorMessage: String?,             // non-null iff failed (AC-32)
    val attemptCount: Int = 0              // for AC-44 / retry-reset (Assumption #12/13)
)

@Dao interface NoteDao {
    @Query("SELECT * FROM notes ORDER BY createdAt DESC") fun observeAll(): Flow<List<NoteEntity>>     // AC-47
    @Query("SELECT * FROM notes WHERE id = :id") suspend fun byId(id: String): NoteEntity?
    @Insert suspend fun insert(n: NoteEntity)                                                          // AC-30
    @Update suspend fun update(n: NoteEntity)
    @Query("DELETE FROM notes WHERE id = :id") suspend fun delete(id: String)                          // AC-51
    @Query("UPDATE notes SET status='pending' WHERE status='transcribing'") suspend fun resetStale(): Int  // AC-33, E-34
}
```
Status is a `String` (validated by a `NoteStatus` enum + `require`d transition guard in the state-machine helper — AC-31). DB built with `.fallbackToDestructiveMigration()` acceptable in v1 (single user, no shipped schema yet).

**State machine (AC-31, AC-32) — only these transitions valid:**
`pending → transcribing` (worker starts) · `transcribing → done` (success) · `transcribing → pending` (retryable) · `transcribing → failed` (non-retryable / max retries) · `failed → pending` (user Retry, attemptCount→0). Invariants enforced in `OutboxStateMachine.transition(...)`: `done`⇒rawText≠null, engine≠null, errorMessage=null; `failed`⇒errorMessage≠null, rawText=null.

### H. WorkManager worker contract (AC-43..AC-45, AC-44 backoff)

`TranscribeWorker : CoroutineWorker`. Enqueued per-note as **unique work** `name = "transcribe-<noteId>"`, `ExistingWorkPolicy.KEEP` (AC-45: one worker per note). Constraint `NetworkType.CONNECTED` (AC-43, E-28). `BackoffPolicy.EXPONENTIAL`, initial 30 s; WorkManager caps internally at 5 h, so the worker enforces the 5-min cap + 10-attempt limit itself via `attemptCount` (AC-44, Assumption #12): on the 11th would-be attempt → `failed` / `"Max retries exceeded"`. `doWork()`: load note → if `audioPath` missing/empty → `failed` (E-40) → set `transcribing` → run dispatcher → map `EngineResult` to state transition + `Result.success/retry/failure`.

### I. Dispatcher engine-selection decision table (AC-34..AC-40, E-21..E-27, E-40)

| mac_url | /health result | fallback on? | openai key? | Action | engine / status |
|---------|----------------|--------------|-------------|--------|-----------------|
| set | 200 model_loaded:true | — | — | POST {mac}/transcribe | success → `done` engine=`mac` (AC-34) |
| set | 200 model_loaded:false | yes | yes | skip Mac POST → OpenAI | `done` engine=`openai` (AC-35) |
| set | 200 model_loaded:false | no/none | — | leave pending | `pending` (AC-35→AC-38) |
| set | timeout(>2500ms)/5xx | yes | yes | OpenAI | `done` engine=`openai` (AC-36, E-21,E-22,E-23) |
| set | timeout/5xx | no/none | — | leave pending, retry | `pending` (AC-38, E-26,E-27) |
| set | **401** | — | — | mark failed, NO fallback | `failed` "Auth failed — check token in Settings" (AC-39) |
| empty | (skip health) | yes | yes | OpenAI | `done` engine=`openai` (AC-37) |
| empty | (skip health) | no/none | — | leave pending | `pending` (AC-37→AC-38) |
| — | (mac POST 200 then) OpenAI **401** | — | — | mark failed | `failed` "OpenAI auth failed — check API key in Settings" (AC-40, E-25) |
| — | OpenAI 429/5xx/timeout | — | — | retryable | `pending`, backoff (AC-44, E-24) |
| any | audio file 0 bytes | — | — | mark failed pre-dispatch | `failed` "Audio file is empty — nothing to transcribe." (E-40) |

URL normalization: strip trailing slash (E-38); reject non-HTTPS in Settings with inline warning, block request (E-39).

### Configuration summary

| Where | Key | Default | Read by |
|-------|-----|---------|---------|
| `server.toml [server]` | host/port/max_upload_mb/busy_wait_s/log_path | 127.0.0.1 / 8080 / 25 / 0 / ~/Library/Logs/murmur-server.log | `load_server_config` |
| `server.toml [stt]` | model/language/vocab_hints | large-v3 / en / PRD §5.5 list | `load_server_config` |
| env / Keychain | `MURMUR_SERVER_TOKEN` / `murmur-server-token` | none (required, E-18) | `get_server_token` |
| Android EncryptedSharedPrefs | mac_url, bearer_token, openai_key, fallback_enabled, keep_audio, vocab_hints | "", "", "", **true** (OQ-2), false, [] | `SettingsRepository` |

## Task Breakdown

> Each task is independently committable. **Repo** column: **A** = this repo (`src/murmur/server/`, branch `feature/murmur-mobile-2026-06-14`); **B** = new repo `~/Documents/MurmurMobile`. Order = dependency order, riskiest-first within a milestone. Server (M0) fully precedes mobile because the Android dispatcher is built to the server's API.

| # | Repo | Task | Satisfies | Done when | Proved by (tests — exact names) |
|---|------|------|-----------|-----------|--------------------------------|
| **M0 — Component A (Murmur Server) — this repo** |
| T-1 | A | Server config + token retrieval: `src/murmur/server/config.py` (`load_server_config`, `[clean]` parsed-ignored) + `src/murmur/server/token.py` (`get_server_token`, hmac compare helper). | AC-15, AC-16, AC-17(token-source), E-15, E-16, E-17, E-18, OQ-5 | `load_server_config` never raises; missing/malformed→defaults+warn; token resolves env→Keychain; constant-time compare exists | `tests/unit/test_server_config.py::test_missing_file_defaults`, `::test_malformed_defaults`, `::test_unknown_key_warns`, `::test_clean_section_parsed_not_acted`; `tests/unit/test_server_token.py::test_env_first`, `::test_keychain_fallback`, `::test_none_when_absent`, `::test_compare_uses_compare_digest` |
| T-2 | A | ffmpeg decode helper `src/murmur/server/audio.py`: `decode_to_pcm(path) -> np.ndarray` via ffmpeg subprocess → float32 mono 16 kHz; raises `AudioDecodeError` on non-zero/timeout/missing-binary. Subprocess injected for tests. | AC-5, AC-11, E-4, E-19 | returns float32 16 kHz array on success (mocked); raises on ffmpeg failure/missing | `tests/unit/test_server_audio.py::test_decode_returns_float32_16k`, `::test_ffmpeg_nonzero_raises`, `::test_ffmpeg_timeout_raises`, `::test_ffmpeg_missing_binary_raises` |
| T-3 | A | FastAPI app skeleton `src/murmur/server/app.py` + `__main__.py`: app factory with injected `Transcriber` + readiness flag + `asyncio.Lock`; `GET /health` (no lock, <500ms, 401 on bad token). | AC-1, AC-2, AC-3, AC-13, R-1, E-11 | `/health` returns exact JSON; 401 on missing/bad token; 200 model_loaded reflects flag; passes while lock held | `tests/unit/test_server_health.py::test_health_ok_shape`, `::test_health_401_missing_token`, `::test_health_401_wrong_token`, `::test_health_model_loaded_false_when_initializing`, `::test_health_fast_while_lock_held` |
| T-4 | A | `POST /transcribe` happy path + transcriber/prompt/guard wiring: auth → validate → ffmpeg(mocked) → `FakeTranscriber` → `build_initial_prompt` → `clean_result` → 200 JSON. | AC-4, AC-5, AC-6, AC-7, E-7, E-8, E-9 | 200 body has all keys, engine="mac", duration_sec>0; transcriber called with float32/16k + initial_prompt; default vocab when absent | `tests/unit/test_server_transcribe.py::test_transcribe_200_shape`, `::test_passes_float32_16k_to_transcriber`, `::test_initial_prompt_from_vocab_hints`, `::test_default_vocab_when_absent`, `::test_language_defaults_en`, `::test_no_speech_returns_422` |
| T-5 | A | `POST /transcribe` validation + error paths: auth-first, missing/empty file, size>limit, unsupported type, decode-fail, transcription-fail, temp-file cleanup (success+error). | AC-8, AC-9, AC-10, AC-11, AC-12, AC-18, E-1..E-6, E-42, E-43, E-19, E-20 | each error returns the contracted status + error_code; no ffmpeg/temp before auth/size; temp deleted in all paths | `tests/unit/test_server_transcribe_errors.py::test_401_before_ffmpeg`, `::test_413_at_25mb_plus_one`, `::test_25mb_exactly_accepted`, `::test_415_unsupported_type`, `::test_octet_stream_m4a_accepted`, `::test_missing_file_400`, `::test_empty_file_400`, `::test_decode_failed_422`, `::test_transcription_failed_422`, `::test_temp_deleted_on_success`, `::test_temp_deleted_on_error`, `::test_503_model_not_loaded` |
| T-6 | A | Single-flight lock: `asyncio.Lock`; concurrent transcribe→503 model_busy (0s wait); concurrent health stays fast. | AC-13, AC-14, E-10, E-11 | second concurrent transcribe → 503 model_busy immediately; health <500ms during transcribe | `tests/unit/test_server_singleflight.py::test_concurrent_transcribe_503_model_busy`, `::test_health_not_blocked_by_lock` |
| T-7 | A | Token-not-logged + audio/transcript-not-logged guarantees: logging filter / explicit redaction; startup fails clearly when token absent. | AC-17, R-7, R-10, E-18 | no log record contains token after a request; startup error message exact | `tests/unit/test_server_logging.py::test_token_never_logged`, `::test_audio_text_never_logged`, `::test_startup_fails_without_token` |
| T-8 | A | launchd plist + single-instance belt: `src/murmur/server/launchd_server.py` (KeepAlive=true, caffeinate -i, correct interpreter/entrypoint) + reuse `lock.py` on `/tmp/murmur-server.pid`. | AC-19, R-16, R-17, OQ-8 | rendered plist passes `plutil -lint`; KeepAlive=true, caffeinate present, RunAtLoad=true, correct args | `tests/unit/test_server_launchd.py::test_plist_lints`, `::test_keepalive_true`, `::test_caffeinate_present`, `::test_program_args_point_to_entrypoint`; `tests/integration/test_server_single_instance.py::test_second_instance_blocked` |
| T-9 | A | Server README + deploy notes (install plist, `tailscale serve`, env/Keychain token, ffmpeg install) and `[hardware]`-marked live smoke test stub. Confirms `127.0.0.1` bind (R-8). | AC-20(setup), R-8, R-9, R-18 | docs runnable; `pytest -m hardware` stub skips cleanly when ffmpeg/tailscale absent | `tests/integration/test_server_live.py::test_live_health_and_transcribe` (marked `hardware`, deselected by default) |
| **M1 — Component B (Murmur Mobile core) — new repo `~/Documents/MurmurMobile`** |
| T-10 | B | Scaffold project from proven skeleton: `git init`; copy/pin Gradle 8.9 wrapper, AGP 8.7.3, Kotlin 2.0.21, Compose BOM 2024.10.01, compileSdk 35/minSdk 31, build-tools 35.0.0; `settings.gradle.kts` (pluginManagement + version catalog), `local.properties` `sdk.dir`, local XML theme; JaCoCo wired. Verify `JAVA_HOME=$(/usr/libexec/java_home -v 21) ./gradlew --no-daemon assembleDebug testDebugUnitTest` green. | R-5, R-6, R-13 | clean build + a placeholder unit test pass headlessly on JDK 21, no emulator | `com.murmur.mobile.BuildSmokeTest#toolchain_builds_and_runs_unit_test` |
| T-11 | B | Settings + secrets: `SettingsRepository` over `EncryptedSharedPreferences`; persist mac_url/token/openai_key/fallback(default ON)/keep_audio/vocab_hints; URL normalize (E-38) + HTTPS-only validate (E-39); secrets never logged. | AC-21, AC-22, R-11, OQ-2, E-38, E-39 | values survive process death; non-HTTPS blocked; trailing slash stripped; no key in Logcat | `SettingsRepositoryTest#persists_and_restores_after_process_death` (robolectric), `#strips_trailing_slash`, `#rejects_non_https`, `SecretsLoggingTest#key_never_in_logcat` (unit) |
| T-12 | B | Room `notes` entity + DAO + `OutboxStateMachine` (transition guard + invariants) + stale-reset. | AC-30, AC-31, AC-32, AC-33, AC-41(hints source), E-34, E-41 | only valid transitions allowed; done/failed invariants enforced; `resetStale` flips transcribing→pending | `OutboxStateMachineTest#valid_transitions_only`, `#done_requires_rawtext_engine`, `#failed_requires_error`, `NoteDaoTest#reset_stale_transcribing_to_pending` (robolectric), `#distinct_ids_rapid_insert` |
| T-13 | B | Recording: `RecordingService` FGS type=microphone, persistent notification (Pause/Resume/Stop), MediaRecorder AAC/m4a mono 16 kHz; Stop writes file + inserts Room row atomically; permission rationale + POST_NOTIFICATIONS-denied warning; 25-min cap; audio-focus-loss + storage-full handling. | AC-24, AC-25, AC-26, AC-27, AC-28, AC-29, AC-30, AC-46, E-29, E-30, E-31, E-32, E-33 | FGS up <2s; m4a 16 kHz mono; atomic save (rollback on disk-full); focus-loss finalizes to pending; 25-min auto-stop+warning | `RecordingServiceTest#starts_fgs_microphone_with_actions` (robolectric), `#continues_when_backgrounded`, `#m4a_16k_mono` , `RecordingPolicyTest#caps_at_25_min` (unit), `#audio_focus_loss_finalizes_pending`, `#disk_full_rolls_back` (unit), `PermissionTest#denied_shows_rationale_no_crash`, `#post_notifications_denied_warns_still_records` (robolectric) |
| T-14 | B | Note list + detail UI (Compose): Home list ordered desc with status badge + preview; detail with raw_text/status/engine tag + Copy/Share/Retry(if failed)/Delete. | AC-47, AC-48, AC-49, AC-50, AC-51, E-35, E-36, E-37 | list renders rows; Copy→clipboard==raw_text; Share→ACTION_SEND text/plain EXTRA_TEXT==raw_text; Delete removes row+audio; Retry rules | `NoteListTest#orders_desc_with_badges_and_preview` (robolectric), `NoteDetailTest#copy_sets_clipboard`, `#share_emits_action_send`, `DeleteTest#removes_row_and_audio` (unit), `#missing_audio_warns_not_crash`, `RetryTest#failed_with_audio_requeues`, `#failed_without_audio_disabled` |
| **M2 — Component B (resilience / fallback) — new repo** |
| T-15 | B | `TranscriptionEngine` interface + `MacTranscriptionEngine` (HTTP via OkHttp; health 2500ms; multipart to server contract §A) + `OpenAITranscriptionEngine` (contract §B). | AC-34, AC-36, AC-39, AC-40, AC-41, AC-42, E-21, E-22, E-23, E-24, E-25 | both implement interface, substitutable; Mac maps server error_codes; OpenAI maps 401/429; vocab hints sent both ways | `EngineInterfaceTest#both_engines_substitutable`, `MacEngineTest#health_timeout_2500ms_unreachable`, `#mac_401_permanent_no_fallback`, `#sends_vocab_hints_field`, `OpenAiEngineTest#openai_401_permanent`, `#openai_429_retryable`, `#sends_prompt` (all unit) |
| T-16 | B | `TranscriptionDispatcher` implementing decision table §I (Mac-first, health-gated, fallback per OQ-2). | AC-34, AC-35, AC-36, AC-37, AC-38, AC-39, AC-40, E-21..E-27, E-40 | every row of the §I table produces the specified engine/status | `DispatcherTest#mac_loaded_uses_mac`, `#mac_model_not_loaded_falls_through`, `#mac_unreachable_uses_openai`, `#empty_mac_url_skips_health`, `#fallback_disabled_stays_pending`, `#mac_401_failed_no_fallback`, `#openai_401_failed`, `#empty_audio_failed` (all unit) |
| T-17 | B | `TranscribeWorker` (CoroutineWorker) + WorkManager wiring: network constraint, unique-per-note KEEP, exp backoff 30s→5m cap/10 attempts, stale-reset on startup. | AC-33, AC-43, AC-44, AC-45, E-26, E-27, E-28, E-34 | no dispatch without network; one worker/note; 11th attempt→failed "Max retries exceeded"; startup resets stale | `WorkerTest#no_dispatch_without_network`, `#unique_work_per_note`, `#backoff_params_30s_5m_10`, `#max_retries_marks_failed`, `#startup_resets_stale_transcribing` (all unit/robolectric via WorkManager test harness) |
| T-18 | B | keep_audio retention wiring: keep_audio=false deletes audio on `done`; true retains + playable; Delete cleanup; Retry reset. | AC-52, AC-53, E-35, E-36, E-37 | audio deleted on done when keep_audio false; retained+exists when true | `RetentionTest#deletes_audio_on_done_when_keep_false`, `#retains_audio_when_keep_true` (unit) |
| T-19 | B | Manual verification runbook `docs/manual-verification.md` (Component-B equivalent): numbered MV steps for every `[human-on-device]` AC, each with exact pass condition + setup (Tailscale URL, Pixel, live Mac). | AC-20, AC-54, AC-55, AC-56, AC-57, AC-58, AC-59, AC-60, AC-61 | one MV-step per device AC, unambiguous pass condition; references server §A contract | runbook step IDs MV-20, MV-54, MV-55, MV-56, MV-57, MV-58, MV-59, MV-60, MV-61 (these ARE the "named proving test" for the device ACs) |
| T-20 | B | Component-B README + JaCoCo coverage gate ≥80% + final headless green (`assembleDebug` + `testDebugUnitTest` + coverage report). | R-13, R-15 | full unit/robolectric suite green headlessly; JaCoCo new-code ≥80% | full suite run; `jacocoTestReport` ≥ 80% |

**AC coverage check:** AC-1…AC-61 each appear in ≥1 task above. Mapping by ID: AC-1/2/3→T-3; AC-4/5/6/7→T-4 (AC-5 also T-2); AC-8/9/10/11/12/18→T-5 (AC-11 also T-2); AC-13/14→T-6 (AC-13 also T-3); AC-15/16/17→T-1 (AC-17 also T-7); AC-19→T-8; AC-20→T-9(setup)+T-19(MV-20); AC-21/22→T-11; AC-23→T-15/T-11 (Test-connection wired in T-15 engine + T-11 settings — see note); AC-24/25/26/27/28/29/30→T-13 (AC-30 also T-12, AC-41 hints-source T-12); AC-31/32/33→T-12 (AC-33 also T-17); AC-34..AC-40→T-16 (AC-34/36/39/40/41 also T-15); AC-42→T-15; AC-43/44/45→T-17; AC-46→T-13; AC-47/48/49/50/51→T-14; AC-52/53→T-18; AC-54..AC-61→T-19. **All 61 ACs mapped.**

> **Note on AC-23 (Test-connection):** the `GET /health`-with-status-mapping logic lives in `MacTranscriptionEngine.checkHealth()` (T-15); the Settings button + four result strings live in T-11's Settings UI. AC-23's proving test `SettingsTest#test_connection_maps_health_states` is added in T-15 (after the engine exists). Listed under T-15 in the test-strategy table.

## Test Strategy

**Levels.** Component A: pytest unit tests using `FastAPI TestClient` + `FakeTranscriber` (from `tests/fakes.py`) + a mocked ffmpeg subprocess (monkeypatched `subprocess.run`/the injected runner) — no ffmpeg/tailscale/model weights (R-14). Single-flight concurrency tested with `httpx.AsyncClient` + `asyncio` against the ASGI app. Live end-to-end is one `hardware`-marked test, deselected by default. Run: `uv run pytest` (server tests live in `tests/unit/test_server_*.py` + `tests/integration/test_server_*.py`, matching existing layout); coverage via existing `--cov` (R-12, `fail_under=80`); `uv run ruff check`, `uv run mypy src/murmur/server`.

Component B: **JVM unit** tests (`junit4` + Truth + mockk + kotlinx-coroutines-test + okhttp `MockWebServer` for engine HTTP) for dispatcher/engines/outbox/worker/retention/recording-policy; **Robolectric** for Android-framework surfaces (EncryptedSharedPreferences, Room, Service lifecycle, Compose UI semantics, Clipboard/Intent). All run via `JAVA_HOME=$(/usr/libexec/java_home -v 21) ./gradlew --no-daemon testDebugUnitTest`. Coverage via JaCoCo `jacocoTestReport` (R-13, ≥80%). **Device** ACs are the manual runbook (T-19) — un-automatable (R-15).

| AC | Test name(s) | Level |
|----|--------------|-------|
| AC-1 | test_server_health::test_health_ok_shape | unit (pytest) |
| AC-2 | test_server_health::test_health_401_missing_token, ::test_health_401_wrong_token | unit |
| AC-3 | test_server_health::test_health_model_loaded_false_when_initializing | unit |
| AC-4 | test_server_transcribe::test_transcribe_200_shape | unit |
| AC-5 | test_server_audio::test_decode_returns_float32_16k; test_server_transcribe::test_passes_float32_16k_to_transcriber | unit |
| AC-6 | test_server_transcribe::test_initial_prompt_from_vocab_hints, ::test_default_vocab_when_absent | unit |
| AC-7 | test_server_transcribe::test_no_speech_returns_422 | unit |
| AC-8 | test_server_transcribe_errors::test_413_at_25mb_plus_one, ::test_25mb_exactly_accepted | unit |
| AC-9 | test_server_transcribe_errors::test_401_before_ffmpeg | unit |
| AC-10 | test_server_transcribe_errors::test_415_unsupported_type, ::test_octet_stream_m4a_accepted | unit |
| AC-11 | test_server_audio::test_ffmpeg_nonzero_raises; test_server_transcribe_errors::test_decode_failed_422 | unit |
| AC-12 | test_server_transcribe_errors::test_transcription_failed_422 | unit |
| AC-13 | test_server_singleflight::test_health_not_blocked_by_lock | unit (async) |
| AC-14 | test_server_singleflight::test_concurrent_transcribe_503_model_busy | unit (async) |
| AC-15 | test_server_token::test_compare_uses_compare_digest | unit |
| AC-16 | test_server_config::test_missing_file_defaults, ::test_malformed_defaults | unit |
| AC-17 | test_server_logging::test_token_never_logged | unit |
| AC-18 | test_server_transcribe_errors::test_temp_deleted_on_success, ::test_temp_deleted_on_error | unit |
| AC-19 | test_server_launchd::test_plist_lints, ::test_keepalive_true, ::test_caffeinate_present, ::test_program_args_point_to_entrypoint | unit |
| AC-20 | MV-20 (manual runbook, T-19) | human-on-device |
| AC-21 | SettingsRepositoryTest#persists_and_restores_after_process_death | robolectric |
| AC-22 | SecretsLoggingTest#key_never_in_logcat | unit |
| AC-23 | SettingsTest#test_connection_maps_health_states | unit |
| AC-24 | RecordingServiceTest#starts_fgs_microphone_with_actions | robolectric |
| AC-25 | RecordingServiceTest#continues_when_backgrounded | robolectric |
| AC-26 | RecordingServiceTest#m4a_16k_mono | robolectric |
| AC-27 | RecordingPolicyTest#audio_focus_loss_finalizes_pending | unit |
| AC-28 | PermissionTest#denied_shows_rationale_no_crash | robolectric |
| AC-29 | PermissionTest#post_notifications_denied_warns_still_records | robolectric |
| AC-30 | RecordingServiceTest#m4a_16k_mono (file+row atomic); OutboxStateMachineTest (row fields) | robolectric+unit |
| AC-31 | OutboxStateMachineTest#valid_transitions_only | unit |
| AC-32 | OutboxStateMachineTest#done_requires_rawtext_engine, #failed_requires_error | unit |
| AC-33 | NoteDaoTest#reset_stale_transcribing_to_pending; WorkerTest#startup_resets_stale_transcribing | robolectric+unit |
| AC-34 | DispatcherTest#mac_loaded_uses_mac; MacEngineTest | unit |
| AC-35 | DispatcherTest#mac_model_not_loaded_falls_through | unit |
| AC-36 | DispatcherTest#mac_unreachable_uses_openai; MacEngineTest#health_timeout_2500ms_unreachable | unit |
| AC-37 | DispatcherTest#empty_mac_url_skips_health | unit |
| AC-38 | DispatcherTest#fallback_disabled_stays_pending | unit |
| AC-39 | DispatcherTest#mac_401_failed_no_fallback; MacEngineTest#mac_401_permanent_no_fallback | unit |
| AC-40 | DispatcherTest#openai_401_failed; OpenAiEngineTest#openai_401_permanent | unit |
| AC-41 | MacEngineTest#sends_vocab_hints_field; OpenAiEngineTest#sends_prompt | unit |
| AC-42 | EngineInterfaceTest#both_engines_substitutable | unit |
| AC-43 | WorkerTest#no_dispatch_without_network | unit |
| AC-44 | WorkerTest#backoff_params_30s_5m_10, #max_retries_marks_failed | unit |
| AC-45 | WorkerTest#unique_work_per_note | unit |
| AC-46 | RecordingPolicyTest#caps_at_25_min | unit |
| AC-47 | NoteListTest#orders_desc_with_badges_and_preview | robolectric |
| AC-48 | NoteDetailTest (renders fields+buttons) | robolectric |
| AC-49 | NoteDetailTest#copy_sets_clipboard | robolectric |
| AC-50 | NoteDetailTest#share_emits_action_send | robolectric |
| AC-51 | DeleteTest#removes_row_and_audio, #missing_audio_warns_not_crash | unit |
| AC-52 | RetentionTest#deletes_audio_on_done_when_keep_false | unit |
| AC-53 | RetentionTest#retains_audio_when_keep_true | unit |
| AC-54 | MV-54 (manual runbook, T-19) | human-on-device |
| AC-55 | MV-55 (manual runbook, T-19) | human-on-device |
| AC-56 | MV-56 (manual runbook, T-19) | human-on-device |
| AC-57 | MV-57 (manual runbook, T-19) | human-on-device |
| AC-58 | MV-58 (manual runbook, T-19) | human-on-device |
| AC-59 | MV-59 (manual runbook, T-19) | human-on-device |
| AC-60 | MV-60 (manual runbook, T-19) | human-on-device |
| AC-61 | MV-61 (manual runbook, T-19) | human-on-device |

**Coverage-check line:** all 61 ACs above have a named proving test or numbered runbook step; the 9 `[human-on-device]` ACs (AC-20, AC-54..AC-61) map to runbook steps MV-20, MV-54, MV-55, MV-56, MV-57, MV-58, MV-59, MV-60, MV-61, whose step IDs are the named proving artifact per the gate's intent.

## New Dependencies

**Component A (server) — added to `[project.dependencies]` / `[dependency-groups].dev`:**

| Package | Why | Why this one | License | Maintenance |
|---------|-----|--------------|---------|-------------|
| `fastapi>=0.115` | HTTP framework for `/health` + `/transcribe`; async lock fits naturally | Repo has no web framework; FastAPI is the de-facto async standard, first-class multipart + dependency injection (auth) | MIT | Active, very widely used |
| `uvicorn[standard]>=0.30` | ASGI server to run FastAPI under launchd | Standard FastAPI pairing | BSD | Active |
| `python-multipart>=0.0.9` | Required by FastAPI to parse `multipart/form-data` uploads | Mandatory transitive for `UploadFile` | Apache-2.0 | Active |
| `httpx>=0.27` (dev) | Async test client for single-flight concurrency tests (AC-13/14) | FastAPI's recommended async test client; already a Starlette test dep | BSD | Active |

> `numpy` already present. ffmpeg is a **runtime binary** (Homebrew), not a Python dep — invoked as subprocess, mocked in tests (R-14). No Anthropic/`/clean` deps (OQ-3/OQ-5, Non-Goal #1).

**Component B (Android) — `gradle/libs.versions.toml`, versions confirmed compatible with the pinned toolchain (AGP 8.7.3 / Kotlin 2.0.21 / compileSdk 35 / minSdk 31):**

| Library | Version | Why | License |
|---------|---------|-----|---------|
| Compose BOM | 2024.10.01 | UI (proven in skeleton) | Apache-2.0 |
| `androidx.room` (runtime/ktx/compiler via KSP) | 2.6.1 | Outbox persistence (AC-30..AC-33) | Apache-2.0 |
| `com.google.devtools.ksp` | 2.0.21-1.0.28 | Room annotation processing; **KSP version must match Kotlin 2.0.21** | Apache-2.0 |
| `androidx.work:work-runtime-ktx` | 2.9.1 | Retry/backoff dispatcher (AC-43..AC-45); + `work-testing` for headless worker tests | Apache-2.0 |
| `androidx.security:security-crypto` | 1.1.0-alpha06 | `EncryptedSharedPreferences` (AC-21/22) — alpha is the only line supporting API 31+ reliably; see Risks | Apache-2.0 |
| `com.squareup.okhttp3:okhttp` + `mockwebserver` | 4.12.0 | HTTP client for both engines + multipart; MockWebServer for headless engine tests | Apache-2.0 |
| `org.jetbrains.kotlinx:kotlinx-coroutines-android` + `-test` | 1.8.1 | CoroutineWorker + suspend engines; `-test` for deterministic dispatcher tests | Apache-2.0 |
| `org.robolectric:robolectric` | 4.13 | Headless Android-framework tests (R-15); 4.13 supports SDK 35 | Apache-2.0/MIT |
| `junit:junit` | 4.13.2 | Test runner (proven) | EPL-1.0 |
| `com.google.truth:truth` | 1.4.2 | Readable assertions | Apache-2.0 |
| `io.mockk:mockk` | 1.13.12 | Kotlin mocking (engines/dao in dispatcher tests) | Apache-2.0 |
| `androidx.lifecycle` (service/viewmodel) | 2.8.7 | FGS + Compose state (proven line) | Apache-2.0 |

> Retrofit deliberately **not** added: OkHttp alone covers both the small server API and OpenAI multipart; fewer moving parts (ADR-5). KSP is preferred over kapt (faster, Kotlin-2.0-native). `security-crypto` alpha pin is a known risk — see Risks + ADR-7.

## Risks

| Risk | Likelihood | Impact | Mitigation (or "accepted") |
|------|------------|--------|------------------------------|
| `foregroundServiceType="microphone"` + `FOREGROUND_SERVICE_MICROPHONE` behavior differs API 31 vs 34+ (OQ-1) | Med | High (recording is the core feature) | Declare both `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_MICROPHONE` perms and the manifest type; gate `startForeground(type=…)` by SDK; Robolectric asserts the service+type (AC-24); real behavior confirmed on-device (MV-57/58). OQ-1 default pins to API 34 behavior. |
| ffmpeg not installed at runtime (E-19) silently breaks transcribe | Med | High | T-2 explicitly maps missing-binary → `audio_decode_failed` 422 + log; `/health` stays 200 (server still "up"); README install step; tested `test_ffmpeg_missing_binary_raises`. |
| Single-flight `asyncio.Lock` + sync mlx-whisper call blocks the event loop, stalling `/health` (violates R-1/AC-13) | Med | High | Run the blocking `transcribe()` in a threadpool (`run_in_executor` / `anyio.to_thread`) so the event loop stays free for `/health`; lock guards entry, not the loop. Tested by `test_health_not_blocked_by_lock` with a slow FakeTranscriber. **This is the subtlest server bug** — flagged for the reviewer. |
| `EncryptedSharedPreferences` (security-crypto) is in alpha and has known Keystore edge cases on some API 31 devices | Med | Med | Pin `1.1.0-alpha06`; wrap reads/writes with a try/catch that surfaces a Settings error rather than crashing; Robolectric covers the happy path; on-device MV step confirms persistence. Accepted residual risk: AndroidX has shipped no stable line; this is the standard choice. |
| Room insert + audio-file write atomicity (AC-30, E-32, E-33) — no real cross-resource transaction | Med | Med | Order: write file first → on success insert row; on insert failure delete the file; on file failure never insert (compensating action in a single suspend fn). Tested `#disk_full_rolls_back`. Accepted: not a true 2-phase commit, but the compensating-action pattern guarantees no orphan row/file. |
| Device ACs (AC-20, AC-54..61) inherently un-automatable (R-15) | High | Med | Converted to a precise manual runbook (T-19) with unambiguous pass conditions; orchestrator + human accept these are human-verified. Surfaced at checkpoint. |
| Disk pressure during Android build (~8 GB free, Gradle caches ~1.3 GB, Robolectric/Room/AGP add more) | Med | Med | Build `--no-daemon`; reuse `~/.gradle` caches from the toolchain proof; `brew cleanup` already reclaimed space; monitor before T-10. If <2 GB free → stop-and-ask (operating rules). |
| Two-repo API drift (server contract changes, Android not updated) | Med | Med | Single authoritative HTTP contract (§A) in this plan; the server's error-code registry is the source of truth; MockWebServer fixtures in Android engine tests encode §A exactly; MV-54 exercises the real contract end-to-end. ADR-8. |
| OQ answers differ from defaults → re-plan | Low–Med | varies | Each OQ-contingent task tagged below; a different answer is a *bounded* re-plan, not a redesign. |

## Plan Notes for Reviewer

- **Plan is built against all 8 OQ recommended defaults.** Contingencies if the human answers differently at the checkpoint:
  - **OQ-2 (fallback default-on)** — the single most structural privacy decision. Default ON ⇒ `SettingsRepository.fallbackEnabled` defaults `true` (T-11) and the dispatcher's empty-mac-url + unreachable rows fall to OpenAI. If Jay wants **default OFF**: change one default in T-11 and flip the expected engine/status in `DispatcherTest#empty_mac_url_skips_health` and `#mac_unreachable_uses_openai` (→ stays pending). Bounded: ~1 file + 2 tests. No structural change.
  - **OQ-6 (server package location)** — drives every server import path. Default `src/murmur/server/` ⇒ all T-1..T-9 land there and import `murmur.transcribe.*` directly. If `src/murmur_server/` (sibling package): a second `pyproject.toml` install target + cross-package import; re-plan T-1/T-3/T-9 packaging. Most structural server decision — flagged.
  - OQ-1 → T-13 FGS type-declaration + MV setup (pinned API 34). OQ-3/OQ-5 → `[clean]` parsed-ignored only (T-1); enabling `/clean` is net-new scope. OQ-4 → 25-min cap (T-13/AC-46). OQ-7 → `model_not_loaded` 503 (T-5). OQ-8 → launchd + belt lock (T-8).
- **AC-23 split across T-11 (UI strings) and T-15 (health-state mapping)** — its proving test lands in T-15. Called out so the reviewer doesn't read it as an unmapped AC.
- **Server version bump to `1.0.0`** (Assumption #3, ADR-1) — the `/health` `version` field. If the human prefers keeping `0.1.0`, change one constant; AC-1 asserts `<semver>` shape, not a specific value.
- **Scope held to spec.** No `/clean`, no chunking, no search (Non-Goals #1–3). The `[clean]` TOML table is parsed and ignored only.
- **Nothing the implementer must invent:** every interface (HTTP §A, engine §F, Room §G, worker §H, decision table §I) is specified. The one judgment call left to implementation is the threadpool mechanism for the blocking transcribe call (flagged in Risks).
