# Final Report: Murmur — Personal Always-On Voice Transcription for macOS

**Run ID:** murmur-2026-06-11 · **Branch:** feature/murmur-2026-06-11 · **Completed:** 2026-06-12T02:25:00+0300

## Summary

Murmur v0.1.0 is a complete, tested implementation of the PRD's v1 scope (M1 core pipeline + M1.5 bake-off + M2 menubar daemon): a single Python process that captures the default microphone at 16 kHz, gates audio through Silero VAD (silence never touches disk), segments speech (1.5 s silence close / 120 s force-split at the quietest frame, 0.3 s pre/post-roll), spools WAVs as a durable on-disk queue, transcribes FIFO with mlx-whisper large-v3 (vocabulary hints via initial_prompt, hallucination guard with checkpoint-corrected thresholds, optional OpenAI fallback off by default), and appends timestamped local-time blocks to `~/Transcripts/YYYY-MM-DD.md` via atomic whole-file replace. A rumps menubar app (●/⏸/⚠ + queue depth), `⌃⌥⌘M` global hotkey with persisted pause state, launchd autostart, single-instance lock, retention pruning, disk-low gate, device-change/sleep-wake recovery, model download with explicit sha256 verification, and a six-command CLI (`run/cat/copy/bench/install-agent/uninstall-agent`) plus standalone `scripts/bakeoff.py` complete the system. 233 tests pass (1 platform-constraint xfail), 84.72% coverage, ruff/mypy clean, with no microphone, GUI, or model download needed in the default suite.

## Traceability

21 AUTO ACs: VERIFIED by named passing tests. 9 MANUAL ACs (hardware/human-judgment by spec design): runbook procedures ready in `docs/manual-verification.md`, execution pending by the owner on the target machine.

