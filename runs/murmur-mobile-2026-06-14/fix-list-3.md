# Fix List — Iteration 3 (post-escalation targeted retry)

**Run ID:** murmur-mobile-2026-06-14 · **Source:** validation-report.md iteration 3 (R3-1) · **Date:** 2026-06-14

Human authorized a one-time counter reset for a SINGLE targeted fix. Fix **only R3-1**. Do NOT touch anything else — the rest of the system is green and validated (server 357 tests, android 188 tests, 92.2% coverage, all other findings closed). Do not refactor, do not change other behavior, do not weaken any passing test.

| # | Sev | Origin | Location | Problem | Required outcome |
|---|-----|--------|----------|---------|------------------|
| H-1 | blocker | code-review (R3-1) | MurMur `src/murmur/server/app.py` (`POST /transcribe`, ~lines 188-272) | TOCTOU race: the `if lock.locked()` busy-check (line 201) and the `async with lock:` acquire (line 265) are separated by real `await file.read(...)` calls (lines 213, 221). Two concurrent requests can both pass the busy-check, yield at the file-read awaits, and both reach the acquire — the second queues instead of getting an immediate 503 `model_busy`. Violates AC-14 / E-10 / ADR-2. (Prior comments claiming "no intervening await" were false — orchestrator confirmed by reading the code.) | Make the single-flight gate genuinely atomic AND keep §A ordering (busy → immediate 503 is the first post-auth gate). Recommended design: replace the step-2 `lock.locked()` *check* with an **atomic non-blocking acquire** at §A step 2 — e.g. `if lock.locked(): return 503` immediately followed (no await between) by `await lock.acquire()`, OR `try: await asyncio.wait_for(lock.acquire(), timeout=0) except TimeoutError: return 503` — then hold the lock through validation + ffmpeg + transcription and release in a `finally`. Net effect: a busy server returns 503 *immediately* (before reading the upload), and there is NO window where two requests both proceed. Restructure the handler so auth (step 1) → atomic acquire-or-503 (step 2) → model-loaded (step 3) → file/size/type validation (steps 4-7, now under the lock) → transcribe (step 8) → release in finally. |

## Required proving test (this is non-negotiable — the prior tests did NOT reproduce the race)
Add a concurrency test that **actually exercises the race**, not a tiny in-memory payload:
- Fire TWO genuinely concurrent `POST /transcribe` requests where the first holds the model/lock in a slow transcription (slow `FakeTranscriber`), and assert the second returns **503 `model_busy`** — with NO artificial `sleep` ordering the requests, and using a payload/UploadFile that genuinely yields at the read `await` (e.g. a >1 MB body so Starlette spools to a real file and `await file.read()` hands control to the event loop, OR an UploadFile/stream whose `read()` is patched to `await asyncio.sleep(0)` so the yield is deterministic). The test must FAIL against the current racy code and PASS after the fix.
- Keep/repair the existing `test_concurrent_transcribe_503_model_busy` and `test_health_not_blocked_by_lock` (AC-13 — `/health` must still be fast while the lock is held; confirm the new lock-during-upload design doesn't block `/health`).
- Confirm §A ordering still holds: `test_model_busy_beats_model_not_loaded__G1_spec_A` (busy wins over model_not_loaded) still passes.

## Consider (note in implementation-notes, don't expand scope)
The recommended design holds the lock during the upload read. For this single-flight, single-user, Tailscale-only server that is acceptable (only one transcription at a time anyway; a busy server 503s immediately, which is exactly what the phone's WorkManager wants). Briefly note this rationale in implementation-notes so the reviewer/doc-writer can capture it (a one-line ADR-2 addendum is welcome but optional — do not re-plan).

## After fixing — required green state (paste it)
- Server: `uv run pytest` (≥ prior 357 passing + your new concurrency test; coverage ≥80), `uv run ruff check`, `uv run mypy src/murmur` — all clean.
- Confirm `/health` still answers <500 ms while a transcription holds the lock (AC-13 not regressed by lock-during-upload).
- Append a "Post-escalation fix (H-1 / R3-1)" section to implementation-notes.md: the new lock design, why it's atomic, and the test that proves it (including how the test actually triggers the yield). Commit with a conventional message, e.g. `fix(server): atomic single-flight acquire at step 2 — close TOCTOU [H-1, R3-1, AC-14]`.
- Do NOT modify the Android repo. Do NOT change any other server behavior.