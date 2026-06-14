# Code Review — murmur-mobile-2026-06-14
**Reviewer:** code-reviewer · **Iteration:** 4 (post-escalation fix, single targeted review) · **Date:** 2026-06-14

---

## Code Review

### Scope

This review covers ONLY the H-1 / R3-1 fix: commit `f00f461` touching `src/murmur/server/app.py` and `tests/unit/test_server_singleflight.py`. No other files changed. Android repo untouched (confirmed from diff).

---

### What was checked

- Read `src/murmur/server/app.py` in full.
- Read `git -C /Users/faithomojola/Documents/MurMur show f00f461` (the complete commit diff).
- Read `tests/unit/test_server_singleflight.py` in full (all four test functions).
- Inspected CPython `asyncio.Lock.acquire`, `locked`, and `release` source (Python 3.11) to confirm the fast-path guarantee.
- Ran two isolated asyncio simulations to confirm (a) `await lock.acquire()` does not yield when the lock is free and `_waiters` is None, and (b) under the new code's invariant `_waiters` is always None at the point of acquire.
- Read spec (AC-14, E-10, E-11, AC-13, §A steps 1-11), plan (§A, ADR-2), fix-list-3 (H-1), and implementation-notes (Post-escalation section).

---

## Findings

| # | Sev | File:Line | Finding | Action |
|---|-----|-----------|---------|--------|
| — | — | — | No blockers, majors, or minors found. Full rationale below. | — |

---

### 1. Atomicity — R3-1 genuinely closed

**Finding: CLOSED.**

The prior race had `if lock.locked(): return 503` (step 2) separated from `async with lock:` (step 8) by multiple `await file.read()` expressions. The new code replaces that pattern with:

```
if lock.locked():                         # line 215 — synchronous
    return _error(503, _E_MODEL_BUSY, …)  # line 216 — returns immediately
await lock.acquire()                       # line 217 — fast-path, no yield
try:
    … all steps 3-11, including all await file.read() calls …
finally:
    lock.release()                         # line 281
```

There is literally no `await` expression between line 215 (`lock.locked()`) and line 217 (`await lock.acquire()`). The two statements are separated only by the `return` on line 216, which exits the handler entirely for the busy case. In the non-busy case, control falls directly from line 215 to line 217 with zero intermediate statements.

**CPython fast-path confirmation.** `asyncio.Lock.acquire()` has an early-exit branch:

```python
if (not self._locked and (self._waiters is None or
        all(w.cancelled() for w in self._waiters))):
    self._locked = True
    return True
```

This branch has no `await`. Under the new code's invariant — only the request that sees `lock.locked()==False` ever calls `lock.acquire()`, so all other requests return 503 before reaching line 217 — `_waiters` is always `None` at line 217. Therefore the fast-path branch is always taken: `_locked` becomes `True` and the coroutine returns without suspending. The event loop cannot schedule any other coroutine between lines 215 and 217. Confirmed by simulation: a side-task created before `await lock.acquire()` on a free lock does not run before the acquire returns.

**Two concurrent requests, traced:**
- Request A: line 215 → `lock.locked()==False` → line 217 → lock acquired atomically (no yield). Now holds the lock.
- Request B: line 215 → `lock.locked()==True` (A holds it) → line 216 → returns 503 `model_busy`. Never reaches line 217. Never reads the upload.

There is no window in which B can slip past the busy-check. The TOCTOU window is closed.

### 2. Lock leak — no leak possible

**Finding: NO LEAK.**

The structure is:

```
await lock.acquire()   ← line 217 (outside try)
try:                   ← line 219
    … all returns, all raises, all await expressions …
finally:
    lock.release()     ← line 281
```

Every path from line 217 onward is covered by the `try/finally`. Enumerated paths:
- Step 3 returns 503 `model_not_loaded` → `finally` releases.
- Step 4 returns 400 `missing_file` → `finally` releases.
- Step 5 returns 400 `empty_file` → `finally` releases.
- Step 6 returns 413 `file_too_large` (inside the while loop) → `finally` releases.
- Step 7 returns 415 `unsupported_media_type` → `finally` releases.
- `_run_transcription` returns 422 `audio_decode_failed` → `finally` releases.
- `_run_transcription` returns 422 `transcription_failed` → `finally` releases.
- `_run_transcription` returns 422 `no_speech_detected` → `finally` releases.
- `_run_transcription` returns 200 → `finally` releases.
- Any unhandled exception escapes `_run_transcription` → `finally` releases (FastAPI catches it at a higher level and returns 500).

The only code between `await lock.acquire()` (line 217) and `try:` (line 219) is the `try:` keyword itself, which is a statement header — it is not executable code that can raise. There is no code path from line 217 that reaches `finally` without the `try:` block having been entered.

**`_run_transcription` inner `finally` for temp-file cleanup (lines 298-358):** Correct and independent of the lock-release `finally`. The temp-file `finally` (line 356: `tmp_path.unlink(missing_ok=True)`) is nested inside the outer lock-release `finally`. On any error inside `_run_transcription`, the inner `finally` runs first (deletes temp file), then the inner function returns an error `Response`, then the outer `finally` releases the lock. This ordering is correct.

**`lock.acquire()` itself raising before `try:`.** On the fast path (confirmed above: always taken under this invariant), `lock.acquire()` sets `_locked=True` and returns `True` without any `await`. CancelledError can only be raised at an `await` point. Therefore `lock.acquire()` cannot raise on the fast path, and the concern of a lock acquired but `try:` not reached is moot.

### 3. §A order — preserved