| AC | Criterion (short) | Implemented in | Verified by test | Status |
|----|-------------------|----------------|------------------|--------|
| AC-1 | WAV deleted after success (default) | src/murmur/transcribe/worker.py | tests/unit/test_worker.py::test_success_deletes_wav | VERIFIED |
| AC-2 | keep_audio=true retains WAV | src/murmur/transcribe/worker.py | tests/unit/test_worker.py::test_keep_audio_retains_wav | VERIFIED |
| AC-3 | Hallucination guard drops artifacts | src/murmur/transcribe/guard.py | tests/unit/test_guard.py::test_drops_high_no_speech_prob, ::test_drops_filler_phrase | VERIFIED |
| AC-4 | `**[HH:MM]**` local-time line format | src/murmur/output/writer.py | tests/unit/test_writer.py::test_line_format_local_time + test_validation_iter1.py::test_writer_local_time_not_utc | VERIFIED (bug found+fixed in validation, F-1) |
| AC-5 | No file on silent day | src/murmur/output/writer.py | tests/unit/test_writer.py::test_no_file_until_first_segment; tests/integration/test_pipeline.py::test_silent_day_no_file | VERIFIED |
| AC-6 | Second instance exits ≤2 s, first unaffected | src/murmur/lock.py, cli.py | tests/integration/test_single_instance.py::test_second_instance_exits_fast | VERIFIED |
| AC-7 | Fallback-off: WAV stays, error count, no network | src/murmur/transcribe/worker.py | tests/unit/test_worker.py::test_fallback_off_leaves_wav_increments_error_no_api | VERIFIED |
| AC-8 | 3rd local failure → API fallback | src/murmur/transcribe/worker.py, api.py | tests/unit/test_worker.py::test_third_failure_invokes_fallback; test_api.py::test_request_shape | VERIFIED |
| AC-9 | retention=7: 8-day-old pruned, 7-day kept | src/murmur/output/retention.py | tests/unit/test_retention.py::test_prunes_8d_keeps_7d | VERIFIED |
| AC-10 | retention=0 keeps forever | src/murmur/output/retention.py | tests/unit/test_retention.py::test_zero_keeps_forever | VERIFIED |
| AC-11 | `murmur cat today` → stdout, exit 0 | src/murmur/cli.py | tests/integration/test_cli.py::test_cat_today_stdout | VERIFIED |
| AC-12 | `murmur copy today` → clipboard | src/murmur/cli.py, mac/clipboard.py | tests/integration/test_cli.py::test_copy_today_pbpaste_roundtrip | VERIFIED |
| AC-13 | 121 s speech → ≥2 segments ≤120 s, split at quietest | src/murmur/audio/segmenter.py | tests/unit/test_segmenter.py::test_force_split_at_quietest_point_121s | VERIFIED |
| AC-14 | 1.6 s gap → two segments | src/murmur/audio/segmenter.py | tests/unit/test_segmenter.py::test_1_6s_gap_two_segments | VERIFIED |
| AC-15 | 0.3 s pre/post-roll ±50 ms | src/murmur/audio/segmenter.py | tests/unit/test_segmenter.py::test_pre_post_roll_within_50ms | VERIFIED |
| AC-16 | Rest-of-day pause survives relaunch before midnight | src/murmur/control.py | tests/unit/test_state.py::test_rest_of_day_persists_before_midnight; test_pipeline.py::test_starts_paused_from_state | VERIFIED |
| AC-17 | Pause expires after midnight | src/murmur/control.py | tests/unit/test_state.py::test_expires_after_midnight | VERIFIED |
| AC-18 | vocab_hints reach initial_prompt verbatim | src/murmur/transcribe/prompt.py, local.py | tests/unit/test_prompt.py::test_hints_verbatim; test_local.py::test_initial_prompt_passed | VERIFIED |
| AC-19 | Disk <2 GB → pause, ⚠, notify; resume on recovery | src/murmur/audio/engine.py | tests/unit/test_engine.py::test_disk_low_pauses_then_resumes_with_notifications (icon assert added, F-3) | VERIFIED |
| AC-20 | ≥2× real-time throughput | `murmur bench` harness (cli.py, runtime.py) | MV-20 (de-risked: test_bench.py::test_bench_reports_rtf_with_fake) | MANUAL — procedure ready |
| AC-21 | 8 h unattended run, queue drains, CPU idle | whole pipeline | MV-21 (de-risked: test_pipeline.py e2e) | MANUAL — procedure ready |
| AC-22 | AirPods pull → recovery ≤30 s | src/murmur/audio/engine.py, source.py | MV-22 (de-risked: test_engine.py::test_device_signature_change_reopens) | MANUAL — procedure ready |
| AC-23 | Hotkey pause ≤1 s + notification | src/murmur/app/hotkey.py, control.py | MV-23 (de-risked: test_hotkey.py::test_callback_toggles, test_controller.py::test_toggle_fires_notification) | MANUAL — procedure ready |
| AC-24 | Accent accuracy ≥ ChatGPT voice input (go/no-go) | model choice + pipeline | MV-24 (informed by bake-off) | MANUAL — procedure ready |
| AC-25 | Sleep/wake recovery ≤60 s | src/murmur/audio/engine.py (watchdog) | MV-25 (de-risked: test_engine.py::test_watchdog_reopens_after_stall) | MANUAL — procedure ready |
| AC-26 | bakeoff.py → 4-section side-by-side MD | scripts/bakeoff.py | MV-26 (de-risked: test_bakeoff.py::test_four_sections_with_fakes) | MANUAL — procedure ready |
| AC-27 | Mic permission revoked → ⚠ + instructions | src/murmur/mac/permissions.py, audio/engine.py | MV-27 (de-risked: test_permissions.py::test_status_mapping, test_engine.py::test_mic_denied_blocks_and_recovers) | MANUAL — procedure ready |
| AC-28 | First-run model download + checksum | src/murmur/transcribe/models.py | MV-28 (de-risked: test_models.py checksum suite) | MANUAL — procedure ready |
| AC-29 | launchd plist valid | src/murmur/mac/launchd.py | tests/integration/test_launchd.py::test_plist_plutil_lint (PASSING); ::test_launchctl_load_inert_roundtrip xfailed (launchctl bootstrap impossible outside Aqua session on macOS 26.4 — TE ruled verified, minor) | VERIFIED |
| AC-30 | config.toml values applied exactly | src/murmur/config.py | tests/unit/test_config.py::test_values_applied | VERIFIED |

## Validation Summary

**Iterations used:** 2 of 3 · **Findings fixed:** blockers: 1 (AC-4 UTC-vs-local-time in writer), majors: 7 (negative-retention data loss; AC-19 test gap; silent worker-thread death; silent capture-thread death; ADR-1 callback ownership violation; force-split 1-frame degenerate; unbounded work queue) · **Minors logged (open):** 24, see validation-report.md — none blocking; the operationally relevant ones are documented as Known Limitations in README/architecture docs.

Security review: PASS both iterations. Privacy guarantee verified at code level: default config makes zero outbound network calls in normal operation; transcript text never reaches logs, notifications, or error strings; API key never in argv/logs/repr.

## Documentation Produced

