# ADRs: Murmur Mobile — Server + Android

**Run ID:** murmur-mobile-2026-06-14 · **Status:** proposed → accepted at checkpoint · **Date:** 2026-06-14

These are the decisions that genuinely shaped the build. Boilerplate (which assert library, which JSON encoder) is omitted.

---

## ADR-1: Server lives at `src/murmur/server/` inside the existing package (OQ-6)

### Context
Component A is a FastAPI service that *must reuse* `src/murmur/transcribe/` (`MlxWhisperTranscriber`, `build_initial_prompt`, `clean_result`, `ModelManager`) without re-implementing any of it (Non-Goal #11). It also wants the existing test harness (`tests/fakes.py::FakeTranscriber`, `conftest.py`, pytest markers, `pytest-cov fail_under=80`, ruff, strict mypy). The question is whether the server is a new sub-package of `murmur` or a separate sibling package `murmur_server`.

### Decision
A new sub-package **`src/murmur/server/`** in this repo, on branch `feature/murmur-mobile-2026-06-14`. Server tests live alongside existing tests as `tests/unit/test_server_*.py` and `tests/integration/test_server_*.py`. Entry point: `python -m murmur.server`.

### Options Considered
| Option | Pros | Cons |
|--------|------|------|
| A (chosen) `src/murmur/server/` sub-package | Direct `from murmur.transcribe import …`; one install target, one `pyproject.toml`, one coverage/lint/mypy config; reuses fakes + conftest verbatim; smallest diff | Couples server release to the Mac-app package version (acceptable — same author, same repo) |
| B `src/murmur_server/` sibling package | Independent versioning/install | Second `pyproject.toml` + build target; cross-package import gymnastics; duplicate test config; the macOS-app deps (`rumps`, `pynput`, PyObjC) would still be dragged in or need careful extras splitting |
| C separate repo | Clean separation | Loses the entire reuse + test-harness advantage that is the whole point of Component A |

### Consequences
Easier: imports, testing, lint/type config — all inherited. The server's `version` field reports the package version; we bump `pyproject.toml` to **`1.0.0`** so `/health` reports a real release (Assumption #3). Harder: the server now pulls the package's heavyweight deps at install time (mlx-whisper, torch) — acceptable because the server *needs* mlx-whisper anyway and runs on the same Mac. Revisit if the server ever needs to deploy somewhere the Mac-app deps can't go.

---

## ADR-2: Async single-flight via `asyncio.Lock`, blocking transcribe offloaded to a thread, `/health` lock-free

### Context
FastAPI is async. The model is single-instance and not concurrency-safe, so only one transcription may run at a time (AC-14: second concurrent `POST` → immediate 503 `model_busy`). Simultaneously, `/health` must answer < 500 ms *even while a transcription is in progress* (AC-1, AC-3, AC-13, R-1, E-11). The hazard: `MlxWhisperTranscriber.transcribe()` is a synchronous, CPU/Metal-bound call that can run for seconds — if awaited directly on the event loop it blocks *everything*, including `/health`, regardless of the lock.

### Decision
A module-level `asyncio.Lock`. `POST /transcribe` does `if lock.locked(): return 503 model_busy` then `async with lock:` and runs the blocking `transcribe()` via `anyio.to_thread.run_sync` / `run_in_executor` so the event loop stays free. `GET /health` reads a plain `bool` readiness flag and **never references the lock**. The `0 s` busy-wait (Assumption #5) means contention is an immediate 503, not a queue.

### Options Considered
| Option | Pros | Cons |
|--------|------|------|
| A (chosen) `asyncio.Lock` + `to_thread` for the blocking call, lock-free `/health` | Meets AC-13/14 exactly; event loop never blocks; health is trivially fast; matches "single-flight, no waiters" language | Must remember to offload (the subtle bug — flagged in plan Risks) |
| B `threading.Lock` + sync endpoints (run FastAPI sync) | Simpler mental model | Loses async health concurrency; sync endpoints run in a bounded threadpool that the long transcribe can starve → health stalls |
| C Semaphore/queue with waiters | "Fair" queueing | Spec explicitly wants no queued waiters (Assumption #4, E-10); unbounded wait risk; more code |

### Consequences
The `to_thread` offload is mandatory, not optional — without it AC-13 fails despite the lock being correct. The plan flags this as the single subtlest server defect and gives it a dedicated test (`test_health_not_blocked_by_lock` with a slow `FakeTranscriber`). `model_busy` retries are handled client-side by WorkManager backoff, so an immediate 503 is safe. Revisit only if v2 wants request queueing.

### Addendum (R3-1 post-escalation fix, 2026-06-14)

The initial implementation had a TOCTOU race: `if lock.locked()` and `async with lock:` were separated by real `await file.read()` calls, which are genuine event-loop yield points. Two concurrent requests could both pass the busy-check, both yield at the file reads, and both queue at the acquire — causing both to return 200 (breaking AC-14).

The as-built design replaces `async with lock:` with an explicit `if lock.locked(): return 503` / `await lock.acquire()` pair with **no await expression between them**. In CPython's asyncio, `Lock.acquire()` on an uncontested lock sets `_locked=True` and returns synchronously without suspending — the event loop does not yield between the check and the acquire. Once the acquire returns, the lock is held for the duration of the upload read, ffmpeg decode, and transcription. A concurrent request seeing `lock.locked()==True` returns 503 `model_busy` before reading any upload bytes, closing the TOCTOU window. The lock is released unconditionally in a `finally` block.

This design is appropriate for Murmur's single-user, Tailscale-only deployment: the additional lock coverage during the upload read has no practical cost (only one transcription is ever in progress). Verified by `test_concurrent_transcribe_race_proof__R3_1_AC14` in `tests/unit/test_server_singleflight.py`, which injects a forced event-loop yield at the file-read point to prove the race cannot occur.

---

## ADR-3: ffmpeg as a subprocess, not a Python audio library

### Context
`MlxWhisperTranscriber.transcribe` takes a **float32 mono 16 kHz numpy array, not a file** (confirmed in `transcribe/local.py`). The phone uploads m4a/aac (and the API accepts wav/mp3/ogg/opus). Something must decode arbitrary containers to PCM. Options: shell out to `ffmpeg`, or use a Python decoder (`pydub`+ffmpeg, `soundfile`/libsndfile, `av`/PyAV, `librosa`).

### Decision
Invoke **`ffmpeg` as a subprocess** (`-i <in> -ac 1 -ar 16000 -f f32le -`) and read float32 PCM from stdout. The subprocess runner is injected so tests mock it (R-14). Missing binary / non-zero exit / timeout → `audio_decode_failed` 422 (AC-11, E-4, E-19).

### Options Considered
| Option | Pros | Cons |
|--------|------|------|
| A (chosen) ffmpeg subprocess | Handles every container the spec lists incl. m4a/aac natively; trivially mockable (R-14); no new Python C-extension deps; matches the spec's explicit AC-5 ("invokes ffmpeg as a subprocess") | Runtime dependency on the `ffmpeg` binary (mitigated: README + graceful 422 + `/health` stays up) |
| B PyAV / `av` | In-process, no subprocess | Heavy C-extension build; AC-5 explicitly mandates an ffmpeg subprocess call captured in tests; harder to mock |
| C `soundfile`/libsndfile | Simple | No m4a/aac support (the phone's primary format) — disqualifying |

### Consequences
AC-5 is satisfied literally (tests capture the ffmpeg argv and the transcriber's array dtype/rate). The cost is the runtime binary dependency, handled by E-19's graceful degradation: server starts and `/health` is 200 even without ffmpeg; only `/transcribe` fails, with a clear error-code and log. Revisit if we ever need to drop the binary dependency for distribution.

---

## ADR-4: Component B is a separate sibling repo, not a monorepo addition

### Context
Component B is a Kotlin/Gradle/Android app; Component A's repo is a Python/uv project. They share a network contract (§A of the plan) but zero code. State.md records the human's decision that the Android app lives in `~/Documents/MurmurMobile`.

### Decision
A **new git repo** `~/Documents/MurmurMobile`, `git init`'d by the implementer, scaffolded from the proven `/tmp/android-toolchain-check` skeleton with the exact pinned toolchain. Run artifacts (spec/plan/state) remain in this repo's `runs/` dir (cross-repo run).

### Options Considered
| Option | Pros | Cons |
|--------|------|------|
| A (chosen) separate repo | No Gradle/uv/CI cross-contamination; clean Android-native tooling; matches human's recorded decision; independent versioning | Two repos to keep the §A contract in sync (mitigated by ADR-8) |
| B monorepo subdir | One place | Python and Android build systems, lint, CI, and `.gitignore` collide; coverage tools (`pytest-cov` vs JaCoCo) can't share config; awkward |

### Consequences
Two repos, one authoritative contract. The implementer must `git init` and produce a PR-ready branch in *each* repo. The §A HTTP contract is the only coupling and is pinned in the plan + encoded in Android MockWebServer fixtures (ADR-8). Revisit only if the project ever wants unified CI.

---

## ADR-5: OkHttp (not Retrofit/Ktor) as the single Android HTTP client

### Context
Android needs two HTTP clients: the Mac server (`GET /health`, multipart `POST /transcribe` per §A) and OpenAI (multipart `POST /v1/audio/transcriptions` per §B). Choices: Retrofit (on OkHttp), raw OkHttp, or Ktor client.

### Decision
**Raw OkHttp 4.12.0** with `MultipartBody` for uploads and an explicit 2500 ms call timeout on the health check (R-3). Tests use OkHttp's `MockWebServer` to encode the §A/§B contracts.

### Options Considered
| Option | Pros | Cons |
|--------|------|------|
| A (chosen) OkHttp directly | Two tiny endpoints don't justify an interface layer; full control over multipart + per-call timeouts; MockWebServer gives faithful headless tests; one dep | Slightly more boilerplate than annotated interfaces |
| B Retrofit | Declarative interfaces | Extra dep + converter for two endpoints; multipart still needs OkHttp types; over-engineered here |
| C Ktor client | Coroutine-native, multiplatform | New engine dep; team/skeleton already proven with the AndroidX/OkHttp ecosystem; no multiplatform need |

### Consequences
Minimal dependency surface; precise timeout control (critical for the 2500 ms health gate, AC-34/AC-36). MockWebServer makes engine tests deterministic and headless (R-15). If the API surface grows past a handful of endpoints in v2, reconsider Retrofit.

---

## ADR-6: Room + WorkManager outbox — durable state machine drives all dispatch

### Context
Capture must work fully offline and never lose a recording (Goal); transcription completes automatically once an engine is reachable (AC-56). This demands durable local state surviving process death (AC-33), connectivity-gated dispatch (AC-43), and bounded retry (AC-44) with no duplicate workers (AC-45).

### Decision
A **Room `notes` table is the single source of truth** (§G); **WorkManager** is the only thing that drives transcription. Recording writes `pending`; a unique-per-note `CoroutineWorker` (KEEP policy) with a `CONNECTED` constraint and exponential backoff (30 s→5 min cap, 10 attempts) advances the state machine (§H). Stale `transcribing` rows reset to `pending` at startup (AC-33, E-34).

### Options Considered
| Option | Pros | Cons |
|--------|------|------|
| A (chosen) Room + WorkManager outbox | OS-managed durability + connectivity + backoff for free; survives process death/reboot; unique work = no dup dispatch; idiomatic | Two AndroidX deps; WorkManager's internal 5 h backoff cap means the 5-min cap is enforced in-worker |
| B Foreground service does dispatch inline | Simpler control flow | No durability across kill; must hand-roll connectivity + backoff; service lifetime ≠ retry lifetime |
| C Plain DB + custom scheduler | Full control | Re-implements WorkManager's constraints/backoff; more code, more bugs |

### Consequences
The Room row is the contract between recording, UI, and dispatch — UI observes it via `Flow` (AC-47). WorkManager handles the hard parts (connectivity, backoff, persistence). The 5-min cap and 10-attempt limit are enforced via `attemptCount` in `doWork()` because WorkManager won't cap that low natively (plan §H). Atomic file+row creation uses a compensating-action pattern, not a real transaction (plan Risks). Revisit if v2 needs cross-device sync.

---

## ADR-7: Secrets in EncryptedSharedPreferences (server: env→Keychain)

### Context
Android stores a Mac bearer token and an OpenAI key; both must never hit Logcat and travel only over HTTPS (R-10, R-11, AC-22). The server's bearer token must never be in config/logs/source — env var or Keychain only (R-7, E-18).

### Decision
**Android:** `EncryptedSharedPreferences` (`androidx.security:security-crypto 1.1.0-alpha06`) backed by the Android Keystore for all secrets and Settings values. **Server:** `get_server_token` reads `MURMUR_SERVER_TOKEN` env first, then `security find-generic-password -s murmur-server-token -w` — mirroring the existing `mac/keychain.py::get_openai_api_key` pattern exactly. Bearer comparison uses `hmac.compare_digest` (AC-15).

### Options Considered
| Option | Pros | Cons |
|--------|------|------|
| A (chosen) EncryptedSharedPreferences / Keystore (Android) + env→Keychain (server) | Hardware-backed where available; matches repo's existing Keychain precedent; meets R-7/R-10/R-11 directly | `security-crypto` has no stable release line — must pin alpha (risk noted) |
| B Plain SharedPreferences + manual crypto | No alpha dep | Hand-rolled crypto is a footgun; worse than the AndroidX alpha |
| C DataStore (no encryption) | Modern API | No at-rest encryption — fails R-11 |

### Consequences
Secrets are encrypted at rest and never logged (a logging redaction filter + never passing tokens to log calls enforce AC-17/AC-22). The `security-crypto` alpha pin is the accepted residual risk (ADR/plan Risks): it's the only AndroidX option and is widely used despite the alpha label. The server reuses an already-tested retrieval pattern, lowering risk. Revisit Android storage if AndroidX ships a stable replacement (e.g., encrypted DataStore).

---

## ADR-8: One authoritative HTTP contract keeps two repos in sync

### Context
Server (repo A) and client (repo B) share only the §A HTTP API. With two repos there is real risk of drift: a server error-code rename or field change silently breaks the Android dispatcher's `when(status, error_code)` mapping.

### Decision
The plan's **§A (HTTP API) + error-code registry is the single source of truth**, authored once. The server's tests assert it produces exactly those codes/shapes; the Android engine tests use `MockWebServer` fixtures that **replay §A verbatim** (same status codes, same `error_code` strings, same JSON keys). The live MV-54/MV-55 runbook steps exercise the real contract end-to-end on-device.

### Options Considered
| Option | Pros | Cons |
|--------|------|------|
| A (chosen) prose contract + mirrored fixtures both sides + live MV check | Zero new tooling; both repos test against the identical shape; drift caught by a failing fixture or MV step | Contract is prose, not a machine-checked schema — discipline required |
| B OpenAPI spec generated from FastAPI + codegen Android client | Machine-checked | Heavy: codegen toolchain in the Android build; overkill for two endpoints; cross-repo codegen sync is its own problem |
| C Shared schema package | Single definition | No natural shared-package mechanism across Python+Kotlin repos |

### Consequences
The error-code registry (`unauthorized`, `model_busy`, `model_not_loaded`, `missing_file`, `empty_file`, `file_too_large`, `unsupported_media_type`, `audio_decode_failed`, `transcription_failed`, `no_speech_detected`) is frozen and referenced by both repos. Any future change must update §A, both sides' tests, and the MV steps together. If the API grows materially in v2, graduate to option B (OpenAPI + codegen).
