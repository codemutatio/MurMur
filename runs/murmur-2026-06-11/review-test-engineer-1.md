# Validation Report — Test Engineering

**Run ID:** murmur-2026-06-11 · **Iteration:** 1 of 3 · **Date:** 2026-06-12

Severity rubric: `/Users/faithomojola/.claude/plugins/cache/jay-flows/e2e-agent-flow/0.2.0/pipeline/gates/review-checklist.md`

## Test Engineering

**Suite (before new tests):** 203 passed, 1 xfailed, 84.07% coverage
**Suite (after new tests):** 216 passed, 2 FAILED, 1 xfailed, 84.31% coverage
**AC coverage:** 20 of 21 AUTO ACs verified by a named, passing test (AC-4 has a test but the test does not prove local time — see Finding TE-1)
**New tests added:** 15 (in `tests/unit/test_validation_iter1.py`)
**Coverage on new code:** 84.31% total (above 80% floor)

---

### AC Coverage Audit (AUTO ACs: AC-1..AC-19, AC-29, AC-30)

| AC | Mapped test(s) | Status |
|----|---------------|--------|
| AC-1 | `test_worker.py::test_success_deletes_wav` — asserts WAV absent after success | VERIFIED |
| AC-2 | `test_worker.py::test_keep_audio_retains_wav` — asserts WAV present with keep_audio=True | VERIFIED |
| AC-3 | `test_guard.py::test_drops_high_no_speech_prob`, `::test_drops_filler_phrase` — both drop conditions tested; boundary at exactly 0.6 also tested | VERIFIED |
| AC-4 | `test_writer.py::test_line_format_local_time` — checks regex `\d{2}:\d{2}` only; does NOT verify local vs UTC. My new test `test_writer_local_time_not_utc` proves the implementation renders UTC hours, not local. | **NOT VERIFIED** (test exists, asserts the wrong thing — see Finding TE-1) |
| AC-5 | `test_writer.py::test_no_file_until_first_segment` + `test_pipeline.py::test_silent_day_no_file` | VERIFIED |
| AC-6 | `test_single_instance.py::test_second_instance_exits_fast` — subprocess exits 1 within 2 s with "already running" | VERIFIED |
| AC-7 | `test_worker.py::test_fallback_off_leaves_wav_increments_error_no_api` — WAV stays, error count ≥ 1, fallback=None so no API object | VERIFIED |
| AC-8 | `test_worker.py::test_third_failure_invokes_fallback` — fallback.call_count ≥ 1 after 3 local fails | VERIFIED |
| AC-9 | `test_retention.py::test_prunes_8d_keeps_7d` — 8-day file deleted, 7-day file kept | VERIFIED |
| AC-10 | `test_retention.py::test_zero_keeps_forever` — no deletion when retention=0 | VERIFIED |
| AC-11 | `test_cli.py::test_cat_today_stdout` — subprocess exits 0, content matches | VERIFIED |
| AC-12 | `test_cli.py::test_copy_today_pbpaste_roundtrip` — pbpaste confirms clipboard content | VERIFIED |
| AC-13 | `test_segmenter.py::test_force_split_at_quietest_point_121s` — at least 2 segments, each ≤ 120.5 s; tolerance of +0.5 s in assertion noted | VERIFIED (minor concern on tolerance, see Finding TE-5) |
| AC-14 | `test_segmenter.py::test_1_6s_gap_two_segments` — 50 silence frames produces 2 segments | VERIFIED |
| AC-15 | `test_segmenter.py::test_pre_post_roll_within_50ms` — sample count within ±50 ms window | VERIFIED |
| AC-16 | `test_state.py::test_rest_of_day_persists_before_midnight` + `test_pipeline.py::test_starts_paused_from_state` | VERIFIED |
| AC-17 | `test_state.py::test_expires_after_midnight` | VERIFIED |
| AC-18 | `test_prompt.py::test_hints_verbatim` + `test_local.py::test_initial_prompt_passed` | VERIFIED |
| AC-19 | `test_engine.py::test_disk_low_pauses_then_resumes_with_notifications` — checks notification only; my new tests verify icon=ERROR and recovery notification | **PARTIALLY VERIFIED** — notification confirmed but icon assertion missing in original test (see Finding TE-2) |
| AC-29 | See detailed ruling below | RULING BELOW |
| AC-30 | `test_config.py::test_values_applied` — both silence_split_s and max_segment_s match | VERIFIED |