**Finding: CORRECT.**

Step 2 (model_busy, line 215) is the first post-auth gate. Step 3 (model_not_loaded, line 221) is inside the `try:` block, after the lock is acquired. The spec's §A ordering is: auth (1) → busy (2) → model_not_loaded (3) → file absent (4) → empty (5) → size (6) → MIME (7) → decode (8) → transcribe (9) → clean (10). All 10 post-auth steps appear in exactly this order in the code.

The proving test `test_model_busy_beats_model_not_loaded__G1_spec_A` holds the lock via a slow decode, clears the model flag, fires a second request, and asserts 503 `model_busy` (not `model_not_loaded`). This correctly validates the ordering.

### 4. AC-13 not regressed — `/health` still lock-free

**Finding: CORRECT, NOT REGRESSED.**

`GET /health` (`app.py` lines 154-173) reads `app.state.model_loaded_flag.is_set()` (a `threading.Event`). It does not reference `app.state.lock` or `_lock` in any form. The lock is now held during `await file.read()` calls (steps 5-6), but `await file.read()` yields to the event loop — which can then process a `/health` request. The slow transcription work (ffmpeg decode, Whisper transcribe) is offloaded via `run_in_executor` (lines 307-309, 319-324), keeping the event loop free for concurrent `/health` throughout the most expensive phase.

The `test_health_not_blocked_by_lock` test (`app.py` uses `_make_slow_decode(1.0)`) starts a transcription, sleeps 0.05 s (enough for the handler to acquire the lock and reach `run_in_executor` given a 15-byte payload), then issues a `/health` request and asserts it completes in under 0.5 s. This test continues to be structurally valid: the 0.05 s margin is generous relative to reading 15 bytes.

### 5. No other behavior changed

**Finding: CONFIRMED.**

The diff shows the validation steps 3-11 have been moved inside `try:` but are otherwise byte-for-byte identical to the prior code. Error codes, HTTP status codes, response shapes, `_run_transcription` logic, temp-file cleanup, token redaction, and all helper functions (`_resolve_hints`, `_check_auth`, `_error`) are untouched. The plan's §A step ordering and the error-code registry (ADR-8) are preserved.

### 6. Availability assessment

**Finding: ACCEPTABLE.**

The lock is now held during the upload-read phase (`await file.read()` calls). For a 25 MB file at typical Tailscale LAN speeds (>100 Mbps), upload reads complete in under 2 seconds. During this time, a concurrent `POST /transcribe` receives 503 immediately (correct behavior), and `GET /health` is served from the event loop between reads (not blocked). The tradeoff — holding the lock during upload read to guarantee atomicity — is the right call for this context: single-user, single-flight, Tailscale-private server. The implementation notes document this rationale (Post-escalation section, "Design rationale"). No practical availability problem exists.

### 7. Comments — no false claims

**Finding: COMMENTS ARE NOW ACCURATE.**

The prior fix's comment ("no intervening await / all file I/O awaits completed above") was factually false and was the specific lie identified in R3-1.

The new comment block (lines 195-214) states:
1. "no `await` expression appears between them" — True: lines 215, 216, 217 are the only statements; line 216 returns (no await), line 217 is the acquire.
2. "`await lock.acquire()` — when the lock is FREE, CPython's asyncio.Lock sets `_locked=True` and returns `True` immediately without suspending" — True: confirmed by reading the CPython source and by simulation.
3. "Every subsequent concurrent request sees `lock.locked()==True` at statement 1 and returns 503 `model_busy` before reading the upload" — True: this follows from the atomicity established in (1) and (2).

All three claims are factually correct and directly verifiable.

### 8. Proving test — structural validity

**Finding: STRUCTURALLY SOUND.**

`test_concurrent_transcribe_race_proof__R3_1_AC14` patches `starlette.datastructures.UploadFile.read` to prepend `await asyncio.sleep(0)`, forcing a deterministic event-loop yield at the exact former race point (step 5/6 file reads). This is:
- **Deterministic:** `asyncio.sleep(0)` always yields to the event loop on every call, regardless of payload size or scheduling heuristics.
- **Structurally correct under old code:** With the old pattern, both tasks would pass the busy-check (lock free), both would yield at the patched `file.read`, both would reach `async with lock:`, and the second would queue — producing two 200 responses. The test's assertion `assert 503 in statuses` would fail.
- **Structurally correct under new code:** Task A atomically acquires the lock at step 2. The patched `file.read` yield at step 5 is now INSIDE the `try:` block (lock already held). Task B reaches `lock.locked()==True` at line 215 and returns 503. The patched `file.read` in Task B is never reached. Assertion holds.
- **Transcriber ran exactly once:** `assert len(slow_transcriber.calls) == 1` — correct; the single-flight guarantee means one transcription, not two.

One observation: the test does not assert the 200 response is valid (it only checks 503 is present and the transcriber ran once). This is fine — other tests cover the 200 response shape. The specific purpose of this test is to prove the race is closed, and it does that.

---

## Summary

| Sev | Count |
|-----|-------|
| Blocker | 0 |
| Major | 0 |
| Minor | 0 |

**Verdict: PASS**

R3-1 is genuinely closed. The fix is atomic (confirmed by CPython source inspection and simulation), has no lock leak (confirmed by exhaustive path enumeration), preserves §A ordering (step 2 before step 3, confirmed), does not regress AC-13 (health lock-free, confirmed), leaves all other behavior unchanged (confirmed by diff inspection), and carries no false comments (every claim in the comment block is now literally true). The proving test is structurally valid and would fail on the prior (racy) code.
