# Validation Report — Test Engineering

**Run ID:** murmur-2026-06-11 · **Iteration:** 2 of 3 · **Date:** 2026-06-12

Severity rubric: `/Users/faithomojola/.claude/plugins/cache/jay-flows/e2e-agent-flow/0.2.0/pipeline/gates/review-checklist.md`

Fix-list reviewed: F-1 through F-8 (fix commits dd923db..6faae07)

## Test Engineering

**Suite (before new tests):** 225 passed, 1 xfailed, 84.72% coverage  
**Suite (after new tests):** 233 passed, 1 xfailed, 84.72% coverage  
**AC coverage:** 21 of 21 AUTO ACs verified (all previously failing ACs now genuinely verified)  
**New tests added:** 8 (in `tests/unit/test_validation_iter2.py`)  
**Coverage:** 84.72% total (above 80% floor)  
**Lint:** ruff — clean. mypy — clean.

---

## Per-Fix Verification Table

| F-item | Proving test(s) exist | Tests pass | Assertions genuinely prove requirement | Notes |
|--------|-----------------------|------------|----------------------------------------|-------|
| F-1 | `test_writer.py::test_line_format_local_time` (rewritten), `test_validation_iter1.py::test_writer_local_time_not_utc` | PASS | Yes — `test_line_format_local_time` now asserts `rendered == start.astimezone():%H:%M` (exact value, not regex). `test_writer_local_time_not_utc` passes. `writer.py:55` has `local_start = start_at.astimezone()`. | FIXED |
| F-2 | `test_validation_iter1.py::test_retention_negative_days_is_noop` | PASS | Yes — asserts file exists AND `deleted == []` for `retention_days=-1`. Guard at `retention.py:41` is `<= 0`. | FIXED |
| F-3 | `test_engine.py::test_disk_low_pauses_then_resumes_with_notifications` | PASS | Yes — test now asserts `icon_under_disk_low == IconState.ERROR` (captured mid-run, not post-stop). Notification assertion retained. | FIXED |
| F-4 | `test_worker.py::test_unexpected_exception_does_not_kill_worker_loop_f4`, `test_worker.py::test_bad_filename_in_spool_is_skipped_f4`, `test_spool.py::test_parse_segment_start_raises_on_bad_filename_f4` | PASS | Yes — `MemoryError` path: worker continues, error count incremented, second WAV processed. Bad-filename path: good WAV still processed. ValueError surface: proven. | FIXED |
| F-5 | `test_engine.py::test_unexpected_exception_does_not_kill_capture_thread_f5` | PASS | Partially — test proves thread exits cleanly (not killed by the exception). Exception injection point is `write_segment_wav`, not `source.read()`. New iter-2 test covers `source.read()` path. See Minor finding TE2-1. | FIXED (with minor gap noted) |
| F-6 | `test_source.py::test_sounddevice_source_callback_owns_no_instance_state_f6` | PASS | Yes — asserts neither `_dropped_frames` nor `dropped_frames` exist on the instance. `source.py` callback has only `put_nowait` with `contextlib.suppress(queue.Full)`. | FIXED |
| F-7 | `test_segmenter.py::test_force_split_uniform_rms_no_1_frame_segment_f7` | PASS | Yes — uniform RMS input (argmin returns 0 without fix), asserts no segment shorter than 2 frames. `segmenter.py:184` uses `max(1, int(np.argmin(...)))`. | FIXED |
| F-8 | `test_engine.py::test_emit_segment_drops_gracefully_when_work_q_full_f8` | PASS | Yes — pre-fills a maxsize=1 queue, calls `_emit_segment`, asserts no crash and queue depth unchanged. `runtime.py:97` has `Queue(maxsize=256)`. Recovery story verified by new iter-2 test `test_f8_dropped_wav_recoverable_by_list_spool`. | FIXED |

---

## AC Coverage Audit (post-fix)