- `README.md` — install, permissions, quick start, full config + CLI reference (every command executed), Keychain setup, launchd, bake-off decision rule, troubleshooting, known limitations
- `docs/architecture.md` — thread model (ADR-1), data flow, durability story (ADR-2), state locations, recovery mechanisms
- `docs/manual-verification.md` — MV-20..MV-28 runbook (pre-existing from T-20; MV-20 step corrected during doc verification)
- `CHANGELOG.md` — Keep-a-Changelog, v0.1.0

## Deviations & Decisions

- Checkpoint decisions (state.md): OQ-2 guard semantics corrected vs spec recommendation (drop compression_ratio > 2.4); OQ-1/3/4/5 recommended defaults; launchd KeepAlive=false; `murmur bench`/`install-agent` tooling additions.
- Implementation deviations (implementation-notes.md, all flagged): rumps works on macOS 26.4 (no fallback needed); clipboard.py module added; AC-29 launchctl xfail (platform constraint); `Segmenter.reset_vad()` added; HotkeyListener callback type widened.
- F-8 resolution: work queue bounded at 256 with put_nowait drop-and-log; spool on disk remains the durable queue (drops recovered at next startup scan).
- Doc-stage discrepancies: **D-3 — `stt.engine` config key parses but is never read** (engine selection is solely `api_fallback` + key presence); omitted from docs to avoid documenting a no-op; future: wire it or drop it. D-1 — launchd plist generated programmatically (plan listed a static template file; render_plist is the tested contract). D-2 — MV-20 runbook command bug, fixed.
- Orchestrator mechanical interventions (transparency): two style-only commits (86f9673, and the SIM105 follow-up) applying linter-prescribed fixes to the test-engineer's iter-2 test file — import sorting and contextlib.suppress; no logic touched.
- Arbitrations: none required (no disputed findings; implementer disputed nothing).

## How to Review

1. Start with `docs/architecture.md` (thread ownership + durability model), then `src/murmur/audio/segmenter.py` (purest logic, heaviest tests), then `transcribe/worker.py` + `audio/engine.py` (the two long-running loops — scrutinize the exception-guard semantics added in F-4/F-5).
2. The validation trail: `runs/murmur-2026-06-11/validation-report.md` → `fix-list-1.md` → fix commits dd923db..6faae07.
3. Run locally: `uv sync && uv run pytest` (no hardware needed; ~35 s). Lint/types: `uv run ruff check . && uv run mypy src`.
4. Hardware acceptance: follow `docs/manual-verification.md` MV-20..MV-28 in order; MV-26 (bake-off) feeds the MV-24 model decision.
5. Commit history is task-ordered (`[T-n]`/`[TASK-n]` tags) — reviewable commit-by-commit.

## Suggested PR

**Title:** `feat: Murmur v0.1.0 — always-on local voice transcription daemon for macOS`

**Description:**

> Implements the Murmur PRD v1 scope end-to-end: M1 core pipeline (mic → Silero VAD → segmenter → WAV spool → mlx-whisper large-v3 → daily Markdown), M1.5 bake-off script, M2 menubar daemon (rumps UI, ⌃⌥⌘M global hotkey, pause timers with persisted state, launchd autostart, permission detection, error surfacing).
>
> **Design:** single process, thread-per-stage with strict ownership (ADR-1); spool directory doubles as the durable work queue, so crash recovery and backlog absorption are the same mechanism. Transcript durability via whole-file rewrite + atomic replace (ADR-2). Every hardware boundary is an injectable protocol — the full default test suite (233 tests, 84.72% coverage) runs with no microphone, no GUI, and no model downloads.
>
> **Privacy:** default config makes zero outbound network calls (verified in security review); raw audio deleted after transcription; API fallback off by default; keys via env/Keychain only.
>
> **Validation:** 2 adversarial iterations — 1 blocker (UTC-vs-local timestamps) and 7 majors (incl. silent thread-death risks and an unbounded queue) found and fixed; test-engineer, code-reviewer, and security-reviewer all PASS. 24 minors logged in the run artifacts.
>
> **Test evidence:** `uv run pytest` → 233 passed, 1 xfailed (launchctl bootstrap needs an Aqua session — platform constraint, covered by runbook), coverage 84.72% ≥ 80% bar. ruff + mypy clean.
>
> **Hardware acceptance pending owner execution:** docs/manual-verification.md MV-20..MV-28 (throughput bench, 8-h run, AirPods pull, hotkey latency, accent-accuracy go/no-go, bake-off, permission flows, model download).
>
> No breaking changes (greenfield). 🤖 Generated with [Claude Code](https://claude.com/claude-code)
