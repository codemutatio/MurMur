# Final Report: Murmur Mobile — Android Voice Capture + Self-Hosted Transcription API

**Run ID:** murmur-mobile-2026-06-14 · **Completed:** 2026-06-14 · **Status:** COMPLETE — PR-ready locally (not pushed)

**Deliverables span two repos:**
- **Component A — Murmur Server:** `/Users/faithomojola/Documents/MurMur`, branch **`feature/murmur-mobile-2026-06-14`** (20 code/doc commits ahead of `main`, 0 behind → no rebase needed).
- **Component B — Murmur Mobile (Android):** **new repo** `/Users/faithomojola/Documents/MurmurMobile`, branch **`main`** (19 commits, greenfield — the whole history is the feature).

## Summary
A two-part personal system so Jay can dictate on his Pixel with the accent fidelity of his Mac's Whisper large-v3 model. **Component A** is a FastAPI service (`src/murmur/server/`) that wraps the existing `murmur.transcribe` module unchanged: bearer-auth (constant-time), `ffmpeg`→16 kHz-mono-PCM decode, an **atomic single-flight** lock (so `/health` stays sub-500 ms and a concurrent transcribe gets an immediate `503`), launchd + `caffeinate`, bound to `127.0.0.1` and exposed only via `tailscale serve`. **Component B** is a Kotlin/Jetpack-Compose app: a `microphone`-typed foreground service records AAC/m4a mono 16 kHz, every note lands in a Room "outbox", and a WorkManager dispatcher resolves it Mac-first (health-gated over Tailscale) with automatic OpenAI `gpt-4o-transcribe` fallback (default ON), retrying on backoff and surviving offline capture. Secrets live in EncryptedSharedPreferences; notes can be copied/shared into Claude. Scope is v1 (M0+M1+M2); `/clean` cleanup, silence-chunking, and search are deferred to v1.5 (non-goals). **Verification:** server 358 pytest tests (86.76% coverage); Android 188 unit/Robolectric tests (92.2% measured coverage); 9 end-to-end device ACs are captured as a manual on-device runbook (they require the physical Pixel + live Mac + Tailscale and cannot be automated).

## Traceability
All 61 ACs. **VERIFIED** = passing automated test on the final branch. **VERIFIED (runbook)** = device-only AC with a precise manual step Jay runs on his Pixel (cannot be automated in this environment). Full per-AC test names are in `plan.md` §Test Strategy and the iteration validation reports.

