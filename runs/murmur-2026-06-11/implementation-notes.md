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

**Doc-writer:** murmur-2026-06-11, iteration 2, 2026-06-12

### Files created / updated

| File | Action | Notes |
|------|--------|-------|
| `/Users/faithomojola/Documents/MurMur/README.md` | Rewritten (full replacement of stub) | Complete front door: what/install/permissions/quick-start/config reference/API key/launchd/CLI reference/bake-off/menubar/transcript format/model weights/state locations/troubleshooting/known limitations |
| `/Users/faithomojola/Documents/MurMur/docs/architecture.md` | Created | Thread model (ADR-1), data flow mic→VAD→spool→whisper→markdown, durability story (ADR-2), state files, device-change/sleep-wake mechanism, model checksum, hallucination guard constants, audio constants, startup sequence, notification attribution |
| `/Users/faithomojola/Documents/MurMur/CHANGELOG.md` | Created | Keep-a-Changelog format, v0.1.0 entry covering full feature set + known limitations |
| `/Users/faithomojola/Documents/MurMur/docs/manual-verification.md` | Fixed MV-20 step 1 | Procedure incorrectly used `bakeoff.py --duration 3600 --output bakeoff-60min.wav` — bakeoff.py's `--output` takes a `.md` path and the script does not save the recorded WAV to disk; replaced with a direct sounddevice recording snippet and a note explaining the constraint |

### Commands executed (gate: every example run)

| Command | Result |
|---------|--------|
| `uv run murmur --help` | PASS — full help text confirmed |
| `uv run murmur run --help` | PASS |
| `uv run murmur cat --help` | PASS |
| `uv run murmur copy --help` | PASS |
| `uv run murmur bench --help` | PASS |
| `uv run python scripts/bakeoff.py --help` | PASS |
| `uv run murmur cat today` against a fixture file | PASS — printed `# Transcript — 2026-06-12\n\n**[09:00]** Testing cat command.`, exit 0 |
| `uv run python3 -c "import tomllib; ..."` on `config.example.toml` | PASS — all keys round-tripped correctly |
| Config loader validation (`load_config(Path("config.example.toml"))`) | PASS — all values match expected defaults; no warnings |
| Plist render via `render_plist(...)` | PASS — valid XML with all required keys |
| `uv run pytest --tb=short -q` (before docs work) | PASS — 233 passed, 1 xfailed (pre-existing, AC-29 platform constraint) |
| `uv run pytest --tb=short -q` (after docs work) | PASS — 233 passed, 1 xfailed, 0 failed |

Note: The fixture file created during `cat today` verification was cleaned up before running the full test suite to avoid contaminating the `test_cat_missing_file_exit_1` tests, which rely on `~/Transcripts/YYYY-MM-DD.md` being absent.

### Plan vs. code discrepancies found

| # | Plan said | What the code actually does | Impact |
|---|-----------|----------------------------|--------|
| D-1 | `launchd/com.jay.murmur.plist.template` listed as a repo file in the repository layout | The `launchd/` directory exists but is empty. The plist is generated dynamically by `mac/launchd.py:render_plist()` at install time. There is no static template file. | None — the generated plist is correct and tested; the "template" was an architectural sketch that became code. |
| D-2 | MV-20 procedure (plan T-20 / existing docs) used `bakeoff.py --duration 3600 --output bakeoff-60min.wav` to create a 60-minute WAV | `bakeoff.py` does not save the recorded WAV to disk; it holds the audio in memory and transcribes it. `--output` takes a `.md` path. The command as documented would never produce a `.wav` file. | Fixed in `docs/manual-verification.md`. |
| D-3 | Plan contracts for `SttConfig` listed `engine: Literal["local","api"] = "local"` as a field | `config.py` does implement this field with exactly that default, but neither `runtime.py` nor any other production code reads `stt.engine` to switch modes — the fallback is always determined by `stt.api_fallback=true` and key availability, not by setting `engine="api"`. Setting `engine="api"` has no effect in the current code. | Minor aspirational surface — not documented as working. Config reference in README documents `api_fallback` as the operative switch; `engine` key is omitted from the README config reference since it has no runtime effect. |

## Post-ship fix — mic permission Catch-22 (live-hardware bug, 2026-06-13)

Surfaced on the owner's first live `murmur run` on the target MacBook Pro (the suite fakes every hardware boundary by design, so this could only appear on real hardware). Symptom: headless run sat at the mic gate forever, never opened the stream, never wrote a transcript, and the terminal app never appeared in System Settings → Privacy & Security → Microphone.

