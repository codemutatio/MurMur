# Test Engineering Review — Iteration 1

**Run ID:** murmur-mobile-2026-06-14  
**Reviewer role:** test-engineer  
**Date:** 2026-06-14  
**Repos reviewed:**  
- Component A: `/Users/faithomojola/Documents/MurMur` (branch `feature/murmur-mobile-2026-06-14`)  
- Component B: `/Users/faithomojola/Documents/MurmurMobile`

---

## Suite Results

### Component A — Murmur Server (pytest)

| Metric | Value |
|--------|-------|
| Tests run | 350 passed (after TE additions) / 340 before |
| Deselected | 1 (`hardware` marker, no ffmpeg/tailscale) |
| Expected failures | 1 (`xfail` — launchctl GUI test, non-headless) |
| Failures | 0 |
| Coverage (total repo) | 85.62% (≥ 80% required — PASS) |
| New-code coverage (server sub-package only) | `app.py` 96%, `audio.py` 100%, `config.py` 89%, `launchd_server.py` 100%, `token.py` 100% |

**Suite status: GREEN**

### Component B — Murmur Mobile (Gradle + Robolectric)

| Metric | Value |
|--------|-------|
| Tests run | 153 passed (after TE additions) / 127 before |
| Failures | 0 |
| Errors | 0 |
| JaCoCo measured coverage | 90.7% on measurable classes (243/268 lines) |
| Unmeasured classes | `TranscribeWorker`, `RecordingService`, `NotificationHelper`, `SettingsRepository`, Room DAO, Compose screens — all Robolectric-tested, excluded by JaCoCo/Robolectric classloader incompatibility (deviation B-2) |

**Suite status: GREEN**

---

## AC Coverage Audit — 61 of 61

### Component A ACs (AC-1 through AC-20)

| AC | Test(s) that prove it | Verdict |
|----|----------------------|---------|
| AC-1 | `test_server_health::test_health_ok_shape` — asserts 200, all required JSON keys, model=large-v3, version non-empty | PASS |
| AC-2 | `test_health_401_missing_token`, `test_health_401_wrong_token` — asserts 401 + no internals leaked | PASS |
| AC-3 | `test_health_model_loaded_false_when_initializing` — flag unset → model_loaded:false | PASS |
| AC-4 | `test_transcribe_200_shape` — asserts text/segments/language/duration_sec>0/model/engine in body | PASS |
| AC-5 | `test_decode_returns_float32_16k` + `test_passes_float32_16k_to_transcriber` + `test_duration_sec_from_pcm_length` | PASS |
| AC-6 | `test_initial_prompt_from_vocab_hints`, `test_default_vocab_when_absent`, `test_language_defaults_en` | PASS |
| AC-7 | `test_no_speech_returns_422` — clean_result→None → 422 no_speech_detected | PASS |
| AC-8 | `test_413_at_25mb_plus_one`, `test_25mb_exactly_accepted` | PASS |
| AC-9 | `test_401_before_ffmpeg` — auth checked before ffmpeg/transcriber | PASS |
| AC-10 | `test_415_unsupported_type`, `test_octet_stream_m4a_accepted` | PASS |
| AC-11 | `test_ffmpeg_nonzero_raises`, `test_decode_failed_422` | PASS |
| AC-12 | `test_transcription_failed_422` | PASS |
| AC-13 | `test_health_fast_while_lock_held` + async `test_health_not_blocked_by_lock` — both assert <500ms while lock/slow-decode/slow-transcriber holds (see §III for genuineness analysis) | PASS |
| AC-14 | `test_concurrent_transcribe_503_model_busy` — second concurrent POST → 503 model_busy immediately | PASS |
| AC-15 | `test_compare_uses_compare_digest`, `test_compare_digest_matches_hmac`, `test_verify_token_impl_uses_compare_digest` [TE] — inspects source to confirm `compare_digest` is present, not `==` | PASS |
| AC-16 | `test_missing_file_defaults`, `test_malformed_defaults` | PASS |
| AC-17 | `test_token_never_logged` — captures root logger, asserts token absent | PASS |
| AC-18 | `test_temp_deleted_on_success`, `test_temp_deleted_on_error` — both paths | PASS |
| AC-19 | `test_plist_lints`, `test_keepalive_true`, `test_caffeinate_present`, `test_program_args_point_to_entrypoint` | PASS |
| AC-20 | MV-20 — manual runbook step present with unambiguous pass condition | PASS (human-on-device) |

