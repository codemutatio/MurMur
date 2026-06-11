# Validation Report — Code Review (Iteration 2)

**Run ID:** murmur-2026-06-11 · **Iteration:** 2 of 3 · **Date:** 2026-06-12
**Reviewer:** code-reviewer-2 (fix verification)
**Base commit:** 208a417 · **Fix commits reviewed:** dd923db, 6abb61a, 3d9d8a8, fba6ed2, bce1326, 37a198f, 95d6586, 6faae07

---

## Per-Fix Verification

| F-item | Verdict | Notes |
|--------|---------|-------|
| F-1 | PASS | `writer.py:55` calls `start_at.astimezone()` before `%H:%M` formatting. `test_line_format_local_time` now asserts exact `start.astimezone():%H:%M` value, not regex. `test_writer_local_time_not_utc` passes (previously failing). Fix is correct and complete. |
| F-2 | PASS | `retention.py:41` guard is now `<= 0`, with the negative-days branch adding a warning log. `test_retention_negative_days_is_noop` passes. Semantics are exactly right: 0 = keep-forever (silent), negative = misconfiguration (logged no-op). |
| F-3 | PASS | `test_engine.py:141` now asserts `icon_under_disk_low == IconState.ERROR`. The snapshot is taken while the engine is running under disk-low, before `stop.set()`. Recovery notification assertion retained. Fix is correct. |
| F-4 | PASS — with one minor observation (see findings table) | `_process` now wraps `parse_segment_start` in `try/except ValueError` and returns on bad filename. The live loop's `try/except Exception / finally task_done()` pattern is correct. 50ms sleep between retries is correctly placed before `continue`. The outer belt-and-suspenders guard on the live loop body correctly catches any exception that escapes `_process`. Three new tests cover all three sub-items. The bad-filename skip avoids an infinite loop: startup scan iterates a static snapshot from `list_spool`, so the bad file is encountered once per launch, not re-encountered mid-scan. |
| F-5 | PASS | `engine.py:206–215` wraps the entire `while` body in `try/except Exception` with 1s sleep and log. Stop-event path is not affected: `continue` inside a `try` block returns to the loop header and re-evaluates `not self._stop_event.is_set()`. `AudioSourceError` path (inner guard at line 159) is not double-handled: `AudioSourceError` is now unreachable by the outer guard because the inner `try/except AudioSourceError` at line 152–164 catches it first. Test injects `RuntimeError` through `write_segment_wav` and verifies the thread does not die. |
| F-6 | PASS | `self._dropped_frames` and `dropped_frames` property removed entirely. Callback closure now uses `contextlib.suppress(queue.Full)` and touches nothing but `frames_q`. `test_sounddevice_source_callback_owns_no_instance_state_f6` asserts both attributes are absent. ADR-1 conformance restored. No callers of `dropped_frames` existed. |
| F-7 | PASS | `segmenter.py:184` applies `split_idx = max(1, int(np.argmin(rms_values)))`. With `split_idx >= 1`, `part1_frames = frames[:split_idx+1]` always contains at least 2 frames. `part2 = frames[split_idx+1:]` is at most `_max_frames - 2` frames, so one more push yields `_max_frames - 1` — below the force-split threshold. The pathological re-split case is structurally prevented. `test_force_split_uniform_rms_no_1_frame_segment_f7` confirms no segment shorter than 2 frames. |
| F-8 | PASS — with one minor observation (see findings table) | `runtime.py:97` uses `queue.Queue(maxsize=256)`. `_emit_segment` uses `put_nowait` with `queue.Full` catch that logs a warning. `set_queue_depth(self._work_q.qsize())` is called unconditionally after the put attempt — when the put is dropped, `qsize()` returns 256 (full), which is the accurate observable state. Recovery story is sound: WAV is on disk, startup scan picks it up on next launch, disk-low gate independently caps spool growth. No deadlock/livelock risk: the put-drop path is non-blocking and the worker drains independently. |

---

## Code Review (Iteration 2)

| Sev | Location | Finding | Suggested action |
|-----|----------|---------|------------------|
| **Minor** | `src/murmur/transcribe/worker.py:110–118` | Bad-filename files are skipped and logged, but never removed from disk. On every subsequent process launch, `list_spool` will return the bad file again, the startup scan will skip it again. Over weeks of restarts this accumulates. The fix is correct per the spec (spec does not say to delete non-conforming files), but the behavior is worth documenting. | Either delete the non-conforming file after logging the warning (if policy is "spool should only contain canonical WAVs"), or add a note in the skip-log that the file will be re-skipped on each restart so operators can investigate. Not a blocker — the accumulation rate is bounded by how often bad files are planted, not by normal operation. |
| **Minor** | `src/murmur/audio/segmenter.py:182` | The comment at line 182 says "so part 1 always contains at least one frame" but the actual lower bound is 2 frames (indices 0 and 1 when `split_idx = 1`). The test at `test_segmenter.py:266` correctly checks `>= 2 * FRAME_SAMPLES / SAMPLE_RATE`, so the code and test are consistent; only the comment is wrong. | Change comment to "at least 2 frames (indices 0…split_idx)". |

---

## Iteration-1 Minors — Status Verification

All 8 iteration-1 minors were deliberately not fixed. Confirmed none have mutated into something worse:

| Minor (iter-1) | Location | Still minor? |
|---------------|----------|-------------|
| 1 — redundant `or text in FILLER_PHRASES` branch | `guard.py:65` | Yes — still dead code for non-lowercase input; no behavioral change introduced by fixes. |
| 2 — unused `_PRE_ROLL_FRAMES` / `_POST_ROLL_FRAMES` module constants | `segmenter.py:21–22` | Yes — still unreferenced. The F-7 diff touched `segmenter.py` but did not add references to these constants. |
| 3 — `lines` list assembled but never wired to the menubar | `menubar.py:96–102` | Yes — `lines` is still computed and discarded. R-11 queue-depth display is still absent. Unchanged by fixes. |
| 4 — `_marker_path` places marker in HF cache root, not MURMUR\_HOME | `models.py:158–160` | Yes — unchanged. Still a plan-contract mismatch (minor). |
| 5 — `notifier: object` annotation in `CaptureEngine.__init__` | `engine.py:51` | Yes — still `object`, still `# type: ignore[attr-defined]` on every call site. Unchanged by F-5 fix. |
| 6 — `notifier: object` annotation in `Controller.__init__` | `control.py:193` | Yes — unchanged. |
| 7 — `_emit_segment` local import + redundant `assert isinstance` | `engine.py:232–234` | Yes — still present. The F-8 diff added `return` after the OSError path but did not touch the import/assert. |
| 8 — `prune_transcripts` called with `date.today()` not injected `now` | `runtime.py:124` | Yes — still uses `date.today()`. Testability minor remains. |

No minor has escalated.

---

**Verdict:** PASS

No blockers. No majors. Two new minors introduced by the fixes (non-conforming file accumulation; inaccurate comment). All 5 iteration-1 majors are correctly resolved. All 8 iteration-1 minors are unchanged and still minor-level.
