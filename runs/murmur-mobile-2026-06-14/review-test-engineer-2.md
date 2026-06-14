# Test Engineering Review — Iteration 2

**Run ID:** murmur-mobile-2026-06-14
**Reviewer role:** test-engineer (re-validation)
**Date:** 2026-06-14
**Prior review:** review-test-engineer-1.md
**Fix list verified:** fix-list-1.md

---

## Suite Results

### Component A — Murmur Server (pytest)

| Metric | Value |
|--------|-------|
| Tests run | 354 passed |
| Deselected | 1 (`hardware` marker) |
| Expected failures | 1 (`xfail` — launchctl GUI) |
| Failures | 0 |
| Coverage (total) | 86.74% |
| New server module coverage | app.py: 99%, audio.py: 100%, config.py: 89%, launchd_server.py: 100%, token.py: 100% |

**Suite status: GREEN**

### Component B — Murmur Mobile (Gradle + Robolectric)

| Metric | Value |
|--------|-------|
| Tests run | 187 passed (181 pre-TE-2 + 6 new TE-2 tests) |
| Failures | 0 |
| Errors | 0 |

**Suite status: GREEN**

### JaCoCo Per-Package Coverage (iteration-2 actual numbers)

| Package | Lines Covered | Coverage | Gate (≥80%) |
|---------|--------------|----------|-------------|
| `engine` (Mac + OpenAI) | 27/161 | **16.8%** | **FAIL** |
| `dispatch` (TranscriptionDispatcher) | 36/38 | 94.7% | PASS |
| `worker` (TranscribeWorkerLogic) | 6/6 | 100.0% | PASS |
| `retention` (AudioRetentionManager + RetryEligibility) | 17/19 | 89.5% | PASS |
| `data/db` (OutboxStateMachine + NoteStatus + NoteEntity) | 46/47 | 97.9% | PASS |
| `data/repository` (UrlValidator) | 6/6 | 100.0% | PASS |
| `ui` (NotePreview) | 6/6 | 100.0% | PASS |
| **Overall measured** | **144/283** | **50.9%** | **FAIL** |

The implementer's iteration-1 notes claimed the engine package retained 86.5% JaCoCo coverage via `robolectric.sandbox.instrumentationPackages`. This is **false**. The actual measurement is 16.8% — only constructors and `getName()` are captured. The `robolectric.sandbox.instrumentationPackages` system property is not preventing Robolectric from stripping JaCoCo offline instrumentation probes from `MacTranscriptionEngine` and `OpenAITranscriptionEngine`. The claimed 90.7% overall / 86.5% engine from iteration-1 were stale numbers from before the F-5 regression to `org.json` (which forced the Robolectric runner). Actual current measurement: 50.9% overall, 16.8% engine.

---

## AC Coverage Audit — Iteration 2

Only re-auditing ACs that were flagged as weak or whose covering tests were modified in iteration 1.

| AC | Prior verdict | Iteration-2 finding |
|----|--------------|---------------------|
| AC-27 | WEAK (tautological) | **PARTIAL IMPROVEMENT.** The F-12 fix replaced the tautological constant assertion with two tests that exercise the service lifecycle. The first test (`audio_focus_loss_stop_path_does_not_crash`) delivers focus-loss via a try/catch reflection route, but the implementation falls back to ACTION_STOP when the method is not found — which exercises a DIFFERENT code path. The TE-2 tests (`AudioFocusListenerTest`) address this by retrieving the registered `OnAudioFocusChangeListener` via `ShadowAudioManager.getLastAudioFocusRequest().audioFocusRequest` and invoking it via reflection. Test log confirms the production log line fires: `"Audio focus lost — finalizing recording to pending"`. AC-27 now has a genuine behavioral test. **Status: CLOSED by TE-2 test.** |
| AC-30 / E-32 | WEAK (tautological) | **E-32 IMPROVED, E-33 STILL OPEN.** The `zero_byte_audio_does_not_insert_room_row__E32_F11` test drives the real service through ACTION_STOP and asserts no DB row is inserted. This is genuine (log confirms the guard fired). **E-33 (Room-insert-failure → audio-file-deleted) remains untested** in production code: `room_insert_failure_deletes_audio_file__E33_F11` is still a tautological no-op — it throws `RuntimeException` in the test body, not in the production DAO, and never calls `saveNoteAtomically`. The private method plus singleton `AppDatabase.getInstance()` make injection impossible without production code changes. AC-30/E-33 remains a finding. |
| AC-33 / E-34 | PASS (but F-2 test was simulated) | **IMPROVED.** TE-2 added `ResetStaleProductionPathTest` which calls the actual production `TranscribeWorker.resetStaleAndRequeue(context)`. Log confirms: `"Reset 1 stale 'transcribing' note(s) to 'pending'"`. Now genuinely closed. |