### Component B ACs (AC-21 through AC-53)

| AC | Test(s) that prove it | Verdict |
|----|----------------------|---------|
| AC-21 | `SettingsRepositoryTest#persists_and_restores_after_process_death` — creates new repo over same prefs, asserts all fields restored. **Deviation B-1**: uses plain SharedPreferences; on-device encryption path is MV-54. | PASS (with deviation) |
| AC-22 | `SecretsLoggingTest#key_never_in_logcat` — asserts ShadowLog.getLogs() contains no key string | PASS |
| AC-23 | `MacEngineTest#test_connection_maps_health_states__AC23` — asserts all 4 UI strings for all 4 HealthState values | PASS |
| AC-24 | `RecordingServiceTest#starts_fgs_microphone_with_actions` — Robolectric, asserts notification channel created | PASS (partial; see finding M-1) |
| AC-25 | `RecordingServiceTest#continues_when_backgrounded` — asserts service still non-null after lifecycle events | PASS (shallow — see finding Mi-1) |
| AC-26 | `RecordingServiceTest#m4a_16k_mono` + MV-26-B — headless test checks API constant values (OutputFormat.MPEG_4==2, AudioEncoder.AAC==3); real file verified on-device | PASS (hybrid — deviation B-3) |
| AC-27 | `RecordingPolicyTest#audio_focus_loss_finalizes_pending` — **TAUTOLOGICAL**: only asserts `AudioManager.AUDIOFOCUS_LOSS == -1`, which is a platform constant. Does NOT test the service finalizes to pending. See finding M-2. | **WEAK** (see M-2) |
| AC-28 | `PermissionTest#denied_shows_rationale_no_crash` | PASS |
| AC-29 | `PermissionTest#post_notifications_denied_warns_still_records` | PASS |
| AC-30 | `RecordingServiceTest#m4a_16k_mono` for file+row atomicity; `RecordingPolicyTest#disk_full_rolls_back` for rollback — **disk_full test is tautological** (asserts `0L > 0L == false`, not that the service rolls back). See finding M-2. | **WEAK** (see M-2) |
| AC-31 | `OutboxStateMachineTest#valid_transitions_only` + invalid_transition tests | PASS |
| AC-32 | `OutboxStateMachineTest#done_requires_rawtext_engine`, `#done_requires_engine_not_null`, `#done_requires_null_error_message`, `#failed_requires_error`, `#failed_requires_null_rawtext` | PASS |
| AC-33 | `NoteDaoTest#reset_stale_transcribing_to_pending` + `WorkerTest#startup_resets_stale_transcribing__AC33_E34` | PASS |
| AC-34 | `DispatcherTest#mac_loaded_uses_mac__AC34` + `MacEngineTest#health_200_model_loaded_true_returns_LOADED__AC34` | PASS |
| AC-35 | `DispatcherTest#mac_model_not_loaded_falls_through__AC35` + `MacEngineTest#health_200_model_loaded_false_returns_NOT_LOADED__AC35` | PASS |
| AC-36 | `DispatcherTest#mac_unreachable_uses_openai__AC36` + `MacEngineTest#health_timeout_2500ms_unreachable__AC36` | PASS |
| AC-37 | `DispatcherTest#empty_mac_url_skips_health__AC37`, `#empty_mac_url_fallback_disabled_stays_pending__AC37_AC38` | PASS |
| AC-38 | `DispatcherTest#fallback_disabled_stays_pending__AC38` | PASS |
| AC-39 | `DispatcherTest#mac_401_failed_no_fallback__AC39` + `MacEngineTest#mac_401_permanent_no_fallback__AC39` | PASS |
| AC-40 | `DispatcherTest#openai_401_failed__AC40` + `OpenAiEngineTest#openai_401_permanent__AC40_E25` | PASS |
| AC-41 | `MacEngineTest#sends_vocab_hints_field__AC41`, `OpenAiEngineTest#sends_prompt__AC41`, `DispatcherTest#vocab_hints_passed_to_mac_engine__AC41`, `#vocab_hints_passed_to_openai_engine__AC41` | PASS |
| AC-42 | `EngineInterfaceTest#both_engines_substitutable` | PASS |
| AC-43 | `WorkerTest#no_dispatch_without_network__AC43` — asserts CONNECTED constraint on request spec | PASS |
| AC-44 | `WorkerTest#backoff_params_30s_5m_10__AC44` + `#max_retries_marks_failed__AC44` + `WorkerLogicTest#preflight_returns_MaxRetriesExceeded_when_attemptCount_equals_max` | PASS |
| AC-45 | `WorkerTest#unique_work_per_note__AC45` — enqueues twice, asserts only 1 work info with KEEP | PASS |
| AC-46 | `RecordingPolicyTest#caps_at_25_min` + `#cap_warning_contains_required_text` | PASS |
| AC-47 | `NoteListTest#orders_desc_with_badges_and_preview` + `#preview_done_truncates_to_80_chars` + status placeholders | PASS |
| AC-48 | `NoteDetailTest#detail_shows_rawtext_status_engine` | PASS |
| AC-49 | `NoteDetailTest#copy_sets_clipboard` | PASS |
| AC-50 | `NoteDetailTest#share_emits_action_send` | PASS |
| AC-51 | `DeleteTest#removes_row_and_audio` + `#missing_audio_warns_not_crash` — **finding**: DAO delete test asserts file STILL EXISTS (ViewModel layer not exercised; see Mi-2) | PASS (DAO layer; ViewModel file deletion not tested) |
| AC-52 | `RetentionTest#deletes_audio_on_done_when_keep_false__AC52` | PASS |
| AC-53 | `RetentionTest#retains_audio_when_keep_true__AC53` | PASS |

