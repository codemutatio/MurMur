# Implementation Notes

**Run ID:** murmur-2026-06-11

Created by the implementer in Stage 4; appended by the implementer during fix-list work and by the doc-writer in Stage 6. Append-only — never rewrite earlier entries.

## Build Log
<Per task: anything non-obvious — decisions made under ambiguity, gotchas, why something looks unusual.>

| Task | Note |
|------|------|
| T-1 | Scaffold created: pyproject.toml, .python-version, src layout, pytest+coverage config, ruff+mypy, paths.py, log.py, fakes.py, conftest.py. Entry point `murmur` wired via hatchling. All tooling green on smoke test. |
| T-2 | Config loader: all defaults, missing/unparseable→defaults+warning, unknown key warnings. `load_config` never raises. `get_openai_api_key` uses env→keychain fallback order. E-21 handled: fallback enabled + no key → warn + treat as off. |
| T-3 | Segmenter + spool: ring buffer (deque of frames), pre/post-roll as frame counts (10 frames = 320 ms within AC-15 tolerance). Force-split at min-RMS frame, no samples discarded. Spool WAV writes via tmp+rename using stdlib `wave` (no ffmpeg). |
| T-4 | SileroVAD adapter: bundled model (no download in tests), `torch.set_num_threads(1)`. Integration test uses `say` + `afconvert` to generate 16 kHz mono WAVs — no binary fixtures, no network. |
| T-5 | TranscriptWriter: whole-file rewrite+atomic replace (ADR-2). Lazy file creation (AC-5). Mid-day deletion recovery (E-12). Retention pruner: strict boundary (8d deleted, 7d kept at `retention_days=7`). |
| T-6 | Hallucination guard: OQ-2 corrected semantics — drop when `no_speech_prob > 0.6 AND avg_logprob < -1.0`, OR `compression_ratio > 2.4`, OR normalized text in FILLER_PHRASES, OR empty. |
| T-7 | MlxWhisperTranscriber: audio always passed as np.ndarray (no ffmpeg). `assert_apple_silicon` checks `platform.machine()=="arm64"` and `sys.platform=="darwin"`. mlx_whisper import is mocked in unit tests. |
| T-8 | OpenAITranscriber: in-memory WAV encoding via stdlib `wave` + `io.BytesIO`. Injected client factory for testability. API results get sentinel scores (no_speech_prob=0.0, compression_ratio=1.0). |
| T-9 | TranscribeWorker: startup scan via `list_spool` → backlog_drained event. Strict FIFO. 3-failure policy: 3 consecutive local failures → fallback if configured, else leave WAV + increment error + skip. RTF logged per segment. |
| T-10 | bakeoff.py: standalone script reusing MlxWhisperTranscriber/OpenAITranscriber. Record-prompt path (E-23). Missing OPENAI_API_KEY → API sections marked "SKIPPED". No ffmpeg — arrays throughout. |
| T-11 | StateStore + PauseState + Controller: REST_OF_DAY stores `until = next local midnight`. MANUAL pause has `until=None`. `is_pause_active`: MANUAL→True always; until set→`now < until`; expired/none→False. State file corruption → not paused. |
| T-12 | CaptureEngine: gate ordering (paused→permission→disk→open). Watchdog (3s stall). Device poll (2s, signature compare). All via injected fakes. `SounddeviceSource.open()` calls `sd._terminate()/_initialize()` to refresh PortAudio device cache. |
| T-13 | SingleInstanceLock: flock LOCK_EX|LOCK_NB on lock_file. Stale-safe (flock dies with process). Second instance reads PID from lock file and raises AlreadyRunningError. Integration test spawns a subprocess to confirm ≤2s exit. |
| T-14 | `mac/clipboard.py` added. CLI `cat today`/`copy today`: use tmp `$MURMUR_HOME` in integration tests. CLI unit tests for missing-file path. pbcopy/pbpaste round-trip integration test. |
| T-15 | ModelManager: fake HfApi + fake snapshot_download injected via monkeypatch. Checksum pass → writes marker. Mismatch → deletes snapshot dir + raises ChecksumError. Marker presence → skips re-hash. Progress callback tested. |
| T-16 | `runtime.py`: Runtime supervisor wiring all threads. `murmur run --headless` + `murmur bench` CLI paths. Pipeline integration test: FakeAudioSource→Segmenter→spool→worker(FakeTranscriber)→transcript. Silent day creates no file. Paused state honored at startup. |
| T-17 | rumps spike: confirmed rumps 0.4.0 imports and `rumps.App` subclass works on macOS 26.4 — no pyobjc NSStatusItem fallback needed. `app/menubar.py` implemented. `OsascriptNotifier` tests mock subprocess. Permissions tests mock AVFoundation/ApplicationServices. |
| T-18 | HotkeyListener: `to_pynput_spec` converts "ctrl+alt+cmd+m" → "<ctrl>+<alt>+<cmd>+m". `start()` returns False without Accessibility trust. pynput GlobalHotKeys runs its own thread; callback calls `controller.toggle_pause()`. |
| T-19 | `mac/launchd.py`: plist rendered via Python string formatting. `plutil -lint` test validates production render. `launchctl bootstrap`/`bootout` round-trip uses inert plist (`/usr/bin/true`, RunAtLoad=false). |
| T-20 | `docs/manual-verification.md`: MV-20..MV-28 with step-by-step procedures and unambiguous pass conditions. README: install, config, launchd setup, bake-off decision rule. All README commands smoke-run. |