---

## Specific Verdicts on Scrutinized Items

### F-11 (TE-M-1): AC-30/E-32/E-33 rollback — PARTIALLY CLOSED

**E-32 (zero-byte guard):** Closed. The `zero_byte_audio_does_not_insert_room_row__E32_F11` test drives the real service; the Robolectric `ShadowMediaRecorder` naturally produces a zero-byte file, triggering the real production guard in `stopRecordingAndSave()` (`file.length() == 0L`). Genuine behavioral test.

**E-33 (Room-insert-failure → file-delete):** Still open. The `room_insert_failure_deletes_audio_file__E33_F11` test throws a `RuntimeException` in a local try/catch block and then calls `audioFile.delete()` directly in the test body. This is still the same tautological pattern — it never invokes `saveNoteAtomically` or any production DAO. The production rollback path in `RecordingService.saveNoteAtomically` lines 263-266 is untested. A genuine test requires either a DAO injection seam in the service (production refactor) or a way to force Room to throw on insert (only achievable via duplicate PK, but UUID is generated inside the method). **This finding is downgraded from MAJOR to MINOR** because: (1) E-32 is now genuinely tested end-to-end; (2) the E-33 code path (Room-failure rollback) is simple and the code is directly visible at lines 263-266 of `RecordingService.kt`; (3) in the Robolectric environment, Room's in-memory implementation does not fail on insert, so this path is architecturally non-injectable without a production refactor. The risk is low but measurable.

**Final verdict on F-11: MAJOR DOWNGRADED TO MINOR** (E-32 closed, E-33 architecturally non-injectable without refactor).

### F-12 (TE-M-2): AC-27 audio-focus-loss → pending — CLOSED by TE-2

**Implementer's claim:** "ShadowAudioManager.notifyAudioFocusChangeListeners() is not available in Robolectric < 4.10." This was used to justify falling back to ACTION_STOP.

**TE-2 verification:** Confirmed that `notifyAudioFocusChangeListeners` does NOT exist in Robolectric 4.13's `ShadowAudioManager` (verified by decompiling `shadows-framework-4.13.jar`). HOWEVER, the listener IS retrievable:

- `ShadowAudioManager.getLastAudioFocusRequest()` returns a `ShadowAudioManager$AudioFocusRequest` with a public `audioFocusRequest` field (type `android.media.AudioFocusRequest`).
- In Robolectric's instrumented platform (`android-all-instrumented-14-robolectric-10818077-i6.jar`), `AudioFocusRequest.getOnAudioFocusChangeListener()` is a `public` method.

The TE-2 test `AudioFocusListenerTest.audio_focus_loss_listener_invocation_stops_recording__AC27_TE` retrieves the listener via reflection and invokes `onAudioFocusChange(AUDIOFOCUS_LOSS)` on it. The test log confirms the production listener fired: `"Audio focus lost — finalizing recording to pending"`.

**The implementer's F-12 tests were inadequate** (both fall back to ACTION_STOP, not the focus listener). The TE-2 test closes AC-27 properly. **F-12: CLOSED by TE-2 test.**

### F-5 (Coverage): Engine JaCoCo — REGRESSION CONFIRMED, STILL OPEN

**Implementer's claim:** "Engine tests moved to Robolectric; coverage still measured via `robolectric.sandbox.instrumentationPackages`."

**TE-2 finding:** This is **false**. The actual JaCoCo numbers from the current `jacocoTestReport`:

- `engine` package: **16.8%** (27/161 lines) — only constructors and `getName()` covered.
- `MacTranscriptionEngine`: **11.3%** — `checkAvailabilityState`, `transcribe`, `checkHealth`, `mapTranscribeResponse`, `parseJsonStringField`, `extractBooleanField`: all 0 covered.
- `OpenAITranscriptionEngine`: **20.3%** — `isAvailable`, `transcribe`, `transcribeInternal`, `mapResponse`, `extractTextField`: all 0 covered.

The `robolectric.sandbox.instrumentationPackages = "com.murmur.mobile"` system property does NOT prevent Robolectric's `SandboxClassLoader` from stripping JaCoCo offline instrumentation probes from engine classes. The overall measurable JaCoCo coverage is **50.9%** (down from the claimed 90.7%).

The engine package at 16.8% violates R-13 (≥80% for new-code coverage). This is a regression caused by the F-5 fix moving engines from pure JVM to Robolectric.

**F-5: NOT CLOSED — regression confirmed, engine coverage is 16.8%.**

### F-1 (enqueue from production paths) — PARTIALLY IMPROVED

**Prior ValidationFixesTest:** Only called `TranscribeWorker.enqueue(context, noteId)` directly — not through any production code path.

**TE-2 addition:** `EnqueueProductionPathTest.retry_note_production_path_enqueues_worker__F1_AC44_TE` calls the production `NoteViewModel.retryNote(failedNote)` and verifies the work item is enqueued. Test passes; retry path is now verified.

**Still open:** The recording-stop path (`RecordingService.saveNoteAtomically → enqueue`) cannot be tested headlessly because `ShadowMediaRecorder` never writes audio data, so the file is always 0 bytes and the guard exits before `saveNoteAtomically` is called. This path is inherently on-device (MV-57).

**Verdict: Partially closed. Retry path closed. Recording-stop production path remains on-device-only.**

### F-2 (resetStaleAndRequeue production path) — CLOSED by TE-2

`ResetStaleProductionPathTest.production_resetStaleAndRequeue_resets_and_enqueues__F2_AC33_E34_TE` calls the actual production `TranscribeWorker.resetStaleAndRequeue(context)`. Test log confirms: `"Reset 1 stale 'transcribing' note(s) to 'pending'"`. F-2: CLOSED.

### F-3, F-4, F-6, F-7, F-8, F-9, F-10 — No regressions found

These were all clean in iteration-1 and the suites confirm no regressions.

---

## New Tests Added in Iteration 2

### Component B (Android) — 3 new test files, 6 new tests

**`recording/AudioFocusListenerTest.kt`** — 2 tests [test-engineer]:
1. `audio_focus_loss_listener_invocation_stops_recording__AC27_TE` — retrieves production `OnAudioFocusChangeListener` via Robolectric shadow API and invokes it with `AUDIOFOCUS_LOSS`; confirms production log fires.
2. `audio_focus_loss_is_noop_when_service_not_recording__AC27_TE` — delivers focus-loss when service not recording; asserts no DB row inserted.

**`worker/ResetStaleProductionPathTest.kt`** — 2 tests [test-engineer]:
3. `production_resetStaleAndRequeue_resets_and_enqueues__F2_AC33_E34_TE` — calls the actual `TranscribeWorker.resetStaleAndRequeue()` production function; confirms reset + enqueue.
4. `production_resetStaleAndRequeue_noop_when_no_stale_notes__F2_TE` — proves no-hang when no stale notes.

**`worker/EnqueueProductionPathTest.kt`** — 2 tests [test-engineer]:
5. `retry_note_production_path_enqueues_worker__F1_AC44_TE` — calls production `NoteViewModel.retryNote()`; asserts FAILED → PENDING and work item enqueued.
6. `recording_stop_with_empty_file_does_not_enqueue__F1_E32_TE` — confirms no spurious enqueue when service stops with an empty file.

**Total new tests added in iteration 2: 6** (all pass deterministically, no sleeps-as-synchronization issues beyond `Thread.sleep(400)` which is used for Robolectric coroutine dispatch, consistent with existing tests in the same repo).

**Cumulative TE-added tests: 40** (34 from iteration-1 + 6 from iteration-2).

---

## Findings

### Severity Table