| AC | Mapped test(s) | Status |
|----|----------------|--------|
| AC-1 | `test_worker.py::test_success_deletes_wav` | VERIFIED |
| AC-2 | `test_worker.py::test_keep_audio_retains_wav` | VERIFIED |
| AC-3 | `test_guard.py::test_drops_high_no_speech_prob`, `::test_drops_filler_phrase` | VERIFIED |
| AC-4 | `test_writer.py::test_line_format_local_time` — now asserts exact local HH:MM value, not regex only | **VERIFIED** (was NOT VERIFIED in iter-1) |
| AC-5 | `test_writer.py::test_no_file_until_first_segment` | VERIFIED |
| AC-6 | `test_single_instance.py::test_second_instance_exits_fast` | VERIFIED |
| AC-7 | `test_worker.py::test_fallback_off_leaves_wav_increments_error_no_api` | VERIFIED |
| AC-8 | `test_worker.py::test_third_failure_invokes_fallback` | VERIFIED |
| AC-9 | `test_retention.py::test_prunes_8d_keeps_7d` | VERIFIED |
| AC-10 | `test_retention.py::test_zero_keeps_forever` | VERIFIED |
| AC-11 | `test_cli.py::test_cat_today_stdout` | VERIFIED |
| AC-12 | `test_cli.py::test_copy_today_pbpaste_roundtrip` | VERIFIED |
| AC-13 | `test_segmenter.py::test_force_split_at_quietest_point_121s` | VERIFIED |
| AC-14 | `test_segmenter.py::test_1_6s_gap_two_segments` | VERIFIED |
| AC-15 | `test_segmenter.py::test_pre_post_roll_within_50ms` | VERIFIED |
| AC-16 | `test_state.py::test_rest_of_day_persists_before_midnight` | VERIFIED |
| AC-17 | `test_state.py::test_expires_after_midnight` | VERIFIED |
| AC-18 | `test_prompt.py::test_hints_verbatim` | VERIFIED |
| AC-19 | `test_engine.py::test_disk_low_pauses_then_resumes_with_notifications` — now asserts `IconState.ERROR` | **VERIFIED** (was PARTIALLY VERIFIED in iter-1) |
| AC-29 | `test_launchd.py::test_plist_plutil_lint` (xfail rationale unchanged, accepted in iter-1) | VERIFIED |
| AC-30 | `test_config.py::test_values_applied` | VERIFIED |

---

## Findings

| Sev | ID | Location | Finding | Suggested action |
|-----|----|----------|---------|------------------|
| Minor | TE2-1 | `tests/unit/test_engine.py:342` (`test_unexpected_exception_does_not_kill_capture_thread_f5`) | **F-5 test docstring says "from the source" but injection is into `write_segment_wav`.** The named F-5 proof test documents itself as proving that "an injected unexpected exception from the source does not terminate the loop" but it actually injects into `write_segment_wav` (called inside `_emit_segment`, which is inside the `for seg in segments:` loop). The `source.read()` path — the primary Risk-3 scenario cited in the fix-list — was not covered. My new test `test_capture_engine_survives_read_exception` covers this path and confirms the outer guard works there too. | Update the docstring to reflect the actual injection point. The gap in coverage is now closed by the iter-2 test. |
| Minor | TE2-2 | `src/murmur/transcribe/worker.py:110-118` | **Bad-filename WAV is never deleted from spool.** The F-4 fix returns from `_process()` without deleting the file or incrementing errors when `parse_segment_start` raises `ValueError`. The file stays in spool indefinitely, is re-encountered on every startup scan, and is re-skipped every time. This is not data-loss, but a permanently stranded file. Confirmed by my new test `test_bad_filename_wav_persists_and_is_reskipped_on_rerun`. | Either delete the bad-filename WAV after logging the warning (safest resolution), or move it to a quarantine subdirectory. Not a blocker because the worker doesn't fail; the file is benign but accumulates. |
| Minor | TE2-3 | `src/murmur/audio/engine.py:206-215` (outer guard) | **`stream_open` is not reset in the outer exception handler.** If an unexpected exception is thrown while `stream_open = True` (e.g., during `source.read()` at line 182), the outer guard catches it but `stream_open` remains True. On the next iteration, the "open stream if not open" block is skipped and the loop retries `read()` on what may be a broken stream. The 1s sleep prevents a catastrophic spin but repeated exceptions would loop at 1/s rather than closing and reopening the stream. | Reset `stream_open = False` and `last_device_sig = ""` in the outer except handler to force a clean reopen after any unexpected exception. This is the same pattern used in the inner device-change and watchdog handlers. |
| Minor | TE2-4 | `src/murmur/audio/engine.py:246-254` and `src/murmur/runtime.py:97` | **F-8 drop path deferred to next-launch recovery.** When `work_q` is full and a WAV's path is dropped from the in-process queue, the file stays in spool but will NOT be processed during the current session — only on the next launch (via startup scan). R-16 says "prior run" files are processed at launch; it does not cover same-run drops. Per spec R-16 wording this is acceptable, but user experience degrades: if the worker is slow/dead, WAV files accumulate silently and are only processed at restart. The fix-list asked for this to be flagged. | Accepted trade-off per implementation-notes. The disk-low gate (R-20) provides a natural cap. If the intent is to also retry during the current run, a background "spool reaper" periodic scan could be added in a future iteration. Document the current behavior explicitly in implementation-notes. |