### Device-only ACs (AC-54 through AC-61) + AC-20

| AC | Runbook step | Verdict |
|----|-------------|---------|
| AC-20 | MV-20 — unambiguous pass condition, references §A contract | PASS |
| AC-54 | MV-54 — 2-min note, Mac awake, <30s done, engine=mac, accent | PASS |
| AC-55 | MV-55 — Mac asleep, OpenAI fallback, engine=openai | PASS |
| AC-56 | MV-56 — airplane mode → pending; reconnect → done auto | PASS |
| AC-57 | MV-57 — lock screen 60s, recording still active, also covers AC-26 format (MV-26-B) | PASS |
| AC-58 | MV-58 — background 60s, complete note | PASS |
| AC-59 | MV-59 — phone call interruption, pending note with non-zero duration | PASS |
| AC-60 | MV-60 — Share → Claude app, full raw_text in compose field | PASS |
| AC-61 | MV-61 — non-tailnet device blocked, port 8080 on 127.0.0.1 only | PASS |

**AC Coverage: 61 of 61 ACs have a proving test or manual runbook step.**  
**2 ACs (AC-27, AC-30) have weak/tautological tests — see findings M-2.**

---

## Coverage Gate Verdict (R-13, B-2/M2-2)

**Verdict: CONDITIONALLY PASS with documented measurement gap.**

**Reasoning:**

The JaCoCo measurement gap (deviation B-2/M2-2) is real but bounded:

1. **Measurable classes (pure JVM):** 90.7% overall (engine 86.5%, dispatch 94.4%, worker logic 100%, retention 89.5%, db 97.9%, UrlValidator 100%, NotePreview 100%). All exceed the 80% gate.

2. **Unmeasurable classes (Robolectric-tested):** `RecordingService`, `TranscribeWorker` (Android framework), `SettingsRepository` (Keystore), Room DAO, Compose UI. These ARE exercised by passing Robolectric tests — the gap is measurement, not execution.