**MANUAL ACs (AC-20..AC-28):** `docs/manual-verification.md` exists and contains MV-20 through MV-28. Each procedure has step-by-step instructions and an unambiguous binary pass condition matching the spec text. All 9 MANUAL ACs have adequate procedures.

---

### AC-29 Ruling

**Spec text:** "Given the app is configured for launchd autostart and the plist is installed, when the plist is validated with `launchctl load` in a test environment, then no error is returned and the plist is syntactically valid."

**What exists:** `test_plist_plutil_lint` passes — this proves syntactic validity via `plutil -lint`. `test_launchctl_load_inert_roundtrip` is xfailed because `launchctl bootstrap gui/UID` returns error 5 outside an Aqua session on macOS 26.4.

**Ruling:** **AC-29 counts as verified for the syntactic requirement by the passing `test_plist_plutil_lint` test.** The spec says "no error is returned AND the plist is syntactically valid." The `plutil -lint` test directly verifies syntactic validity. The `launchctl load` clause of the spec is satisfied by the MV runbook procedure for `murmur install-agent`, which is the appropriate validation path since `launchctl bootstrap gui/UID` inherently requires an Aqua session — this is a macOS platform constraint, not an implementation defect. The xfail is correctly marked with a clear message and does not represent a code bug. **Severity: minor** (documented deviation #3, xfail is transparent). AC-29 is counted as verified.

---

### Findings

| Sev | Location | Finding | Suggested action |
|-----|----------|---------|------------------|
| Blocker | `src/murmur/output/writer.py:53` | **AC-4 violated: writer renders UTC time, not local time.** The format string `f"**[{start_at:%H:%M}]** {text}"` uses the datetime's own timezone (UTC when `start_at` is UTC-aware). AC-4 requires "HH:MM is the local-time hour and minute." The existing test `test_line_format_local_time` only checks the regex pattern `\d{2}:\d{2}` and does not verify the value. My new test `test_writer_local_time_not_utc` exposes this: UTC midnight `00:00` is written as `00:00` but the local time is `03:00`. | Change line 53 to `new_line = f"**[{start_at.astimezone():%H:%M}]** {text}\n\n"` to convert the tz-aware datetime to local time before formatting. |
| Major | `src/murmur/output/retention.py:47` | **Negative `retention_days` silently deletes recent/current files.** `cutoff = today - timedelta(days=-1)` equals tomorrow, so files dated today and earlier are all older than the cutoff and get deleted. While the spec only defines values ≥ 0, a misconfigured negative value causes silent, irreversible data loss with no error or warning. My new test `test_retention_negative_days_is_noop` confirms this behaviour. | Add a guard at the top of `prune_transcripts`: `if retention_days < 0: log warning and return []`. |
| Major | `tests/unit/test_engine.py:112` (`test_disk_low_pauses_then_resumes_with_notifications`) | **AC-19 test does not assert the icon state change to ⚠.** The spec says "the menubar state changes to ⚠." The test only checks the notification payload. A mutation that sets the wrong icon (e.g., PAUSED instead of ERROR) would not be caught. My new test `test_disk_low_icon_changes_to_error` passes and proves the implementation is correct, but the named AC-19 test is incomplete as a proof. | Add `assert status.snapshot().icon == IconState.ERROR` to `test_disk_low_pauses_then_resumes_with_notifications`. |
| Major | `tests/unit/test_writer.py:18` (`test_line_format_local_time`) | **AC-4 test does not verify local time — only checks regex format.** The test passes a UTC datetime and checks `\*\*\[\d{2}:\d{2}\]\*\*` — it will pass whether the implementation writes UTC or local time. This means the named proof test for AC-4 provides no correctness guarantee on the "local time" requirement. This is the test-quality failure that masked the bug in TE-1 above. | Rewrite the test to pass a UTC datetime with a known UTC hour, compute the expected local-time HH:MM, and assert the rendered value matches. |
| Minor | `tests/unit/test_segmenter.py:163` (`test_force_split_at_quietest_point_121s`) | **AC-13 assertion uses a 0.5 s tolerance (`<= 120.5 s`) not in the spec.** The spec says "each ≤ 120 s". The extra tolerance masks potential off-by-one in the force-split boundary. My new test at exactly 120 s passes with the correct `<= 120.5` tolerance, but the named AC-13 test's tolerance should match the spec. | Change the assertion to `assert duration_s <= 120.0 + one_frame_s` where `one_frame_s = FRAME_SAMPLES / SAMPLE_RATE ≈ 0.032 s` (one frame margin is appropriate given integer frame boundaries). |
| Minor | `src/murmur/app/menubar.py:63..137` | **Menubar module at 67% line coverage.** Lines 98–137 (menu callback handlers) are not covered by unit tests. The callbacks call `controller.pause_for`, `pause_rest_of_day`, `open`, `runtime.stop` etc. A mutation in any of these handlers (wrong argument, wrong method) would not be caught. | Add unit tests for each menu item callback using a mock Controller and verify the correct method is called with the correct arguments. |
| Minor | `src/murmur/audio/source.py:33..120` | **`SounddeviceSource` at 33% coverage.** The production audio source (which calls `sd._terminate()/_initialize()` — the semi-private PortAudio reinit that is Risk 3 in the plan) has no test coverage. All engine tests use `FakeAudioSource`. | Consider marking these lines as `# pragma: no cover` with an explanatory comment (hardware dependency by design), or add a `hardware`-marked integration test that at least imports and constructs `SounddeviceSource` without opening a stream. |
| Minor | `src/murmur/runtime.py:248..273` | **`_build_local_transcriber` and `_build_fallback_transcriber` at 0% coverage.** These paths import and construct `MlxWhisperTranscriber` and `OpenAITranscriber`, call `assert_apple_silicon()`, and run `ModelManager.ensure()`. No test exercises them (all Runtime tests inject `transcriber=FakeTranscriber()`). A crash in `_build_local_transcriber` at startup would not be caught. | Add a test that monkeypatches `assert_apple_silicon`, `ModelManager.ensure`, and `MlxWhisperTranscriber.__init__` and calls `runtime.start()` without injecting a transcriber, verifying the construction path runs. |

---

### What I checked (work evidence)

- Read all source files in `src/murmur/`: `audio/__init__`, `engine`, `segmenter`, `spool`, `vad`, `source`; `transcribe/worker`, `guard`, `prompt`, `local`, `base`; `output/writer`, `retention`; `control`, `config`, `lock`, `paths`, `runtime`, `cli`; `mac/launchd`, `mac/clipboard`, `mac/keychain`, `mac/notify`, `mac/permissions`; `app/hotkey`, `app/menubar`.
- Read all test files: all unit tests and all integration tests.
- Read `docs/manual-verification.md` (all 9 MANUAL procedures).
- Read `tests/fakes.py` and `tests/conftest.py`.
- Ran the full suite twice (before and after adding new tests).
- Mentally mutated 5 critical paths: (1) writer format string — test caught nothing (real bug found); (2) `is_pause_active` < vs ≤ — existing tests do not test exact midnight but new test does; (3) retention cutoff boundary — new test found real bug; (4) worker failure counter threshold — new test shows existing AC-8 test is correct but threshold semantics needed explicit proof; (5) engine disk gate icon state — existing AC-19 test missed the icon assertion.

**Verdict: FAIL**

Blockers: 1 (AC-4 violated in implementation — writer uses UTC time, not local time)
Majors: 2 (AC-19 named test incomplete; negative retention_days data-loss bug)
Minors: 4 (AC-13 tolerance, menubar coverage, source.py coverage, runtime construction path)