Root cause (two defects at the Gate-2 mic block in `src/murmur/audio/engine.py`):
1. **Catch-22:** when `microphone_permission()` returns `UNDETERMINED` (no prior TCC decision), nothing ever called `request_microphone_access()`. The function existed (`mac/permissions.py`) but had zero call sites — so the macOS prompt never fired, the app never registered with TCC, and the gate blocked indefinitely on a permission nothing requested.
2. **Silent failure (R-18 violation):** the gate only signalled via the menubar ⚠ icon and a single osascript notification — neither visible in a `--headless` run. No log line was emitted, so the blocked state was invisible in both terminal and `murmur.log`.

Fix: Gate 2 now (a) calls an injectable `request_mic_access` (defaults to the real `request_microphone_access`) exactly once when status is `UNDETERMINED`, and (b) logs a clear WARNING — distinct text for `UNDETERMINED` ("requested access, approve the prompt") vs `DENIED`/`RESTRICTED` ("enable in System Settings, then restart") — plus an INFO line when permission is later granted. One-shot flags (`requested_mic_access`, `notified_permission`) reset on grant to avoid prompt/log/notification storms.

Tests added (`tests/unit/test_engine.py`): `test_undetermined_requests_mic_access` (UNDETERMINED → request fired exactly once + notification surfaced) and `test_undetermined_then_granted_opens_stream` (UNDETERMINED → request once → GRANTED → stream opens, no restart). `_make_engine` extended with an injectable `request_mic_access` spy.

Verification: `uv run pytest` → 235 passed, 1 xfailed, coverage 84.82%; ruff check + format + mypy all clean.

Known residual (best-effort, noted not silent): `requestAccessForMediaType_completionHandler_` is invoked from the capture-engine (non-main) thread in a CLI with no AppKit run loop; the TCC prompt is presented out-of-process by macOS so this is expected to work, but if the prompt does not appear the WARNING log now tells the user exactly how to grant access manually. A future hardening could marshal the request to the main thread or trigger it during a stream-open attempt.

## Post-ship fix — startup network call on cached model (privacy-constraint violation, 2026-06-13)

Surfaced from the `murmur run` startup log on the target machine: every launch — even with weights fully cached and the verified-marker present — emitted `httpx: HTTP Request: GET https://huggingface.co/api/models/mlx-community/whisper-large-v3-mlx/revision/main`. This violates the spec Constraints privacy rule ("the default configuration must never make an outbound network call during normal operation"; first-run model download is the only sanctioned exception).

Root cause (`src/murmur/transcribe/models.py`): `ModelManager.ensure()` called `self._download(repo)` *unconditionally, before* the marker check. `_download` calls `huggingface_hub.snapshot_download`, which resolves the remote revision over the network even when every file is already cached. The marker only short-circuited the sha256 re-hash — not the network round-trip — so the "fast startup" path still phoned home on every run.

Fix: `ensure()` now checks the marker *first*. When the marker exists it resolves the snapshot via a new `_resolve_offline(repo)` helper that calls `snapshot_download(local_files_only=True)` — guaranteeing huggingface_hub makes no outbound request (no remote revision resolution). On success it returns the cached path with zero network I/O; the marker-skips-rehash behaviour is preserved. The marker is not blindly trusted: if the local snapshot is missing (`LocalEntryNotFoundError`/`FileNotFoundError`), `_resolve_offline` returns `None` and `ensure()` falls back to the full network download → sha256 verify → marker rewrite path. First-run (no marker) behaviour is unchanged.

Tests added/extended (`tests/unit/test_models.py`):
- `test_marker_present_resolves_offline_no_network` — marker present ⇒ `snapshot_download` is invoked exactly once with `local_files_only=True` and the fake `HfApi.model_info` is never called (proves no network-touching collaborator runs).
- `test_marker_present_but_snapshot_missing_falls_back` — marker present but cache gone ⇒ offline resolve raises `LocalEntryNotFoundError`, ensure() re-downloads (no `local_files_only`) and re-verifies.
- `test_marker_skips_rehash` — updated to patch `_resolve_offline` (the collaborator the marker path now uses) instead of `_download`; still asserts verification is skipped.

Docs: README "Model weights" section now states that, once verified, subsequent launches resolve the cached snapshot fully offline (`local_files_only=True`) with no outbound request.

Verification: `uv run pytest` → 237 passed, 1 xfailed, coverage 85.41%; `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` all clean. Committed as `fix(models): resolve cached model snapshot offline after first verification`.
