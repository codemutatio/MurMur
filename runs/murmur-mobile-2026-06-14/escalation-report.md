# Escalation Report

**Run ID:** murmur-mobile-2026-06-14 · **Date:** 2026-06-14 · **Status:** ESCALATED — nothing shipped

The validation loop hit its 3-iteration cap with **one unresolved blocker** (a server concurrency bug). This report gives full, honest context so you can decide what happens next. **Everything else in the build is green and verified** — this is a single, well-characterized, fixable issue, not a systemic failure.

## Where the build actually stands
- **Component A (server):** 357 tests pass, coverage 86.74%, ruff + mypy(strict) clean.
- **Component B (Android):** 188 tests pass, `assembleDebug` + lint clean, JaCoCo **92.2%** overall (engine 89.4%, all packages ≥80% — independently verified from the report).
- **61/61 acceptance criteria** have a proving test or manual-runbook step (9 device ACs are on-device by nature).
- Of the **3 blockers + 9 majors** found in iteration 1 and the **3 majors** found in iteration 2 — **all are closed except one**, which was re-broken by the fix for another.

## Unresolved Findings
| # | Sev | Origin | Location | Problem | Why still open |
|---|-----|--------|----------|---------|----------------|
| R3-1 | **Blocker** | code-review (iter 3) | `src/murmur/server/app.py:201→265` | **TOCTOU race in the single-flight lock.** The busy-check `if lock.locked()` (line 201) and the `async with lock:` acquire (line 265) are separated by real `await` file-reads (lines 213, 221, reading the upload for the size check). Two concurrent `/transcribe` requests can both pass line 201, yield at the file-read awaits, and both reach the acquire — the **second queues on the lock instead of getting an immediate 503 `model_busy`**, violating AC-14 / E-10 / ADR-2 ("0-second busy-wait, no waiters"). | This is the third appearance of the same underlying problem. The §A ordering requirement and the atomicity requirement are in tension under the current validation-order design, and the two fixes attempted (iter-2 F-3, iter-3 G-1) each satisfied one and broke the other. The iteration cap is now reached. |
| (minor) | minor | test-eng | `RecordingService.saveNoteAtomically` | E-33 Room-insert-failure→file-delete rollback not driven by a real test (needs an injectable-DAO seam) | downgraded to minor in iter 2; non-blocking |
| (minor) | minor | security | `SettingsRepository`/UI | `isEncryptionFallback` flag exists but no UI warns the user when secrets fall back to unencrypted storage | non-blocking |

### Practical blast radius of R3-1 (important for your decision)
The `async with lock:` still genuinely serializes the actual transcription — so the bug does **not** cause double-transcription, model memory pressure, data loss, or a crash. Its only effect: under two genuinely-concurrent requests (e.g. two notes dispatched at once by WorkManager), the second request **waits** (the connection long-polls until the first transcription finishes) instead of getting an immediate `503` + WorkManager backoff. For a single-user personal tool this is a bounded annoyance, not a safety failure — but it does violate an explicit acceptance criterion (AC-14) and the pipeline's "zero open blockers to ship" bar.

## Iteration History

### Iteration 1 — 3 blockers, 9 majors
Reviewers caught defects the green unit suites masked: **the WorkManager job was never enqueued from production** (transcription dead-on-arrival), the stale-`transcribing` reset was dead code with a Flow-hang bug, and the **single-flight lock had a TOCTOU race** (CR-3). Plus 9 majors: uncapped backoff, a regex JSON parser that truncated transcripts with embedded quotes, delete-order orphaning audio, dispatcher/interface coupling, a two-step retention write, a `python-multipart` CVE, an unguarded `host` bind, and two tautological recording tests. → fix-list-1 (F-1..F-12).

