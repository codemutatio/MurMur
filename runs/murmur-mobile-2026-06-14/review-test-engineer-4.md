# Test Engineering Review — Iteration 4 (Post-escalation H-1 / R3-1)

**Run ID:** murmur-mobile-2026-06-14  
**Reviewer:** Test Engineer (adversarial)  
**Date:** 2026-06-14  
**Scope:** Single targeted fix — server TOCTOU single-flight race (H-1 / R3-1, AC-14, E-10). Android untouched.

---

## Test Engineering

### 1. Full Server Suite Result

Command: `uv run pytest --tb=short -q`

```
358 passed, 1 deselected, 1 xfailed, 7 warnings in 42.51s
Required test coverage of 80.0% reached. Total coverage: 86.76%
```

Result: GREEN. Meets the ≥358 passing and ≥80% coverage requirements exactly. The `1 deselected` is a hardware-marked integration test (expected; no regression). The `1 xfailed` is a pre-existing expected failure (unchanged).

Coverage for `src/murmur/server/app.py` specifically: **99%** (149 stmts, 2 missed — lines 164-165, which are a `try/except` in the version lookup path, unreachable without packaging error injection).

### 2. Race-Proof Test Analysis — `test_concurrent_transcribe_race_proof__R3_1_AC14`

Located at: `/Users/faithomojola/Documents/MurMur/tests/unit/test_server_singleflight.py:216`

**Criterion (a) — genuinely concurrent requests with NO artificial sleep ordering:**
Confirmed. Both requests are launched as `asyncio.create_task()` with no `asyncio.sleep()` between task creation and `asyncio.gather()`. The only sleep is inside the decode function (which runs in a thread pool executor after the lock is held, not before), and the patched `UploadFile.read` which is the race-trigger mechanism, not an ordering mechanism.

**Criterion (b) — deterministic event-loop yield at the file-read await:**
Confirmed. The test patches `starlette.datastructures.UploadFile.read` to prepend `await asyncio.sleep(0)` before calling the real read. This forces a guaranteed event-loop context switch at the exact location where the old code had its race window (the `await file.read(1)` and `await file.read(65536)` calls that appeared between the `lock.locked()` check and the `async with lock:` acquire in the prior code). The yield is deterministic and does not rely on scheduling coincidence.

**Criterion (c) — asserts second gets 503 model_busy:**
Confirmed. The test asserts:
- `503 in statuses` (at least one 503 response)
- `len(busy_resps) == 1` (exactly ONE 503, not two)
- `busy_resps[0].json()["error_code"] == "model_busy"` (correct error code)
- `len(slow_transcriber.calls) == 1` (transcriber ran exactly once — single-flight enforced)

**Criterion — proves test fails on old pattern:**
Verified empirically via an isolated reproducer script (`_te_racy_app_test.py`, created and then deleted after testing). The script recreated the old TOCTOU handler structure:

```
Step 2: if lock.locked(): return 503    # check (no await)
# ... model check, file presence check (synchronous) ...
Step 5: first_chunk = await file.read(1)    # YIELD 1 — race window open
Step 6: chunk = await file.read(65536)      # YIELD 2 — race window open
...
Step 8: async with lock: ...               # actual acquire — queues, not rejects
```

With the same `UploadFile.read` patch injecting `await asyncio.sleep(0)`:
- Both concurrent tasks saw `lock.locked() == False` (lock was free at step 2)
- Both yielded at `await file.read(1)` (patched), switching context to the other task
- Both reached `async with lock:` and were queued by asyncio.Lock (FIFO, not reject)
- **Both returned 200 and transcriber ran twice**

Output from the reproducer:
```
OLD CODE RESULT:
  resp1: 200 -> {'text': 'fake transcription', 'engine': 'mac'}
  resp2: 200 -> {'text': 'fake transcription', 'engine': 'mac'}
  statuses: {200}
  transcriber called 2 times
CONFIRMED: Old code returned BOTH 200 (statuses={200}).
```

The assertion `assert 503 in statuses` WOULD FAIL on the old code. The test correctly catches the race.

### 3. AC-13 — `/health` Not Blocked by Lock

Test: `test_health_not_blocked_by_lock` at `tests/unit/test_server_singleflight.py:176`

Result: **PASSED** (confirmed in both targeted run and full suite).

