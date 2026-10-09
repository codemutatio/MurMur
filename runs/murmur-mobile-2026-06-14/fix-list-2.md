# Fix List — Iteration 2

**Run ID:** murmur-mobile-2026-06-14 · **Source:** validation-report.md iteration 2 · **Date:** 2026-06-14

⚠️ **This is the LAST fix iteration before mandatory escalation.** All 3 majors below are *surgical regressions introduced by the iteration-1 fixes*. Fix EXACTLY these three (+ the two cheap minors). **Do NOT** refactor broadly, change unrelated behavior, or weaken/delete any passing test. Every prior passing test (server 354, android 187) must still pass. After each change, re-run the full suite to confirm you did not introduce a new regression (that is what happened last round).

## Required (majors — must close to pass the gate)

| # | Sev | Origin | Location | Problem | Required outcome |
|---|-----|--------|----------|---------|------------------|
| G-1 | major | code-review (NV-1) | MurMur `src/murmur/server/app.py` (~198-260) | The F-3 atomicity reorder inverted §A validation order: the `model_loaded` check now precedes the single-flight busy check, so `model_not_loaded` (503) wins over `model_busy` (503) when both hold — contrary to spec §A (busy is step 2, model-loaded is step 3). | Order the two synchronous checks per §A: `if lock.locked(): 503 model_busy` BEFORE `if not model_loaded: 503 model_not_loaded`. Keep the single-flight ATOMIC (no `await` between the busy-check and `async with lock`/acquire) — do not reintroduce the TOCTOU. A test asserts the §A precedence (busy wins when both conditions hold) OR, if you show the two states are mutually exclusive, document that reasoning in implementation-notes and keep the §A textual order anyway. Do not regress F-3. |
| G-2 | major | code-review (NV-3) | MurmurMobile `dispatch/TranscriptionDispatcher.kt` (constructor) + `EngineInterfaceTest` | F-7 routed the health call through `checkAvailabilityState()` but the dispatcher CONSTRUCTOR still binds concrete `MacTranscriptionEngine?` / `OpenAITranscriptionEngine`; AC-42 / §F "dispatcher depends only on the `TranscriptionEngine` interface" is still unmet, and `EngineInterfaceTest` masks it by mocking the concrete class. | Type the dispatcher's engine dependencies as the `TranscriptionEngine` interface (ordered list or interface-typed params), so a substitute engine needs no dispatcher change. `EngineInterfaceTest` must drive the dispatcher with a hand-written substitute `TranscriptionEngine` implementation (NOT a mock of the concrete class) and assert correct dispatch — proving AC-42 substitutability. Keep all dispatcher decision-table tests green. |
| G-3 | major | test-eng (TE2-M-1) | MurmurMobile `app/build.gradle.kts` (test deps/config) + `engine/*Test.kt` | F-5's switch to Android-platform `org.json` forced the engine tests onto Robolectric, dropping engine JaCoCo coverage to 16.8% (overall measured 50.9%) — violates R-13 (≥80%). | Restore measurable engine coverage to ≥80% while KEEPING the F-5 correctness fix (escaped quotes/backslashes parse intact). Recommended: add a real JVM JSON parser on the unit-test classpath — e.g. `testImplementation("org.json:json:20240303")` so `org.json.JSONObject` resolves to the real artifact in pure-JVM tests — then remove `@RunWith(RobolectricTestRunner::class)` from the engine test files so JaCoCo instruments them. (Production may keep using the platform `org.json`.) Re-run `jacocoTestReport`; the `engine` package must measure ≥80% and overall measured coverage must be ≥80%. The escaped-quote test must still pass. |

## Required (cheap minors — close these too, they are trivial and leave a clean state)

| # | Sev | Origin | Location | Problem | Required outcome |
|---|-----|--------|----------|---------|------------------|
| G-4 | minor | code-review (NV-2) | MurmurMobile `worker/TranscribeWorker.kt` (`computeBackoffSeconds` / retry path) | First retry fires at 60s, not AC-44's "starting at 30 seconds" (attemptCount off-by-one). | First retry delay = 30s, then 60s, 120s, … capped at 300s. A test asserts the first retry delay is 30s and the cap is 300s. |
| G-5 | minor | security (SR-6) | MurMur `tests/.../test_validation_fixes.py` | implementation-notes claim two proof tests exist (`test_python_multipart_version__CVE_2024_53981`, `test_sanitized_tempfile_suffix__SR3`) but they are absent — integrity gap (the code fixes are correct). | Add the two named tests (assert `python-multipart` resolved ≥0.0.18 / the metadata version; assert a `/`-containing upload filename yields a sanitized suffix and the contracted error, not a 500). Or, if you prefer, correct the notes to reference the tests that actually prove these — but adding the tests is preferred. |

## Optional (logged, non-blocking — only if zero-risk)
- TE2-Mi-1 / E-33: the Room-insert-failure→file-delete rollback in `RecordingService.saveNoteAtomically` still isn't driven by a real test (would need a small injectable-DAO seam). Nice-to-have; skip if it risks churn.
- NV-4: add a test that drives `enqueue` via the production `saveNoteAtomically`/`retryNote` callsite (production wiring is already correct).
- CR-13-follow: wire a Settings UI warning that reads `isEncryptionFallback`.

## After fixing — required green state (paste all of it)
- Server: `uv run pytest` (≥ prior 354 passing, coverage ≥80), `uv run ruff check`, `uv run mypy src/murmur` — clean.
- Android: `JAVA_HOME=$(/usr/libexec/java_home -v 21) ./gradlew --no-daemon assembleDebug testDebugUnitTest jacocoTestReport lintDebug` — green; **paste the per-package JaCoCo table** proving engine ≥80% and overall ≥80%.
- Append a "Validation iteration 2 fixes" section to implementation-notes.md (per G-n: what changed + the proving test). Commit incrementally per fix (`fix(...): … [G-n, …]`).
- Confirm you introduced NO new behavior beyond G-1..G-5 and that all previously-passing tests still pass. Watch `df -h /`; if <1.5 GB, stop and report.