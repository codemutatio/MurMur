# Validation Report — Security Review

**Run ID:** murmur-2026-06-11 · **Iteration:** 2 of 3 · **Date:** 2026-06-12

---

## Security Review

**Attack surface delta:** The fix diff (F-1 through F-8) shrinks the attack surface on two fronts: the worker thread can no longer be killed by a bad spool filename (F-4), and the in-process work queue is now bounded at 256 slots (F-8). No new external input channels, subprocess calls, network paths, or file-permission mutations were introduced. The set of files written to disk is unchanged. Log lines added by the fixes log only filenames, integer values (free bytes, retention_days), and exception messages — no transcript text, no key material, no PII. The fix diff is purely defensive within the existing single-user personal-tool threat model.

---

| Sev | Location | Finding | Attack scenario | Remediation |
|-----|----------|---------|-----------------|-------------|
| Minor (new) | `src/murmur/transcribe/worker.py:67–74` + `src/murmur/audio/spool.py:88–92` | **Bad-filename spool file is skipped but never cleaned up — repeated load-and-skip on every startup.** `_process()` now correctly skips files whose stems fail `parse_segment_start`, but it returns without deleting them (line 118). `list_spool()` returns all `*.wav` files; on each subsequent process restart the startup scan re-picks and re-processes the file: it calls `load_wav()` (line 102) first, loading the entire WAV into memory, before the `parse_segment_start` guard (line 111) fires and skips it. A non-conforming WAV placed in `~/Transcripts/spool/` will therefore consume memory proportional to its size on every launch for the lifetime of the file. Exploiting this requires write access to the spool directory, which is `755` and effectively owner-only. Within the single-user threat model this is low probability, but the cleanup gap means a large inadvertent file (e.g., a copied reference recording) could cause a noticeable memory spike on every restart indefinitely. The F-4 fix is correct and the thread-survival goal is fully met; this is a secondary gap left open by the skip-without-delete design. | On skip due to `ValueError`, also `unlink` the non-conforming file (or move it to a quarantine subdirectory for inspection). If deliberate preservation is desired, log the path and add a `keep_audio`-style option. Alternatively, re-order `_process()` to call `parse_segment_start` before `load_wav` so the memory load is skipped too. |

---

### Scoped verification results

**F-4 / iter-1 minor #2 (worker.py:101 unhandled ValueError):** Resolved. `parse_segment_start` is now wrapped in a `try/except ValueError` at `worker.py:110–118`; the skip logs `wav_path.name` and the exception message (no sensitive content). The outer `try/except Exception` in the live loop (`worker.py:88–93`) provides a second layer so that even if `_process` raises unexpectedly, `task_done()` is still called via `finally` and the loop continues. Tests `test_parse_segment_start_raises_on_bad_filename_f4` (test_spool.py) and the worker loop-body exception test (test_validation_iter1.py) confirm both layers. The thread-survival property is genuine.

**Weaponization check (skip-but-no-cleanup / resource exhaustion):** Partially addressed above as a new minor. The fix does not introduce unbounded re-processing on each loop tick — the startup scan runs exactly once per process launch (line 69, not inside the `while` loop). Worst case is O(N) startup time and memory proportional to the largest non-conforming WAV on each launch, not a continuous drain. Not a major.

**F-8 drop path (work_q full):** The queue is bounded at 256 (runtime.py:97). When full, `_emit_segment` calls `put_nowait`, catches `queue.Full`, and logs `wav_path.name` only (no path traversal, no transcript text). The WAV file is already on disk before the failed enqueue; the comment and code correctly document the recovery story (startup scan on next launch). Disk accumulation during worker backoff is bounded by the disk-low gate (Gate 3) which pauses capture when free space drops below 2 GiB — user is notified via menubar and osascript before file accumulation can exhaust the disk. No unbounded disk accumulation path without a user-visible signal.

**F-1 / astimezone (writer.py:55):** `start_at.astimezone()` has no security angle. `astimezone()` is a stdlib datetime method with no I/O, no external call, and no injection surface. Confirmed non-issue.

**F-5 outer guard and mic-permission masking:** The `except Exception` at `engine.py:206` wraps the entire loop body. Gate 2 (`mic_permission`) itself calls `self._mic_permission()` which, in production, calls `microphone_permission()` from `permissions.py`. That function wraps all AVFoundation calls in its own `try/except` and returns `PermissionStatus.UNDETERMINED` on any import or runtime error — so a probe failure never escapes Gate 2's conditional to reach the outer guard. The outer guard therefore cannot silently mask a mic-denied state. If the probe raises despite its own guard (a pathological case), the outer guard would log the exception at EXCEPTION level and continue — the icon would retain its prior value, not silently reset to RECORDING. Not a security-relevant masking concern.

**Log content review:** All new log lines added by F-1 through F-8 were reviewed:
- `worker.py:113–117`: logs `wav_path.name` (filename, no content) and `exc` (ValueError parse message, no content).
- `worker.py:91`: logs `exc` string from unexpected worker exception (no audio/transcript data).
- `engine.py:249–252`: logs `wav_path.name` only.
- `engine.py:211–215`: logs `exc` string from capture exception (no audio data).
- `retention.py:45–48`: logs the integer `retention_days` value.
No transcript text, API keys, or PII appears in any new log line.

**New subprocess/network/permission-change check:** `git diff 208a417..HEAD -- src/murmur/` was filtered for `subprocess`, network imports, and `chmod`/`chown`/`os.chmod` patterns. Zero new subprocess calls, zero new network paths, zero file-permission mutations were introduced.

---

**Verdict:** PASS

No blockers. No majors. One new minor (skip-without-cleanup on bad spool filenames causing repeated load-on-startup). Four iter-1 minors (plist XML injection, marker-file trust, symlink retention, dependency pinning) remain logged and unchanged from iter-1; the fix diff made none of them worse.