| Sev | ID | File:line | Finding | Suggested Action |
|-----|-----|-----------|---------|-----------------|
| Major | TE2-M-1 | `app/build/reports/jacoco/jacocoTestReport/jacocoTestReport.xml` (engine package) | **Engine coverage regression: 16.8% (was claimed 86.5%).** Moving `MacTranscriptionEngine` and `OpenAITranscriptionEngine` to `@RunWith(RobolectricTestRunner::class)` (F-5 fix) stripped all JaCoCo probes from these classes. Overall measured JaCoCo is 50.9% (not 90.7%). R-13 (≥80%) is violated for the engine package. The `robolectric.sandbox.instrumentationPackages` workaround does not work. Root fix: restore engine implementations to pure JVM by replacing `org.json.JSONObject` with a JVM-native JSON library (e.g., `org.json` is available as a standalone non-Android artifact on Maven, or use `kotlinx.serialization`, or a minimal hand-rolled parser that doesn't invoke Android platform classes — Moshi, Gson, or Jackson are all pure JVM). | Replace `org.json.JSONObject` with a pure-JVM JSON parser in `MacTranscriptionEngine` and `OpenAITranscriptionEngine`, remove `@RunWith(RobolectricTestRunner::class)` from `JsonParserTest`, `MacEngineTest`, `MacEngineEdgeCaseTest`, `OpenAiEngineTest`, `OpenAiEdgeCaseTest`. JaCoCo will then measure engine coverage. |
| Minor | TE2-Mi-1 | `app/src/test/.../recording/RecordingPolicyTest.kt:114-129` | **E-33 Room-failure rollback still untested.** `room_insert_failure_deletes_audio_file__E33_F11` throws in the test body and calls `audioFile.delete()` directly — it never invokes `saveNoteAtomically`. The production rollback path at `RecordingService.kt:263-266` (catch block deletes audio file on Room insert failure) has zero behavioral test coverage. The path is only achievable headlessly if `RecordingService.saveNoteAtomically` is refactored to accept an injectable `NoteDao`. | Refactor `saveNoteAtomically` to accept a `NoteDao` parameter (or inject via a factory). With injection, the test can pass a mock/spy DAO that throws on `insert`, and assert the file is deleted. Until then, the E-33 compensating-action path is documented as on-device-only. |
| Minor | TE2-Mi-2 | `app/src/test/.../recording/RecordingPolicyTest.kt:147-189` | **F-12 implementing tests still exercise ACTION_STOP, not the focus-loss listener.** `audio_focus_loss_stop_path_does_not_crash__AC27_F12` tries reflection but falls back to `ACTION_STOP` when `notifyAudioFocusChangeListeners` is not found (which it won't be in 4.13). The TE-2 test properly exercises the listener, but the implementer's own test is misleadingly named. | Non-blocking: the TE-2 test closes AC-27. The misleading test should be renamed or updated to clarify it tests ACTION_STOP, not focus-loss. |

---

## Verdict

**FAIL**

**Reason:** 1 major finding blocks shipping:

- **TE2-M-1 (new):** Engine JaCoCo coverage is 16.8%, not the claimed 86.5%. The F-5 fix (moving engines to Robolectric for `org.json`) regressed coverage below the 80% R-13 gate. `robolectric.sandbox.instrumentationPackages` does not fix this. The overall measurable JaCoCo drops to 50.9%.

**Items from iteration-1 that are now closed:**
- TE-M-2 (AC-27 tautological test): Closed by TE-2 `AudioFocusListenerTest`.
- F-2 (resetStaleAndRequeue simulation): Closed by TE-2 `ResetStaleProductionPathTest`.
- F-1 retry path: Closed by TE-2 `EnqueueProductionPathTest`.

**Items that remain open or are newly downgraded:**
- TE-M-1 → **downgraded to Minor** (E-32 closed; E-33 architecturally non-injectable without refactor).
- TE2-M-1 (new Major): Engine JaCoCo coverage regression.

Both suites are green. 61/61 ACs have a proving test or manual runbook step. 6 new tests added (40 cumulative TE-added). 0 blockers. 1 major (TE2-M-1). 2 minors. **FAIL on major (TE2-M-1).**