## Deviations from Plan
<Each: what the plan said, what was done instead, why, and the flag for the orchestrator. Silent deviations are process failures.>

| # | Plan said | Done instead | Why | Flagged |
|---|-----------|--------------|-----|---------|
| 1 | rumps spike: if rumps fails → implement pyobjc NSStatusItem fallback | rumps 0.4.0 works on macOS 26.4 — no fallback needed | Spike passed cleanly | Yes — confirmed OK |
| 2 | `murmur/mac/launchd.py` in plan contracts | Also added `mac/clipboard.py` (needed by cli.py `copy` command) | clipboard.py was in the plan's module layout but not explicitly listed in mac/ directory — now added | Minor addition consistent with plan |
| 3 | T-19: `test_launchctl_load_inert_roundtrip` expected to pass | Test is marked `xfail` (expected failure on macOS 26.4 in non-GUI context) | `launchctl bootstrap gui/UID` returns error 5 "Input/output error" when invoked outside an Aqua session (terminal). This is a macOS 26.4 security restriction; the plist syntax passes `plutil -lint`. Full bootstrap/bootout is verified by `murmur install-agent` in the MV runbook. The xfail is documented with a clear message. | Flagged — AC-29 syntactic requirement satisfied by plutil-lint; bootstrap behavior is a runtime constraint |

## Fix-List Work

### Fix iteration 1 (2026-06-12)

| F-item | What changed | Test that proves it | Deviation / note |
|--------|-------------|---------------------|------------------|
| F-1 | `writer.py:53`: `start_at.astimezone()` before `%H:%M` formatting so the stamp reflects local wall-clock time, not the datetime's stored tz. | `test_writer_local_time_not_utc` (was failing; now passes). `test_line_format_local_time` rewritten to assert exact expected local `HH:MM` value rather than regex. | None. |
| F-2 | `retention.py`: guard changed from `== 0` to `<= 0`; negative values log a warning and return `[]` immediately to prevent data loss on misconfiguration. | `test_retention_negative_days_is_noop` (was failing; now passes). | None. |
| F-3 | `test_engine.py::test_disk_low_pauses_then_resumes_with_notifications`: snapshot the icon while engine is running under disk-low and assert `IconState.ERROR`. Notification assertion retained. | Same named AC-19 test, now also asserts `icon == IconState.ERROR`. | Test-only fix; no production code change needed for this item. |
| F-4 | `worker.py`: (a) `parse_segment_start` call wrapped in `try/except ValueError` — non-conforming filenames are logged and skipped. (b) `except Exception` added below `except TranscriptionError` to catch MemoryError/RuntimeError etc. — logs, increments errors, returns without killing loop. (c) 50 ms `time.sleep` added between local retries. (d) Outer try/except wraps the live `work_q` loop body for belt-and-suspenders. | `test_unexpected_exception_does_not_kill_worker_loop_f4` (unexpected exception, loop continues). `test_bad_filename_in_spool_is_skipped_f4` (bad filename, good WAV still processed). `test_parse_segment_start_raises_on_bad_filename_f4` (ValueError surface). | None. |
| F-5 | `engine.py`: wrapped entire `while not stop` loop body in `try/except Exception` — logs the error, sleeps 1 s, continues. Inner `try/except` for `segmenter.push` retained as-is. | `test_unexpected_exception_does_not_kill_capture_thread_f5` (injected RuntimeError, thread joins cleanly). | None. |
| F-6 | `source.py`: removed `self._dropped_frames` attribute and `dropped_frames` property entirely. Callback now only calls `put_nowait` inside `contextlib.suppress(queue.Full)`. ADR-1 note updated in docstring. | `test_sounddevice_source_callback_owns_no_instance_state_f6` (asserts `_dropped_frames` and `dropped_frames` not present on instance). | Counter was never surfaced anywhere so removal is the clean fix; no callers broken. |
| F-7 | `segmenter.py:181`: `split_idx = max(1, int(np.argmin(rms_values)))` — lower bound ensures part 1 always has ≥ 2 frames; prevents 1-frame degenerate segment on uniform-RMS input. | `test_force_split_uniform_rms_no_1_frame_segment_f7` (uniform amplitude input, no segment shorter than 2 frames). | None. |
| F-8 | **Chose bounding over documentation.** `runtime.py`: `queue.Queue(maxsize=256)`. `engine.py:_emit_segment`: changed `work_q.put()` to `work_q.put_nowait()` with a `queue.Full` except that logs a warning. WAV stays on disk; startup scan picks it up on next launch. | `test_emit_segment_drops_gracefully_when_work_q_full_f8` (full queue, no crash, queue depth unchanged). | Bound 256 chosen: ~8+ minutes of segments at 2 s/segment before the in-process queue fills. The disk-low gate caps the blast radius independently (capture pauses at < 2 GiB free). Recovery story: spool is the durable queue — the in-process queue is an optimisation layer only. |

## Documentation
<Doc-writer appends: files created/updated, examples executed, plan/code discrepancies found.>