---

## Regression Checks

No regressions found in the changed files:

- `writer.py`: `start_at.astimezone()` is called unconditionally; works for tz-aware and naive inputs. Confirmed by existing tests + new `test_writer_naive_datetime_uses_local_time`.
- `retention.py`: `<= 0` guard correctly preserves AC-10 semantics (`retention_days=0` is still a no-op). Confirmed by `test_retention_zero_still_noop_after_f2_fix`.
- `engine.py`: Outer try/except wraps the entire loop body without changing the gate ordering or the `stream_open` state machine (for the happy path). All existing engine tests pass.
- `segmenter.py`: `split_idx = max(1, ...)` lower-bound confirmed not to change split behavior on non-degenerate inputs (argmin != 0). All existing segmenter tests pass.
- `source.py`: Removal of `_dropped_frames` has no callers in the codebase (confirmed via grep). All existing source-related tests pass.
- `worker.py`: `except Exception` added after `except TranscriptionError` does not affect the `TranscriptionError` path (the first match wins). Sleep added between retries confirmed by existing AC-8 test.
- `runtime.py`: `Queue(maxsize=256)` — the engine uses `put_nowait` so the bounded queue cannot block the capture thread. All runtime tests pass.

---

## What I Checked (work evidence)

- Read all fix commits (dd923db..6faae07) via `git diff 208a417..HEAD -- src tests`.
- Read `writer.py`, `retention.py`, `engine.py`, `worker.py`, `source.py`, `segmenter.py`, `runtime.py` (all changed files).
- Read `test_validation_iter1.py` and verified git history: only `ruff I001` import-sort was applied, no assertions weakened.
- Read `test_writer.py`, `test_engine.py`, `test_worker.py`, `test_source.py`, `test_segmenter.py`, `test_spool.py` (all proving-test locations).
- Read `fakes.py` to understand FakeTranscriber raise semantics for F-4 test.
- Ran full suite: 233 passed, 1 xfailed, 84.72% coverage.
- Ran ruff and mypy: both clean.
- Verified the two iter-1 failing tests both pass (unmodified assertions).
- Mentally mutated 6 code paths: (1) writer astimezone() removal — caught by test_writer_local_time_not_utc; (2) retention `<= 0` back to `== 0` — caught by test_retention_negative_days_is_noop; (3) engine outer guard changed to `raise` — caught by test_unexpected_exception_does_not_kill_capture_thread_f5; (4) segmenter `max(1,...)` removed — caught by test_force_split_uniform_rms_no_1_frame_segment_f7; (5) source `_dropped_frames` re-added — caught by test_sounddevice_source_callback_owns_no_instance_state_f6; (6) work_q put_nowait reverted to put — no existing test catches a blocking call directly, but test_emit_segment_drops_gracefully_when_work_q_full_f8 would hang and time out, which counts as a catch.
- Added 8 new adversarial tests in `tests/unit/test_validation_iter2.py`.

**Verdict: PASS**

Blockers: 0  
Majors: 0  
Minors: 4 (TE2-1: F-5 test docstring mismatch; TE2-2: bad-filename WAV permanently stranded; TE2-3: stream_open stale on outer-guard exception; TE2-4: F-8 same-run drop deferred to next launch, flagged per fix-list instruction)  
New tests added: 8  
Coverage: 84.72% (above 80% floor)
