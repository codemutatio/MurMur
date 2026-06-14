# Code Review — murmur-mobile-2026-06-14
**Reviewer:** code-reviewer · **Iteration:** 2 (re-validation after fix-list-1) · **Date:** 2026-06-14

---

## Code Review

### Re-validation of CR-1..CR-8, CR-10, CR-13 (F-1..F-8, minor CR-10, CR-13)

#### CR-1 / F-1 — TranscribeWorker.enqueue() wiring — **CLOSED**

`RecordingService.saveNoteAtomically()` (`RecordingService.kt:262`) calls `TranscribeWorker.enqueue(this, note.id)` immediately after `dao.insert(note)`. `NoteViewModel.retryNote()` (`NoteViewModel.kt:119`) calls `TranscribeWorker.enqueue(getApplication(), updated.id)` after `dao.update(updated)`. Both production callsites confirmed. The proving tests `enqueue_after_recording_creates_work_item__F1_AC45` and `double_enqueue_keeps_single_work_item__F1_AC45` verify the WorkManager state after calling enqueue, which is the correct proxy. The test does not drive through `saveNoteAtomically` end-to-end (the comment on line 56 acknowledges this) — this is acceptable given the Robolectric `saveNoteAtomically` path cannot use an in-memory Room DB (it uses the global singleton). Code-level inspection confirms the enqueue call is at the correct location.

#### CR-2 / F-2 — resetStaleAndRequeue wiring and Flow bug — **CLOSED**

`MainActivity.onCreate()` (`MainActivity.kt:27-29`) launches `startupScope.launch { TranscribeWorker.resetStaleAndRequeue(applicationContext) }` on `Dispatchers.IO`. The `resetStaleAndRequeue` companion function (`TranscribeWorker.kt:116`) now uses `dao.observeAll().first()` (not `.collect { return@collect }`). The `resetStaleAndRequeue_resets_and_reenqueues__F2_AC33_E34` test exercises the reset and enqueue chain. The `resetStaleAndRequeue_completes_without_hanging__F2` test proves the Flow terminates by using `runTest` which has a default coroutine timeout. Both findings are closed.

#### CR-3 / F-3 — TOCTOU single-flight lock — **CLOSED with notation**

The fix reorders all `await` file-I/O operations (empty check, size read, MIME check) to run BEFORE the `lock.locked()` check. Between `if lock.locked(): return 503` and `async with lock:` at `app.py:256-260` there are now zero `await` expressions. Bytecode-level analysis confirms that `asyncio.Lock.__aenter__` invokes `acquire()`, whose fast path (`not self._locked`) sets `_locked=True` and returns `True` synchronously (no `await fut`) within the same event loop tick. This means, in asyncio's single-threaded cooperative concurrency model, `lock.locked() == False` and `lock._locked = True` happen within the same scheduling quantum with no opportunity for another coroutine to interleave. The concurrent test `test_concurrent_transcribe_503_model_busy` confirms this empirically.

**Notation (not a new finding — flagged for security-reviewer awareness):** The fix reads the entire upload into memory (`bytearray(body)`, `app.py:213-226`) before the single-flight busy-check. A caller sending a 25 MB payload while the server is busy will have 25 MB buffered in the event loop before receiving the 503. With high concurrency of large uploads this could become a memory-amplification vector. This is within the accepted design (loopback-only, single trusted client) but is noted for the security reviewer per the fix-list instruction.

**Validation order deviation (new finding NV-1 below):** The reordering swaps spec steps 2 and 3 — see NV-1.

#### CR-4 / F-4 — Backoff cap at 300 s — **CLOSED with minor residual (NV-2 below)**

`computeBackoffSeconds()` (`TranscribeWorker.kt:94-96`) correctly enforces `min(30 * 2^n, 300)`. The retry path in `doWork()` at line 220-221 uses `computeBackoffSeconds(pending.attemptCount)` and re-enqueues via `enqueueWithDelay()`, then returns `Result.success()` to prevent WorkManager adding its own uncapped delay. The proving tests confirm capping at attempts 4, 5, 10, 30. The cap IS now wired. See NV-2 for a minor off-by-one in the backoff sequence start.

#### CR-5 / F-5 — JSON parser — **CLOSED**

Both `MacTranscriptionEngine.parseJsonStringField()` (`MacTranscriptionEngine.kt:214-219`) and `OpenAITranscriptionEngine.extractTextField()` (`OpenAITranscriptionEngine.kt:151-154`) now use `org.json.JSONObject(...).optString(key, null as String?)`. All engine test classes are annotated `@RunWith(RobolectricTestRunner::class)` so `org.json` (an Android platform class) is available. `JsonParserTest` confirms escaped-quote and backslash handling at both the direct-parser level and via `MockWebServer` end-to-end.