3. **Real untested branches in unmeasured code:** The `RecordingService.saveNoteAtomically` compensating action (E-32/E-33) — the Room-insert-failure→file-rollback branch — is not reachable from Robolectric because Robolectric's Room does not inject failure. This is a **genuine untested path** (see finding M-2).

4. **Where measurable coverage was raised:** TE added tests covering previously-uncovered branches in `MacTranscriptionEngine` (blank macUrl, empty body, malformed 200, IO error, unexpected HTTP), `OpenAITranscriptionEngine` (empty audio, malformed 200, 5xx, unexpected), `RetryEligibility` (non-failed status, blank path), `AudioRetentionManager` (file-already-gone branch), and `TranscriptionDispatcher` (5xx from transcribe itself, language passthrough).

**Overall:** R-13 (≥80%) is met for measurable code. The measurement gap is architecturally inherent (Robolectric + JaCoCo offline instrumentation incompatibility, documented in B-2). The main risk is the untested compensating-action failure branch in `RecordingService` (E-33), which is flagged as a major.

---

## Test Quality — Critical Path Analysis

### test_health_not_blocked_by_lock (AC-13, ADR-2)

**Genuinely proves the claim.** The test uses `httpx.AsyncClient` with `asyncio.create_task` to start a real 1-second decode, waits 50ms for lock acquisition, then measures the health endpoint response time. The assertion `elapsed < 0.5s` would catch a regression where `/health` acquires the lock (would block for ~1s). The slow-transcriber variant added by TE (`test_health_fast_while_transcriber_holds_lock`) adds further coverage: slow transcriber (not just slow decode) is tested. **Verdict: Genuine.**

### test_concurrent_transcribe_503_model_busy (AC-14)

**Genuine.** Uses `asyncio.create_task` for parallel requests and asserts `resp2.status_code == 503` AND `error_code == "model_busy"`. Would catch if lock.locked() check is removed. **Verdict: Genuine.**

### disk_full_rolls_back (E-32/E-33)

**Tautological — does NOT prove the claim.** Asserts `0L > 0L == false`. This is a math identity, not a test of production code. The real compensating action (`saveNoteAtomically` rolling back on Room failure) is tested by zero lines of test code. See finding M-2.

### audio_focus_loss_finalizes_pending (AC-27)

**Tautological — does NOT prove the claim.** Asserts `AudioManager.AUDIOFOCUS_LOSS == -1`. This is a platform constant, always true. The service's `audioFocusListener` response (finalizing to pending) is not exercised. See finding M-2.

### Dispatcher decision table — all §I rows

All 11 rows of the §I table have corresponding test assertions in `DispatcherTest` + the new `DispatcherEdgeCaseTest`. The test for "Mac loaded but transcribe returns 5xx" was missing and added by TE. **Verdict: Now complete.**

---

## Findings

### Severity Table