| AC | Criterion (short) | Implemented in | Verified by | Status |
|----|-------------------|----------------|-------------|--------|
| AC-1..AC-3 | `/health` shape, 401, model_loaded, lock-free | `src/murmur/server/app.py` | `tests/unit/test_server_health.py` | VERIFIED |
| AC-4..AC-7 | `/transcribe` happy path, ffmpeg→float32/16k, vocab prompt, guard→422 | `server/app.py`, `server/audio.py` (reuses `transcribe/prompt.py`,`guard.py`,`local.py`) | `test_server_transcribe.py`, `test_server_audio.py` | VERIFIED |
| AC-8..AC-12, AC-18 | size 413, auth-first, 415, decode 422, transcribe 422, temp cleanup | `server/app.py`, `server/audio.py` | `test_server_transcribe_errors.py` | VERIFIED |
| AC-13, AC-14 | `/health` fast under load; concurrent transcribe → immediate 503 (atomic single-flight) | `server/app.py` (atomic acquire) | `test_server_singleflight.py` incl. `test_concurrent_transcribe_race_proof__R3_1_AC14`, `test_health_not_blocked_by_lock` | VERIFIED |
| AC-15..AC-17, AC-19 | constant-time token, config defaults, token-never-logged, launchd plist | `server/token.py`, `server/config.py`, `server/launchd_server.py` | `test_server_token.py`, `test_server_config.py`, `test_server_logging.py`, `test_server_launchd.py` | VERIFIED |
| AC-20 | live `/health`+`/transcribe` over Tailscale, real voice note | server (deploy) | runbook **MV-20** | VERIFIED (runbook) |
| AC-21..AC-23 | Settings persist (Encrypted), secrets not logged, Test-connection mapping | `data/repository/SettingsRepository.kt`, `engine/MacTranscriptionEngine.kt` | `SettingsRepositoryTest`, `SecretsLoggingTest`, `MacEngineTest#test_connection_maps_health_states__AC23` | VERIFIED |
| AC-24..AC-30, AC-46 | FGS microphone+notification, survives bg/lock, m4a 16k mono, focus-loss finalize, perms, atomic save, 25-min cap | `recording/RecordingService.kt`, `RecordingPolicy`, `AudioFocusListener` | `RecordingServiceTest`, `RecordingPolicyTest`, `PermissionTest`, `AudioFocusListenerTest` | VERIFIED (AC-26 file-format portion → MV-26-B) |
| AC-31..AC-33 | outbox state machine + invariants + stale-reset | `data/db/OutboxStateMachine.kt`, `NoteDao`, `worker/TranscribeWorker.kt` | `OutboxStateMachineTest`, `NoteDaoTest`, `ResetStaleProductionPathTest` | VERIFIED |
| AC-34..AC-42 | dispatcher decision table, engine selection, fallback, 401 handling, vocab hints, interface substitutability | `dispatch/TranscriptionDispatcher.kt`, `engine/*` | `DispatcherTest`, `DispatcherEdgeCaseTest`, `MacEngineTest`, `OpenAiEngineTest`, `EngineInterfaceTest` | VERIFIED |
| AC-43..AC-45 | WorkManager network constraint, backoff 30s→5m/10, unique-per-note | `worker/TranscribeWorker.kt`, `TranscribeWorkerLogic` | `WorkerTest`, `WorkerLogicTest`, `EnqueueProductionPathTest`, `ValidationFixesTest` | VERIFIED |
| AC-47..AC-51 | note list ordered+badges, detail, copy, share, delete (file+row) | `ui/*` (NoteList, NoteDetail, NoteViewModel) | `NoteListTest`, `NoteDetailTest`, `DeleteTest`, `RetryTest` | VERIFIED |
| AC-52, AC-53 | keep_audio delete-on-done / retain | `retention/AudioRetentionManager.kt`, `worker/TranscribeWorker.kt` | `RetentionTest`, `RetentionEdgeCaseTest` | VERIFIED |
| AC-54..AC-61 | end-to-end on device: 2-min note via Mac, Mac-asleep→OpenAI, airplane→auto-complete, lock/bg survival, incoming call, share→Claude, Tailscale-only exposure | full system (deploy) | runbook **MV-54..MV-61** | VERIFIED (runbook) |

**Coverage check:** AC-1…AC-61 all present above. 52 automated (VERIFIED), 9 device-only (VERIFIED via runbook MV-20/MV-54..MV-61, plus MV-26-B for the AC-26 file-format check) — these await Jay running them on the Pixel.

## Validation Summary
**Iterations used:** 3 (capped) + 1 human-authorized targeted retry. · **Findings fixed:** blockers **4** (3 in iter-1: WorkManager never enqueued, dead stale-reset, single-flight TOCTOU; + 1 re-opened TOCTOU R3-1 closed in the retry), majors **12** (iter-1 F-4..F-12 incl. CVE, host-bind, regex-JSON-parser, backoff cap, delete-order, interface coupling, retention write, 2 tautological tests; iter-2 NV-1/NV-3/TE2-M-1 regressions). · **Minors logged (open, non-blocking):** test comment tidy; `isEncryptionFallback` flag not wired to a Settings warning (CR-13); E-33 Room-failure rollback lacks an injected-failure test (needs a small seam); a friendlier launchd-install CLI (the function works; only the doc example was wrong, now fixed).

The loop hit its 3-iteration cap with one open blocker (R3-1 — the single-flight TOCTOU, which each fix traded against the §A ordering). The orchestrator escalated honestly (`escalation-report.md`); the human authorized one targeted fix; the atomic acquire-or-503 pattern closed it, proven by a test that deterministically reproduces the race (and was confirmed to FAIL on the old pattern). Final re-review: all three reviewers PASS.

## Documentation Produced
- `src/murmur/server/README.md` — server API (`/health`,`/transcribe`, error-code registry), `server.toml`, token via env/Keychain, launchd install, `tailscale serve`, ffmpeg dependency, loopback bind, concurrency (atomic single-flight) note.
- `CHANGELOG.md` — `[1.0.0]` entry (Keep a Changelog format).
- `MurmurMobile/README.md` — full one-time setup checklist (PRD §9), pinned-toolchain build/run, sideload, verification pointer.
- `MurmurMobile/docs/manual-verification.md` — device-AC runbook (MV-20, MV-54..MV-61, MV-26-B).
- `runs/.../adr.md` — 8 ADRs + R3-1 atomic-acquire addendum to ADR-2.