#### CR-6 / F-6 — deleteNote file-first ordering — **CLOSED**

`NoteViewModel.deleteNote()` (`NoteViewModel.kt:66-77`) now deletes the audio file first: `file.delete()` (with a logged warning if it returns false), then `dao.delete(note.id)`. A file-delete failure logs `"Could not delete audio file: $path (E-35) — deleting DB row anyway"` and proceeds with the row deletion. The proving tests cover the happy path, the file-already-gone path, and the null-audioPath path.

#### CR-7 / F-7 — Dispatcher health-gating through interface — **CLOSED (partial)**

`TranscriptionEngine` interface now declares `checkAvailabilityState(): EngineAvailability` with a default implementation wrapping `isAvailable()` (`TranscriptionEngine.kt:54-55`). `MacTranscriptionEngine` overrides it with the three-way `HealthState` distinction (`MacTranscriptionEngine.kt:72-79`). `TranscriptionDispatcher` calls `macEngine.checkAvailabilityState()` (`TranscriptionDispatcher.kt:58`) without casting. `EngineInterfaceTest` uses a `mockk<MacTranscriptionEngine>()` with only `checkAvailabilityState()` stubbed (no `checkHealth()` stub), proving the dispatcher never calls `checkHealth()` directly.

**Residual deviation (logged, not a new blocker):** The plan §F contract states the dispatcher should depend on "the interface and an ordered `List<TranscriptionEngine>`." `TranscriptionDispatcher` still takes `MacTranscriptionEngine?` as a concrete parameter type (`TranscriptionDispatcher.kt:29-30`) and `OpenAITranscriptionEngine` as a concrete type (line 30). This means a caller cannot substitute a different implementation for `macEngine` or `openAiEngine` without a cast — a pure `TranscriptionEngine` mock typed as `MacTranscriptionEngine?` would require an unchecked cast. The `EngineInterfaceTest` works around this by mocking `MacTranscriptionEngine` (the concrete class, not the interface). AC-42 says "both implementations can be substituted for the interface without modification to the dispatcher" — the dispatch logic itself is now interface-driven, but the constructor signature still couples to concrete types. This is a silent plan deviation (§F) that was not flagged in implementation-notes. Flagged as major finding **NV-3** since the original CR-7 was major for this exact coupling concern and the root cause (concrete constructor parameter) remains.

#### CR-8 / F-8 — Single DB write for done transition — **CLOSED**

`TranscribeWorker.doWork()` at lines 197-204 now folds the done-and-nullify into a single `dao.update()` call: `TranscribeWorkerLogic.toDone(...).copy(audioPath = null)` (keep_audio=false) or `TranscribeWorkerLogic.toDone(...)` (keep_audio=true). The intermediate state with `status=done` and non-null `audioPath` pointing at a deleted file no longer exists. Proving tests confirm both paths.

#### CR-10 / minor — Token redaction on getMessage() — **CLOSED**

`_TokenRedactFilter.filter()` (`app.py:95-98`) now calls `record.getMessage()` first to expand format args, stores the replaced string in `record.msg`, and clears `record.args = ()`. The test `test_token_redact_filter_on_format_string` verifies the end-to-end redaction on a format-string log record.

#### CR-13 / minor — Fallback warning for unencrypted settings — **CLOSED**

`SettingsRepository.create()` sets `encryptionFallback = true` on catch and passes it to the constructor as `isEncryptionFallback`. The `SettingsRepositoryTest` proves the flag is correct in both the success and fallback paths. Callers are responsible for showing the user warning (the fix does not add a UI component, which is acceptable — the flag is exposed and documented). This is at the spec's edge (AC-22 requires secrets in EncryptedSharedPreferences; the fallback still violates R-11 in theory, but the flag exposes the condition to the caller).

---

### New Defects Introduced by the Fixes