### Iteration 2 — 0 blockers, 3 majors (all new regressions from the fixes)
The 3 blockers + 9 majors were all addressed, but the fixes introduced three new majors: **NV-1** — the F-3 atomicity fix inverted the §A validation order (`model_not_loaded` before `model_busy`); **NV-3** — the F-7 interface fix left the dispatcher constructor bound to concrete types (AC-42 still unmet, masked by a concrete-class mock); **TE2-M-1** — the F-5 correctness fix (switching to `org.json`) forced engine tests onto Robolectric and dropped engine JaCoCo to 16.8% (the implementer's "still measured" claim was verified false). → fix-list-2 (G-1..G-5), scoped tightly with a "no new behavior" constraint.

### Iteration 3 — 1 blocker (final)
G-2 (interface typing), G-3 (engine coverage restored to 89.4% via `testImplementation org.json` + pure-JVM tests), G-4 (retry 30s), G-5 (two missing security tests) all genuinely closed. **But G-1 — fixing the §A order — moved the busy-check back above the file-read awaits, re-opening the TOCTOU (R3-1).** The implementer's code comments asserted atomicity that does not hold.

## Disputed Items
**R3-1 — the three reviewers disagreed**, and the disagreement decided escalate-vs-proceed, so the orchestrator arbitrated by reading the code directly:
- code-reviewer: TOCTOU reintroduced (awaits at 213/221 between busy-check 201 and acquire 265) → BLOCKER.
- security-reviewer: "no await between busy-check and acquire" → PASS.
- test-engineer: "no await between them; concurrent-503 test passes" → PASS.
- **Orchestrator ruling (read `app.py:201-265`): the code-reviewer is correct.** The `await file.read(...)` calls at lines 213 and 221 are unambiguously between the busy-check (201) and the acquire (265). The code's own comment claiming otherwise is false. The security-reviewer checked auth-ordering (fine, but not the relevant span); the test-engineer's passing test uses tiny in-memory payloads whose `await file.read()` does not yield to the event loop, so it cannot reproduce the race — exactly the masking the code-reviewer warned about. Per the operating rules, the reviewer who is right on a correctness fact wins.

## Root-Cause Assessment
**Primarily a plan/contract gap, compounded by implementation oscillation — not a bad spec and not a systemic failure.**

1. **Contract gap (the deep cause).** Plan §A specifies an 11-step order where the single-flight busy-check is *step 2* (early, for a fast 503) but the actual lock acquisition is *step 8* (after reading the upload to validate size/type). Reading the upload requires `await`s. ADR-2 specified `asyncio.Lock` + "0-second busy-wait, no waiters" but did **not** specify *how* to make a step-2 check and a step-8 acquire atomic across those awaits. The naive "`if locked(): 503` … later `async with lock`" pattern is inherently racy under this ordering. Neither the spec nor the plan flagged that "fast busy-check at step 2" and "atomic acquire at step 8" cannot both hold with awaits between them — so the implementer kept trading one for the other.

2. **Implementation quality (the proximate cause).** Across two attempts the implementer satisfied exactly one of {§A order, atomicity} and broke the other, and on the final attempt wrote comments *claiming* both held when they did not. The proving tests used sub-spool-size payloads that never trigger the threadpool yield, so they passed without exercising the race — a test-design blind spot.

3. **Genuinely subtle.** The architect's own ADR-2 called this "the single subtlest server defect." It earned the label: it survived three iterations and split the three reviewers.

The correct fix is a small, localized design refinement (not a re-plan): replace check-then-later-acquire with an **atomic non-blocking acquire at step 2** — attempt a zero-timeout `lock.acquire()`; on contention return `503 model_busy` immediately; on success hold the lock through validation + transcription and release in `finally`. This satisfies §A ordering *and* atomicity. Cost: the lock is held during the upload read (fine for a single-flight, single-user server). It needs a test that uses a real >1 MB payload (or a patched UploadFile) so the `await` genuinely yields and the race is actually exercised.

## Options
1. **Retry validation once (reset the counter), with a one-item fix-list for R3-1.** Sensible because: the blocker is singular, fully diagnosed, and has a known-correct ~1-function fix; everything else is green and verified; a re-plan would be disproportionate. Risk: low — but the last two "small fixes" each regressed something, so the fix must come with a test that *actually reproduces the race* (real >1 MB payload) before re-review.
2. **Revise the plan (re-run from Stage 2) to amend ADR-2/§A** with the atomic-acquire pattern, then re-implement that one task. Sensible if you want the contract itself corrected and traceable. Heavier than needed for a one-function change, but it fixes the documented root cause, not just the code.
3. **Accept R3-1 as a documented known-limitation and ship anyway.** Defensible *only* because the practical blast radius is bounded (a queued wait, never a double-run or data loss) and this is a personal single-user tool. But it knowingly violates AC-14 and the pipeline's zero-open-blockers bar — so it forfeits the gate guarantee, and I would not do it without your explicit instruction.
4. **Abandon the run.** Not warranted — the work is sound and ~99% complete.

**Recommendation: Option 1.** Authorize one targeted fix iteration for R3-1 only (atomic step-2 acquire + a race-reproducing test using a real >1 MB upload), then re-run just the code-reviewer (and a quick test-engineer concurrency check). I'd also fold the amended pattern back into ADR-2/§A so the contract reflects reality (a light touch of Option 2). The rest of the system is already validated; this closes the last gap honestly.
