# Code Review — murmur-mobile-2026-06-14
**Reviewer:** code-reviewer · **Iteration:** 3 (final re-validation after fix-list-2) · **Date:** 2026-06-14

---

## Code Review

### Re-validation of G-1 (NV-1), G-2 (NV-3), G-4 (NV-2)

---

#### G-1 (NV-1) — §A validation order AND atomicity — **NOT CLOSED / NEW BLOCKER**

**Order claim (G-1a):** Confirmed correct. `lock.locked()` returns 503 `model_busy` at step 2
(`app.py:201–202`), unconditionally before the `model_loaded_flag.is_set()` check at step 3
(`app.py:205–206`). The proving test `test_model_busy_beats_model_not_loaded__G1_spec_A`
(`test_server_singleflight.py:111–167`) holds the lock, clears the model flag, fires a second
request, and asserts 503 `model_busy`. The order claim is now correct.

**Atomicity claim (G-1b): FALSE — TOCTOU reintroduced.** The fix commit message and the inline
comment at `app.py:256–262` assert: "All file I/O awaits are completed above. The lock.locked()
check at step 2 above and the `async with lock:` acquisition below have NO intervening await."
This is factually incorrect. Examining the code between the busy-check (line 201) and the acquire
(line 265):

- `app.py:213`: `first_chunk = await file.read(1)` — this is a real `await` expression that
  yields to the asyncio event loop, allowing another coroutine to be scheduled.
- `app.py:221`: `chunk = await file.read(65536)` inside a loop — each iteration is a real `await`
  that yields.

Both of these `await` expressions occur AFTER `if lock.locked(): return 503` (line 201) and
BEFORE `async with lock:` (line 265). The asyncio event loop CAN and DOES yield between them.
Between those yields, another coroutine can acquire the lock, rendering the busy-check stale.

This is the TOCTOU race that F-3 originally fixed and that fix-list-2 item G-1 was required to
preserve. The fix-list-2 instruction was explicit: "Keep the single-flight ATOMIC (no `await`
between the busy-check and `async with lock:`/acquire) — do not reintroduce the TOCTOU." The
implementation-notes entry for G-1 (iteration 2 section) states the atomicity analysis from F-3
"still holds" — it does not. The original F-3 fix placed `lock.locked()` AFTER all file I/O
awaits, so there were zero `await` expressions between the check and the acquire. The G-1 fix
moved `lock.locked()` to BEFORE the file I/O awaits, thereby placing real `await` expressions
between check and acquire.

**Concrete race scenario:** Two concurrent requests (A and B) both see `lock.locked()==False` at
step 2, proceed through the `await file.read(...)` calls at steps 5/6 (yielding the event loop),
and both reach `async with lock:`. Request A acquires the lock; request B also acquires the lock
(asyncio.Lock is FIFO-queued, not rejected, once both have passed the busy-check). Two
transcriptions execute concurrently — the single-flight guarantee is broken.

Note: The test `test_concurrent_transcribe_503_model_busy` is a timing-sensitive empirical test,
not a structural proof. It fires two requests via `asyncio.create_task` and asserts 503 appears.
This test can pass or fail depending on scheduling; it does not prove atomicity. Specifically,
the test may consistently pass because both tasks race to the same first `await file.read(1)` and
one finishes reading (including all file bytes) before the other's first scheduling quantum
completes — but this is not guaranteed and can fail under load or with large files.

The comment "already-completed awaits" (`app.py:259`) is a category error: an `await` expression
on a file read is not "already completed" before it executes; it completes in the future.

**The correct fix** (which the original F-3 achieved): `lock.locked()` check must occur AFTER
the last `await file.read(...)` call, i.e., immediately before `async with lock:`. That restores
the original §A ordering by putting the busy-check logically before model-loaded BUT textually
after the file I/O — OR an alternative is to keep `lock.locked()` early AND add a second
`lock.locked()` check immediately before `async with lock:`. Either must have zero `await`
between the definitive check and the acquire.

---

#### G-2 (NV-3) — Interface typing in constructor + EngineInterfaceTest — **CLOSED**