| Sev | ID | File:line | Finding | Suggested Action |
|-----|-----|-----------|---------|-----------------|
| Major | TE-M-1 | `app/src/test/.../RecordingPolicyTest.kt:44` | `disk_full_rolls_back` is a tautological no-op assertion (`0L > 0L == false`). AC-30's atomicity guarantee (E-32/E-33) — specifically the compensating action that rolls back the Room row when audio write fails, and deletes the audio file when Room insert fails — has **zero test coverage**. The production code at `RecordingService.kt:256-262` (`saveNoteAtomically`) has a Room-failure rollback path that is never exercised by any test. | Replace the tautological assertion with a Robolectric test that: (a) configures the DAO to throw on insert and asserts the audio file is deleted (E-33), OR (b) simulates a zero-byte file after stop and asserts no Room row is inserted (E-32). Both require Robolectric since RecordingService is Android-framework. |
| Major | TE-M-2 | `app/src/test/.../RecordingPolicyTest.kt:30` | `audio_focus_loss_finalizes_pending` asserts `AudioManager.AUDIOFOCUS_LOSS == -1` — a platform constant, always true regardless of production code behavior. AC-27 (on audio-focus loss, service finalizes and saves the recording as `pending`) has **no behavioral test**. The `audioFocusListener` in `RecordingService.kt` is not exercised by any test. | Add a Robolectric test that sends `AUDIOFOCUS_LOSS` to `RecordingService`'s focus listener and asserts: (a) the Room DAO receives an insert with `status=PENDING`, (b) `service.isRecording` becomes false, (c) no audio data is discarded (note has positive `durationSec`). |
| Minor | TE-Mi-1 | `app/src/test/.../RecordingServiceTest.kt:64` | `continues_when_backgrounded` asserts `controller.get() != null` (service object not null) after a no-op lifecycle simulation. This does not verify the foreground service remains actively running — it only checks the object reference is non-null. AC-25 requires audio capture to continue when the app backgrounds. | Strengthen the test: after simulating app-background lifecycle, assert the service is still in the foreground state (via `ShadowApplication.getForegroundServices()`) or assert `isRecording == true`. |
| Minor | TE-Mi-2 | `app/src/test/.../ui/DeleteTest.kt:76` | `removes_row_and_audio` asserts `audioFile.exists() == true` after calling `dao.delete(n.id)` and then notes "ViewModel would delete it; DAO doesn't." This is correct for the DAO test but means AC-51's file-deletion guarantee is only tested at the DAO level. `NoteViewModel.deleteNote` — which actually calls `File(path).delete()` — has no test coverage. | Add a test for `NoteViewModel.deleteNote` (either a plain JVM test with a mock DAO, or a Robolectric test with an AndroidViewModel) that asserts the audio file is deleted from the filesystem when `deleteNote` is called. |
| Minor | TE-Mi-3 | `src/murmur/server/__main__.py:0-174` | `__main__.py` has 0% line coverage (71 uncovered statements) in the full pytest suite. While the test-engineer added `test_startup_token_absent_exits_with_exact_message` covering the E-18 exit path, the single-instance lock path (lines 71-78), model-load thread (lines 83-97), and platform-fallback stub transcriber path (lines 107-127) remain uncovered. These paths include error-handling logic (model load failure, AlreadyRunningError). | Add unit tests for: (a) `AlreadyRunningError` path → `sys.exit(1)` with correct message; (b) `_load_model` exception path → flag stays unset (model_loaded=false); (c) `_StubTranscriber` is created on non-Apple-Silicon. These can all be tested by mocking `ModelManager.ensure` and `MlxWhisperTranscriber`. |
| Minor | TE-Mi-4 | `app/src/main/java/.../engine/MacTranscriptionEngine.kt:177-186` | `no_speech_detected` (HTTP 422) from the Mac server is mapped to `RetryableFailure`. This means a note with genuinely empty/silent audio will be retried 10 times, always failing, before being marked `failed "Max retries exceeded"`. The spec does not explicitly categorize this as retryable vs permanent; the behavior is defensible (the silence could be transient) but wastes 10 retry cycles and gives the user a misleading error message. | Consider mapping `no_speech_detected` to `PermanentFailure("No speech detected in recording")` to give immediate user feedback. If retryable is intentional, document the decision and add a test that asserts `no_speech_detected` → `RetryableFailure` (the existing test covers this correctly). |
| Minor | TE-Mi-5 | `src/murmur/server/config.py:103-104,116-117,134-135,140,146-147,167` | 11 lines in `config.py` are uncovered: the `[server] not-a-table` branch (line 116), `[stt] not-a-table` branch (line 134), `stt.vocab_hints` wrong type branch (lines 146-147), and the int-to-float coercion path (line 167). | Add tests for: (a) `[server]` section is a string not a dict; (b) `[stt]` section is a string; (c) `vocab_hints` value is an int instead of list; (d) a float config key receiving an int (tests the `int→float` coercion). |

---

## New Tests Added by Test Engineer

### Component A (server) — `tests/unit/test_server_edge_cases_te.py`