| # | Sev | Location | Finding | Suggested Action |
|---|-----|----------|---------|-----------------|
| NV-1 | **MAJOR** | `MurMur/src/murmur/server/app.py:198-260` | **F-3 reordering silently inverts spec steps 2 and 3: model_not_loaded fires BEFORE model_busy.** The plan §A validation order is explicit: step 2 = single-flight check → 503 `model_busy`; step 3 = model-not-loaded check → 503 `model_not_loaded`. The F-3 fix moved the single-flight check to after all file I/O (after steps 3–7 in the original numbering). The result: a concurrent request that arrives while a transcription is in progress AND the model flag is unset now gets `503 model_not_loaded` instead of `503 model_busy`. The contract says a busy-server response (`model_busy`) must fire at step 2, before the model check at step 3. This is a silent spec deviation (implementation-notes do not flag this reordering). The deviation is also not covered by any test — no test checks that `model_busy` wins over `model_not_loaded` when both conditions hold simultaneously. In practice this scenario (busy + not loaded) is unlikely in production (model load completes before any transcription could hold the lock), but the spec contract is inverted. | Flag the deviation in implementation-notes. Add a test that verifies the concurrent-request path returns `model_busy` (not `model_not_loaded`) when both the lock is held and the model flag is unset. Alternatively, reorder the code to check `lock.locked()` immediately after auth and return 503 early, then re-check after I/O (accept that two requests can both get model_not_loaded if both race after the empty lock check, since model_not_loaded requests don't involve the lock at all). The simplest correct approach: `if lock.locked(): return 503 model_busy` immediately after auth, then proceed with all other checks. A request that already sees the lock held has no need to read the file at all. |
| NV-2 | **MINOR** | `MurmurMobile/.../worker/TranscribeWorker.kt:220` | **F-4: backoff sequence starts at 60 s, not 30 s as AC-44 specifies.** `computeBackoffSeconds(attemptCount=0)=30s` is correct, but after the first failure `pending.attemptCount` is 1 (incremented by `preflight` and stored via `toTranscribing`). The first retry is therefore `computeBackoffSeconds(1)=60s`. The spec (AC-44, Assumption #12) says "starting at 30 seconds." The test `backoff_attempt_0_is_initial__F4_AC44` asserts `computeBackoffSeconds(0)==30` but the wired retry path uses `computeBackoffSeconds(pending.attemptCount)` where `pending.attemptCount` is at least 1. Practical impact is minor (first retry is 60s instead of 30s, still capped at 300s), but it is a spec deviation. | In `doWork()`, use `computeBackoffSeconds(pending.attemptCount - 1)` for the retry delay, so that the first retry uses `computeBackoffSeconds(0)=30s`. Alternatively, document this as an intentional deviation: the first dispatch is immediate (0s delay), so the 30s start applies to the delay for attempt #2. Update the proving test to assert the wired delay at `pending.attemptCount=1` is 30s. |
| NV-3 | **MAJOR** | `MurmurMobile/.../dispatch/TranscriptionDispatcher.kt:29-30` | **F-7 is partially fixed but the plan §F substitutability contract is still violated: `TranscriptionDispatcher` takes `MacTranscriptionEngine?` and `OpenAITranscriptionEngine` as concrete constructor types, not `TranscriptionEngine`.** The dispatch logic now correctly calls `checkAvailabilityState()` through the interface, but the constructor binds to concrete classes, meaning the dispatcher cannot accept any other `TranscriptionEngine` implementation without an unsafe cast. `EngineInterfaceTest` works around this by mocking `MacTranscriptionEngine` (the concrete class), not a substitute `TranscriptionEngine` — so the test does NOT actually prove "a substitute TranscriptionEngine drives the dispatcher's health gate without a concrete-type cast" as the fix-list requires. The test proves the dispatcher does not call `checkHealth()` directly, but it does so while keeping the concrete constructor type. AC-42 says "both implementations can be substituted for the interface without modification to the dispatcher"; the dispatcher's constructor signature still requires modification to accept a different Mac implementation. This is a silent plan deviation (§F) that was not flagged in implementation-notes. | Change the dispatcher constructor to `(macEngine: TranscriptionEngine?, openAiEngine: TranscriptionEngine)` and update the `EngineInterfaceTest` to construct the dispatcher with a `mockk<TranscriptionEngine>()` (interface mock, not `MacTranscriptionEngine` concrete mock). The call to `macEngine.checkAvailabilityState()` works through the interface's default implementation for engines that don't override it. |
| NV-4 | **MINOR** | `MurmurMobile/.../worker/ValidationFixesTest.kt:56-93` | **F-1 proving tests do not test the actual production trigger path through `RecordingService.saveNoteAtomically` or `NoteViewModel.retryNote`.** The tests call `TranscribeWorker.enqueue()` directly. The comment on line 56 acknowledges this limitation. If `saveNoteAtomically` were to accidentally remove the enqueue call in a future edit, these tests would not catch it. The recording-stop → enqueue chain (the most critical F-1 path) has no Robolectric-level integration test that exercises `saveNoteAtomically` and then verifies WorkManager state. | Add a Robolectric test that starts `RecordingService`, writes a non-empty file, sends ACTION_STOP, and asserts that WorkManager has a work item scheduled for the inserted note's ID. This test would catch any regression in the production trigger path. |

---

### Summary of Fix-list Items

| Fix | Status | Evidence |
|-----|--------|---------|
| F-1 (CR-1) | CLOSED | `RecordingService.kt:262`, `NoteViewModel.kt:119`; tests `enqueue_after_recording_creates_work_item__F1_AC45`, `double_enqueue_keeps_single_work_item__F1_AC45` |
| F-2 (CR-2) | CLOSED | `MainActivity.kt:27-29`, `TranscribeWorker.kt:116`; tests `resetStaleAndRequeue_resets_and_reenqueues__F2_AC33_E34`, `resetStaleAndRequeue_completes_without_hanging__F2` |
| F-3 (CR-3) | CLOSED (notation on step-order deviation → NV-1) | `app.py:256-260`; `test_concurrent_transcribe_503_model_busy`; asyncio single-thread analysis confirms atomicity |
| F-4 (CR-4) | CLOSED (minor off-by-one → NV-2) | `TranscribeWorker.kt:94-96,220-221`; `backoff_capped_at_300s_for_attempt_4_and_beyond__F4_AC44` |
| F-5 (CR-5) | CLOSED | `MacTranscriptionEngine.kt:214-219`, `OpenAITranscriptionEngine.kt:151-154`; `JsonParserTest` 5 tests |
| F-6 (CR-6) | CLOSED | `NoteViewModel.kt:66-77`; `delete_happy_path_removes_file_then_row__F6_AC51`, `delete_file_failure_still_removes_db_row__F6_E35` |
| F-7 (CR-7) | CLOSED (residual plan §F deviation on constructor type → NV-3) | `TranscriptionEngine.kt:54-55`, `MacTranscriptionEngine.kt:72-79`, `TranscriptionDispatcher.kt:58`; `EngineInterfaceTest` 3 tests |
| F-8 (CR-8) | CLOSED | `TranscribeWorker.kt:197-204`; `done_with_keep_audio_false_single_db_write__F8_AC52` |
| CR-10 (minor) | CLOSED | `app.py:92-98`; `test_token_redact_filter_on_format_string` |
| CR-13 (minor) | CLOSED | `SettingsRepository.kt:47-67`; `encryption_fallback_flag_true_when_fallback_occurred__CR13` |

---

### What was checked in this iteration

- Read full diff: `git show 70da6e4` (server) and `git show 380666d` + `b0c4d09` + `412eb45` (Android)
- Read all changed production files in their entirety: `app.py`, `__main__.py`, `TranscribeWorker.kt`, `TranscribeWorkerLogic.kt`, `RecordingService.kt`, `NoteViewModel.kt`, `MacTranscriptionEngine.kt`, `OpenAITranscriptionEngine.kt`, `TranscriptionEngine.kt`, `TranscriptionDispatcher.kt`, `SettingsRepository.kt`, `MainActivity.kt`
- Read all proving test files: `test_server_singleflight.py`, `test_validation_fixes.py`, `ValidationFixesTest.kt`, `EngineInterfaceTest.kt`, `JsonParserTest.kt`, `RecordingPolicyTest.kt`, `SettingsRepositoryTest.kt`
- Traced the F-3 atomicity claim by disassembling the CPython bytecode for `async with lock:` and running empirical concurrency tests to confirm the asyncio single-thread fast-path behavior
- Verified the F-7 EngineInterfaceTest actually mocks `MacTranscriptionEngine` (concrete class) not `TranscriptionEngine` (interface) — the residual plan deviation
- Cross-checked validation step order (app.py steps 2/3 inversion) against spec §A and plan §A

---

**Verdict: FAIL**

Blockers: 0 | New Majors: 2 (NV-1, NV-3) | New Minors: 2 (NV-2, NV-4)

The single worst new issue is **NV-1**: the F-3 fix reorders validation steps 2 and 3, silently inverting the spec's required ordering (model_busy must fire before model_not_loaded). While this scenario is rare in production, the contract is broken and untested, and the deviation is not flagged in implementation-notes.

**NV-3** is the second most serious: the `TranscriptionDispatcher` constructor still binds to concrete engine types, meaning AC-42's substitutability claim and plan §F's "depends only on the interface" contract are still not fully met. The `EngineInterfaceTest` proves that the dispatch logic routes through `checkAvailabilityState()` on the interface, but a truly substitute engine typed as `TranscriptionEngine` (not `MacTranscriptionEngine`) cannot be passed to the dispatcher without an unsafe cast — the original F-7 major finding's root cause (concrete coupling at the constructor) is unresolved.

All eight original blockers/majors (F-1 through F-8) are substantively closed. The two new majors are introduced by the fixes themselves and must be addressed before shipping.
