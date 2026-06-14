# Validation Report

**Run ID:** murmur-mobile-2026-06-14 · **Iteration:** post-escalation retry R1 (counter reset once by human authorization) · **Date:** 2026-06-14

Scope of this round: the single remaining blocker R3-1 (server single-flight TOCTOU). All other findings from iterations 1-3 were already closed. Detail in `review-code-reviewer-4.md`, `review-test-engineer-4.md`; security carried forward from `review-security-reviewer-3.md`.

## Test Engineering
**Suite:** Server 358 passed / 0 fail / 1 deselected(hardware) / 1 xfail, coverage **86.76%**. Android unchanged this round (188 passing, 92.2% — not rebuilt). · **AC coverage:** 61/61.
**Race-reproduction evidence:** the new `test_concurrent_transcribe_race_proof__R3_1_AC14` was independently verified to FAIL on the old check-then-later-acquire pattern (two concurrent requests → [200, 200], transcriber called twice) and PASS on the fix ([200, 503], transcriber called once). AC-13 (`/health` <500 ms while lock held) and §A order (`model_busy` beats `model_not_loaded`) both still pass. No regression; no test weakened/deleted.

| Sev | Location | Finding | Suggested action |
|-----|----------|---------|------------------|
| Minor | tests/unit/test_server_singleflight.py:~270 | stale comment references the `wait_for(lock.acquire(), 0)` design from the fix-list proposal; the implementation uses plain `await lock.acquire()` after a `lock.locked()` check | tidy the comment (non-blocking) |

**Verdict:** PASS

## Code Review
| Sev | Location | Finding | Suggested action |
|-----|----------|---------|------------------|
| — | — | R3-1 genuinely closed. **Atomicity:** zero `await` between `if lock.locked()` and `await lock.acquire()` (CPython fast-path returns without suspending on a free lock) — no concurrent request can proceed. **No lock leak:** acquire immediately followed by `try:`; all 9 early-return error paths + success + unhandled-exception release via `finally`. **§A order** + **AC-13** preserved; no other behavior changed; lock-during-upload acceptable for this single-flight, single-user, Tailscale-only server. Code comments now literally true. | — |

**Verdict:** PASS

## Security Review
**Carried forward from iteration 3 (PASS).** The R3-1 fix changes only the lock acquisition/hold pattern; it does not move work pre-auth (auth remains step 1, confirmed by code-reviewer this round), does not alter secrets handling, logging, the multipart/ffmpeg path, or any network surface. No new auth/secrets/injection/data-exposure/dependency surface introduced. Open carry-forward minor: CR-13 (encryption-fallback flag not wired to a user-visible UI warning) — non-blocking.

**Verdict:** PASS (carried forward; domain untouched by this fix)

## Iteration Outcome
**Overall:** PASS — all three reviewers PASS. Zero open blockers, zero open majors. Minors logged (test comment tidy; CR-13 fallback-warning UI; E-33 rollback test seam). **→ DOCUMENT (Stage 6).**
**Validation summary across the whole run:** iter 1 (3 blockers + 9 majors) → iter 2 (3 majors, all regressions from the fixes) → iter 3 (1 blocker R3-1, escalated) → human-authorized retry R1 (R3-1 closed). New tests added by reviewers across the run: ~40 (test-engineer) + the race-proof test. Final suites: server 358 passed / 86.76%; android 188 passed / 92.2%.