10 new tests:
1. `test_startup_token_absent_exits_with_exact_message` [E-18] — proves __main__.main() calls sys.exit(1) with the E-18 message
2. `test_get_server_token_missing_returns_none` [E-18] — isolated token lookup
3. `test_health_401_bearer_with_spaces_only` [E-14] — whitespace-only token after "Bearer "
4. `test_malformed_toml_clean_section_ignored` [E-16] — malformed [clean] section ignored
5. `test_unknown_stt_subkey_warns_and_defaults_applied` [E-17] — unknown key in [stt]
6. `test_vocab_hints_mixed_empty_and_nonempty` [E-7] — mixed empty/non-empty hints in comma list
7. `test_25mb_boundary_is_binary_mb_not_decimal` [AC-8, E-1, E-2] — confirms binary MB semantics for 25 MB limit
8. `test_verify_token_impl_uses_compare_digest` [AC-15] — source inspection confirms compare_digest not ==
9. `test_health_fast_while_transcriber_holds_lock` [AC-13] — slow transcriber (not just decode) variant
10. `test_transcribe_non_multipart_body_returns_error` — non-multipart body → 400/415/422

### Component B (Android) — 3 new test files

**`engine/MacEngineEdgeCaseTest.kt`** — 10 new tests covering uncovered engine branches:
- blank macUrl → isAvailable=false, transcribe=NotApplicable, checkHealth=UNREACHABLE
- 200 empty body → NOT_LOADED
- malformed 200 (no "text" field) → RetryableFailure
- 302 unexpected status → RetryableFailure
- zero-byte audio → PermanentFailure before server contact
- `extractBooleanField` regex parser coverage

**`engine/OpenAiEdgeCaseTest.kt`** — 5 new tests:
- empty audio → PermanentFailure
- 200 malformed no text → RetryableFailure
- 503 → RetryableFailure
- 302 → RetryableFailure
- fallback disabled → NotApplicable before HTTP

**`dispatch/DispatcherEdgeCaseTest.kt`** — 6 new tests:
- Mac healthy, transcribe returns 5xx → RetryableFailure (no OpenAI fallback)
- Mac healthy, transcribe returns PermanentFailure → PermanentFailure (no OpenAI fallback)
- Mac NOT_LOADED + fallback disabled → NotApplicable (§I row 3)
- Language "fr" passed to Mac engine
- Language "de" passed to OpenAI engine
- null mac + unavailable OpenAI → NotApplicable

**`retention/RetentionEdgeCaseTest.kt`** — 3 new tests:
- RetryEligibility: pending/done/transcribing status → Ineligible "not in failed state"
- RetryEligibility: blank audioPath → Ineligible
- AudioRetentionManager: file-already-deleted on done → returns null without crash

**Total new tests added: 34** (10 server + 10 Mac engine + 5 OpenAI engine + 6 dispatcher + 3 retention)

---

## Coverage Gate — New Code

| Component | Metric | Result |
|-----------|--------|--------|
| Component A — new server module (measured by pytest-cov) | app.py: 96%, audio.py: 100%, config.py: 89%, launchd_server.py: 100%, token.py: 100%, __main__.py: partial (E-18 path now tested) | PASS (≥80%) |
| Component B — measurable classes (JaCoCo) | 90.7% overall; all packages ≥80% after TE additions | PASS (≥80%) |
| Component B — unmeasured (Robolectric) | Robolectric tests pass; measurement gap documented and adjudicated | Adjudicated CONDITIONAL PASS (see §Coverage Gate Verdict) |

---

## Verdict

**FAIL**

**Reason:** 2 major findings (TE-M-1, TE-M-2) block shipping per the rubric ("Major — must fix, blocks shipping"):

- **TE-M-1:** AC-30 and E-32/E-33 compensating-action atomicity tests are tautological no-ops. The disk-full rollback and Room-failure rollback paths in `RecordingService.saveNoteAtomically` have zero test coverage.

- **TE-M-2:** AC-27 test is tautological (platform constant assertion). The audio-focus-loss service finalization to `pending` has zero behavioral test coverage.

Both findings are in the Android Robolectric layer where the implementer may argue Robolectric makes real testing difficult. However, Robolectric is capable of injecting Room failures and sending audio focus events — the issue is that the tests were not written to exercise the behavioral paths, only the constant values.

Both suites are green. 61/61 ACs have a proving test or runbook step. 34 new tests added. No blockers. 2 majors. 5 minors. **FAIL on majors.**