`TranscriptionDispatcher.kt:31–33` constructor: `(macEngine: TranscriptionEngine?,
openAiEngine: TranscriptionEngine)`. Both parameters are typed as the interface; no concrete
type names in the parameter list. The import block (`TranscriptionDispatcher.kt:1–6`) imports
only `TranscriptionEngine`, `EngineAvailability`, `EngineResult`, and `java.io.File` — no
import of `MacTranscriptionEngine` or `OpenAITranscriptionEngine`.

`EngineInterfaceTest.kt` defines four hand-written Kotlin classes (`ReadySubstituteEngine`,
`AuthFailedSubstituteEngine`, `UnavailableSubstituteEngine`, `NotApplicableSubstituteEngine`)
that each implement `TranscriptionEngine` directly, with no extension of any concrete engine
class. These are passed to `TranscriptionDispatcher(macEngine = ..., openAiEngine = ...)` at
the call site (e.g., line 159) without any cast. If the constructor still accepted a concrete
type, this would fail to compile. The test `both_engines_substitutable__AC42` also confirms
that `MacTranscriptionEngine` and `OpenAITranscriptionEngine` each satisfy `val _: TranscriptionEngine`.

`TranscribeWorker.kt` (the production caller) constructs `MacTranscriptionEngine` and
`OpenAITranscriptionEngine` locally (lines 172–179) and passes them to the dispatcher — this
is correct since `MacTranscriptionEngine` and `OpenAITranscriptionEngine` implement
`TranscriptionEngine`, and `TranscriptionDispatcher` accepts the interface.

AC-42 and §F substitutability at the constructor boundary: **confirmed closed.**

---

#### G-4 (NV-2) — First retry delay 30 s, cap 300 s — **CLOSED**

`TranscribeWorker.kt:224`:
```kotlin
val delaySec = computeBackoffSeconds((pending.attemptCount - 1).coerceAtLeast(0))
```

`preflight` (`TranscribeWorkerLogic.preflight`) increments `attemptCount` by 1 before writing
the `transcribing` state. After the first failure, `pending.attemptCount == 1`. The formula
computes `computeBackoffSeconds((1-1).coerceAtLeast(0)) = computeBackoffSeconds(0) = 30L`.
The cap path: `computeBackoffSeconds(4) = min(30 * 16, 300) = 300L`. Both match AC-44.

The proving test `wired_retry_first_delay_is_30s_and_cap_is_300s__G4_AC44`
(`ValidationFixesTest.kt:251–264`) simulates the exact formula used in `doWork()` for
`attemptCount=1` (first retry → 30s), `attemptCount=2` (→ 60s), and `attemptCount=10` (→ 300s).
Minor: the test computes the formula itself rather than driving through `doWork()`, but the
formula at line 224 is straightforward and the risk of divergence is low.

**G-4 confirmed closed.**

---

### New Defects from the Iteration-2 Fixes

| # | Sev | Location | Finding | Suggested Action |
|---|-----|----------|---------|-----------------|
| R3-1 | **BLOCKER** | `MurMur/src/murmur/server/app.py:201–265` | **G-1 fix reintroduced the TOCTOU race it was required to eliminate.** `if lock.locked(): return 503` at line 201 is followed by `await file.read(1)` at line 213 and the loop `await file.read(65536)` at line 221 before `async with lock:` at line 265. Each `await` is a genuine event-loop yield point. Two concurrent requests can both pass the line-201 busy-check (seeing the lock free), then both proceed through the file reads (multiple yields), then both reach `async with lock:` and get QUEUED behind each other — asyncio.Lock queues waiters, it does not return immediately if locked when a coroutine `await`s on it. Both transcriptions then execute serially (not concurrently), violating the single-flight guarantee. Additionally, the empirical test `test_concurrent_transcribe_503_model_busy` does not structurally prove atomicity — it is timing-dependent and can produce false positives. The implementation-notes entry for G-1 contains a factually incorrect analysis: "the model-not-loaded and file checks at steps 3-7 are all synchronous or already-completed awaits" is false for steps 5 and 6 which contain real `await` expressions. | Move `if lock.locked(): return 503` to immediately before `async with lock:` (after line 254) so there are zero `await` expressions between the busy-check and the acquire. To preserve the §A ordering requirement (step 2 busy-check before step 3 model-not-loaded), add a SECOND early check at step 2 that short-circuits on `lock.locked()` before the model check — but the AUTHORITATIVE single-flight check that is actually atomic must remain immediately before the acquire. Both checks serve different purposes: the early one preserves §A ordering for the human-readable contract; the late one is the actual gate. Document both checks and their roles. Alternatively, restructure to put all file I/O before the lock check (the original F-3 approach) and fix the §A ordering via the early check only — as long as the one immediately before `async with lock:` has no `await` after it. |