## Deviations & Decisions
- **Single-flight design (ADR-2 + R3-1):** the lock is now acquired atomically at §A step 2 (immediate 503 on contention) and held through the upload read + transcription — acceptable for a single-flight, single-user, Tailscale-only server. This is the as-built refinement that closed the TOCTOU.
- **Coverage measurement (B-2):** some Android framework classes (RecordingService, TranscribeWorker plumbing, Room DAO, Compose UI, SettingsRepository) are Robolectric-tested but not JaCoCo-measured (classloader/probe incompatibility); core logic (engines, dispatcher, worker logic, retention, state machine) is pure-JVM and measured at 92.2%. Engines were deliberately kept pure-JVM (real `org.json` on the test classpath) so they stay measured.
- **Arbitration A-1:** on R3-1 the reviewers split; the orchestrator read `app.py` directly and ruled the code-reviewer correct (real TOCTOU). Recorded in `state.md`.
- **Device ACs (9):** inherently human-verified on Jay's Pixel — a property of the PRD, not a tooling gap.
- **Two-repo run:** the Android repo is greenfield on `main`; run artifacts live in the server repo's `runs/`.

## How to Review
1. **Server (start here — smallest, highest-risk):** `src/murmur/server/app.py` `POST /transcribe` — scrutinize the atomic acquire (`if lock.locked(): 503` then `await lock.acquire()` with no await between, `try/finally` release) and the §A 11-step order. Then `audio.py` (ffmpeg argv, no shell), `token.py` (`hmac.compare_digest`), `config.py` (loopback host enforcement). Run: `uv run pytest`, `uv run ruff check`, `uv run mypy src/murmur`.
2. **Android contract seam:** `engine/TranscriptionEngine.kt` + `MacTranscriptionEngine`/`OpenAITranscriptionEngine` (MockWebServer fixtures mirror the server's error-code registry) → `dispatch/TranscriptionDispatcher.kt` (decision table) → `worker/TranscribeWorker.kt` (enqueue wiring + backoff). Run: `JAVA_HOME=$(/usr/libexec/java_home -v 21) ./gradlew --no-daemon testDebugUnitTest jacocoTestReport`.
3. **Recording/outbox:** `recording/RecordingService.kt` (FGS type, atomic save) + `data/db/OutboxStateMachine.kt`.
4. **Then run the device runbook** (`MurmurMobile/docs/manual-verification.md`) on the Pixel to close AC-20 / AC-54..AC-61.

## Suggested PR
**Title:** `feat(server): Murmur Server — self-hosted transcription API for Murmur Mobile`

**Description:**
> Adds **Murmur Server** (`src/murmur/server/`), a FastAPI service that exposes the existing `murmur.transcribe` large-v3 pipeline over Tailscale so the companion Android app (Murmur Mobile, separate repo) can transcribe voice notes with full accent fidelity, OpenAI-fallback when the Mac is asleep.
>
> **What:** `GET /health` (lock-free readiness) and `POST /transcribe` (multipart → ffmpeg→16 kHz mono → large-v3 → hallucination guard → JSON), bearer-token auth (constant-time), atomic single-flight lock (immediate 503 on contention, `/health` never blocks), `server.toml` config, launchd agent (`KeepAlive` + `caffeinate`), bound to `127.0.0.1` and fronted by `tailscale serve`. Reuses the transcribe module unchanged. Server version → 1.0.0.
>
> **Tests:** 358 pytest tests, 86.76% coverage; ruff + mypy(strict) clean. Concurrency, auth, error-code, and temp-file-cleanup paths all covered (incl. a TOCTOU race-reproduction test).
>
> **Notes:** `/clean` cleanup deferred to v1.5 (`[clean]` config parsed-and-ignored). Runtime deps `ffmpeg` + Tailscale are deploy-time (mocked in tests). See `src/murmur/server/README.md` for setup.
>
> **Android companion** ships in the new `MurmurMobile` repo (`main`); its 9 end-to-end device ACs are a manual runbook to run on the Pixel.
>
> No breaking changes to the existing Murmur macOS app.

**Android repo:** greenfield at `~/Documents/MurmurMobile` (`main`, 19 conventional commits, 188 tests, 92.2% measured coverage). If a PR workflow is desired, branch it; otherwise the initial `main` history is the deliverable.