Design analysis: The lock is now held during upload read + decode + transcription. The test uses a 1-second blocking decode. The health endpoint reads `model_loaded_flag.is_set()` (a `threading.Event`) and never touches `_lock`. The blocking decode runs in `run_in_executor` (thread pool), keeping the event loop free for concurrent `/health` requests. The test asserts health response time `< 0.5s` while the lock is held — verified.

AC-13 is not regressed by the lock-during-upload design.

### 4. §A Order — `test_model_busy_beats_model_not_loaded__G1_spec_A`

Test: `tests/unit/test_server_singleflight.py:111`

Result: **PASSED** (confirmed in both targeted run and full suite).

The new lock structure acquires the lock at step 2 (before model_loaded check at step 3). When both the lock is held AND the model flag is unset, the second request hits `lock.locked() == True` at step 2 and returns 503 `model_busy` — never reaching step 3 (`model_not_loaded`). §A ordering is preserved.

### 5. No Regression / No Weakened Tests

- Previous passing count: 357 (post-iteration-2)
- Current passing count: 358 (+1: `test_concurrent_transcribe_race_proof__R3_1_AC14`)
- No tests deleted, no test bodies weakened
- `test_concurrent_transcribe_503_model_busy` (pre-existing, [AC-14, E-10, F-3]): still passes
- Full suite: `358 passed, 1 deselected, 1 xfailed` — the deselected and xfailed counts are unchanged

### 6. Minor Observation: Stale Comment in Test

Location: `tests/unit/test_server_singleflight.py:270`

```python
# We wrap the real read so real data is still returned (Starlette still
# needs to parse the multipart body for the first request).
```

The comment at line 270 also says "both requests will have called wait_for(lock.acquire(), 0)" — this references an earlier design from the fix-list's suggested `asyncio.wait_for(lock.acquire(), timeout=0)` pattern. The actual implementation uses `await lock.acquire()` (simpler, equivalent for the uncontested case). The comment is a stale artifact from the design document. The test logic itself is correct; this is purely a documentation nit.

---

## Findings Table

| Sev | Location | Finding | Action |
|-----|----------|---------|--------|
| Minor | `tests/unit/test_server_singleflight.py:270` | Comment references `wait_for(lock.acquire(), 0)` pattern from the fix-list design suggestion; actual implementation uses `await lock.acquire()` directly. Stale comment, no impact on correctness or test validity. | Update comment to reflect actual implementation pattern on next convenient commit. Does not block. |

No blockers. No majors.

---

## AC Coverage Summary

ACs in scope for this iteration: AC-14 (single-flight 503), AC-13 (health latency), and §A step-2 ordering.

| AC | Test | Status |
|----|------|--------|
| AC-14 / E-10 | `test_concurrent_transcribe_503_model_busy` [AC-14, E-10, F-3] | VERIFIED |
| AC-14 / E-10 / R3-1 | `test_concurrent_transcribe_race_proof__R3_1_AC14` [R3-1, H-1, AC-14, E-10] | VERIFIED |
| AC-13 / E-11 | `test_health_not_blocked_by_lock` [AC-13, E-11] | VERIFIED |
| §A step-2 order | `test_model_busy_beats_model_not_loaded__G1_spec_A` [G-1, §A step-2] | VERIFIED |

All 4 targeted ACs/criteria verified. 3 of 3 mandatory tests (AC-13, AC-14/R3-1, §A order) pass.

---

## Suite Summary

| Metric | Value | Threshold | Result |
|--------|-------|-----------|--------|
| Tests passing | 358 | ≥358 | PASS |
| Coverage | 86.76% | ≥80% | PASS |
| Blockers | 0 | 0 | PASS |
| Majors | 0 | 0 | PASS |
| Minors | 1 | — | NOTE |
| Race test fails on old code | YES (both 200, transcriber×2) | Required | PASS |
| Race test passes on new code | YES (one 503, transcriber×1) | Required | PASS |
| AC-13 not regressed | YES | Required | PASS |
| §A order preserved | YES | Required | PASS |

---

**Verdict: PASS**

The H-1 / R3-1 TOCTOU fix is structurally sound. The new race-proof test (`test_concurrent_transcribe_race_proof__R3_1_AC14`) genuinely reproduces the bug: empirically confirmed to produce two 200 responses on the old racy pattern (transcriber called twice) and one 503 + one 200 on the new atomic pattern (transcriber called once). AC-13 and §A ordering are not regressed. Full suite is green at 86.76% coverage with 358 passing tests.