---

### Summary of findings

| Finding | Iteration-2 finding | Status | Evidence |
|---------|----------------------|--------|---------|
| G-1 (NV-1) — §A order | OPEN at iter-2 | ORDER: CLOSED / ATOMICITY: **STILL OPEN (regresssion)** | Ordering: `app.py:201–206` (busy before model-loaded) + test at `test_server_singleflight.py:111`. Atomicity: `await file.read(1)` at line 213, `await file.read(65536)` at line 221 — both between line 201 check and line 265 acquire |
| G-2 (NV-3) — interface typing | OPEN at iter-2 | **CLOSED** | `TranscriptionDispatcher.kt:31–33` typed `TranscriptionEngine?` / `TranscriptionEngine`; `EngineInterfaceTest.kt` uses 4 hand-written substitute impls, no concrete mock |
| G-4 (NV-2) — first retry 30 s | OPEN at iter-2 | **CLOSED** | `TranscribeWorker.kt:224` uses `(pending.attemptCount - 1).coerceAtLeast(0)`; `wired_retry_first_delay_is_30s_and_cap_is_300s__G4_AC44` asserts 30 s |

---

**What was checked in this iteration:**
- Read full `git -C /Users/faithomojola/Documents/MurMur log --oneline -5` and `git -C /Users/faithomojola/Documents/MurmurMobile log --oneline -5`
- Read G-1 fix commit diff in full (`git show b242f84 -- src/murmur/server/app.py`)
- Read `app.py` in entirety, tracing every statement between `lock.locked()` (line 201) and `async with lock:` (line 265), counting `await` expressions
- Read `TranscriptionDispatcher.kt` in entirety, confirming constructor parameter types and import block
- Read `EngineInterfaceTest.kt` in entirety, confirming all four substitute implementations implement only the interface
- Read `TranscribeWorker.kt` in entirety, tracing `computeBackoffSeconds((pending.attemptCount - 1).coerceAtLeast(0))` at line 224
- Read `ValidationFixesTest.kt` in entirety, confirming G-4 proving test
- Read `test_server_singleflight.py` in entirety, confirming G-1 proving test logic
- Read `TranscriptionEngine.kt`, confirming `checkAvailabilityState()` default implementation
- Cross-checked against spec §A steps 2 and 3, fix-list-2 G-1 requirements (order AND atomicity)

---

**Verdict: FAIL**

Blockers: 1 (R3-1) | Majors: 0 | Minors: 0

The single worst issue is **R3-1**: the G-1 fix correctly restored the §A ordering (model_busy step 2 before model_not_loaded step 3) but reintroduced the TOCTOU race that F-3 had eliminated. The `lock.locked()` busy-check now sits at line 201, seven `await` expressions before the `async with lock:` acquire at line 265. Two concurrent requests can both pass the busy-check while the lock is free, execute the file-read awaits in interleaved event-loop ticks, and both reach the acquire — where asyncio.Lock queues them rather than rejecting the second. Both conditions that fix-list-2 required to hold simultaneously (order AND atomicity) do not simultaneously hold in the current code. The implementation-notes entry for G-1 (iteration 2) contains an incorrect atomicity analysis that was accepted without verification.

G-2 is genuinely closed — the dispatcher constructor now takes the interface type and the test uses hand-written substitutes. G-4 is genuinely closed — the off-by-one is fixed with `.coerceAtLeast(0)` and there is a wired proving test.

This is iteration 3, the final allowed iteration. R3-1 is a blocker. Per operating rules: **mandatory escalation to the human** — the validation loop cap has been reached with an unresolved blocker.
