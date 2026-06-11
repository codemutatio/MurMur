# Validation Report

**Run ID:** murmur-2026-06-11 · **Iteration:** 1 of max 3 · **Date:** 2026-06-12

Severity rubric: see `/Users/faithomojola/.claude/plugins/cache/jay-flows/e2e-agent-flow/0.2.0/pipeline/gates/review-checklist.md`. Blockers and majors block shipping; minors are logged.

Full per-reviewer reports: `review-test-engineer-1.md`, `review-code-reviewer-1.md`, `review-security-reviewer-1.md` (same directory). This file is the merged canonical record.

## Test Engineering
**Suite:** before: 203 passed / 1 xfailed (84.07%); after TE additions: 216 passed / 2 FAILED / 1 xfailed (84.31%) — the 2 failures are new tests exposing real bugs (F-1, F-2) · **AC coverage:** 20 of 21 AUTO ACs verified (AC-4 NOT verified — named test asserts only a regex, implementation renders UTC) · **New tests added:** 15 (`tests/unit/test_validation_iter1.py`) · **Coverage on new code:** 84.31%

**AC-29 ruling:** verified via passing `test_plist_plutil_lint`; the `launchctl bootstrap` clause is a macOS platform constraint outside Aqua sessions (xfail transparent, MV runbook covers runtime check). Severity: minor. AC-29 counts as verified.

| Sev | Location | Finding | Suggested action |
|-----|----------|---------|------------------|
| Blocker | `src/murmur/output/writer.py:53` | AC-4 violated: writer renders UTC, not local time (proven by failing `test_writer_local_time_not_utc`) | → fix-list F-1 |
| Major | `src/murmur/output/retention.py:47` | Negative `retention_days` silently deletes today's + older files (proven by failing `test_retention_negative_days_is_noop`) | → fix-list F-2 |
| Major | `tests/unit/test_engine.py:112` | Named AC-19 test never asserts icon → ⚠ | → fix-list F-3 |
| Major | `tests/unit/test_writer.py:18` | Named AC-4 test asserts regex only — masked the F-1 bug | → folded into fix-list F-1 |
| Minor | `tests/unit/test_segmenter.py:163` | AC-13 assertion tolerance +0.5 s vs spec's ≤120 s | logged |
| Minor | `src/murmur/app/menubar.py:63..137` | Menubar 67% coverage; menu callbacks untested | logged |
| Minor | `src/murmur/audio/source.py:33..120` | SounddeviceSource 33% coverage (hardware-bound by design, no pragma) | logged |
| Minor | `src/murmur/runtime.py:248..273` | `_build_local_transcriber`/`_build_fallback_transcriber` 0% coverage | logged |

**Verdict:** FAIL

## Code Review
| Sev | Location | Finding | Suggested action |
|-----|----------|---------|------------------|
| Major | `src/murmur/audio/source.py:75` | PortAudio C-callback writes `_dropped_frames` — ADR-1 ownership violation; counter never surfaced (dead output) | → fix-list F-6 |
| Major | `src/murmur/audio/segmenter.py:127–134,197` | Force-split `split_idx` unbounded below: argmin=0 → 1-frame segment, pathological re-split on uniform RMS | → fix-list F-7 |
| Major | `src/murmur/audio/engine.py:226` | `work_q` unbounded; worker dead/backoff → unbounded growth on a long-running daemon | → fix-list F-8 |
| Major | `src/murmur/transcribe/worker.py:109–156` | Non-`TranscriptionError` exceptions escape all handlers → silent worker-thread death (daemon=True); no sleep between local retries | → fix-list F-4 |
| Major | `src/murmur/audio/engine.py` (`run()`) | No outer exception guard on capture loop → silent capture-thread death while menubar shows ● | → fix-list F-5 |
| Minor | `src/murmur/transcribe/guard.py:64–65` | Redundant `or text in FILLER_PHRASES` branch (dead for non-lowercase input) | logged |
| Minor | `src/murmur/audio/segmenter.py:21–22` | Dead module-level `_PRE_ROLL_FRAMES`/`_POST_ROLL_FRAMES` | logged |
| Minor | `src/murmur/app/menubar.py:95–103` | `_on_tick` builds `lines` (queue depth/errors/notice/download) then discards — R-11 "queue depth in dropdown" not rendered | logged¹ |
| Minor | `src/murmur/transcribe/models.py:158–160` | Marker file placed in HF cache root, not MURMUR_HOME — plan-contract mismatch | logged |
| Minor | `src/murmur/audio/engine.py:51` | `notifier: object` + type-ignores instead of `Notifier` Protocol | logged |
| Minor | `src/murmur/control.py:51` | Same `notifier: object` pattern | logged |
| Minor | `src/murmur/audio/spool.py:64–85` | `parse_segment_start` unguarded ValueError on non-conforming filenames | folded into F-4 |
| Minor | `src/murmur/audio/engine.py:220–221` | Local import + assert-isinstance obscures `Segment` typing | logged |
| Minor | `src/murmur/runtime.py:119` | `date.today()` instead of injected `now()` for retention pruning — testability contract breach, midnight flake risk | logged |

¹ Code-reviewer's summary flagged the unrendered dropdown lines as R-11 spec-conformance (top-3 worthy); table severity recorded as Minor. Orchestrator note: R-11's icon requirement is met; the queue-depth-in-dropdown clause is unmet UI wiring. Kept at Minor per the reviewer's own table; revisit if test-engineer disputes in iter 2.

**Verdict:** FAIL

## Security Review
**Attack surface summary:** New always-on mic daemon: config.toml paths flowing into subprocess/filesystem ops, osascript notification channel, spool WAV parsing, env/Keychain API-key path, HF model download on first run, launchd plist via f-string XML. **Privacy guarantee verified clean:** with default config no outbound network occurs (mlx snapshot gated on local path existing; silero loads from wheel; `OpenAITranscriber` never constructed when fallback off); transcript text never reaches logs/notifications/error strings; API key never in argv/logs/repr.

| Sev | Location | Finding (with attack scenario) | Remediation |
|-----|----------|--------------------------------|-------------|
| Minor | `src/murmur/mac/launchd.py:39,57,59` | Plist XML injection via unescaped interpolated paths (adversarial local env/PATH) | plistlib or html.escape — logged |
| Minor | `src/murmur/transcribe/worker.py:101` | Malformed spool filename → ValueError kills worker thread | folded into F-4 |
| Minor | `src/murmur/transcribe/models.py:65–68` | Marker-file trust: weights never re-verified; same-user process can forge marker | logged |
| Minor | `src/murmur/output/retention.py:50–64` | Pruning follows symlinked transcripts_dir → deletes matching files in shared target | logged |
| Minor | `pyproject.toml:7–18` | Lower-bound-only dep pins; uv.lock is the effective pin | logged (informational) |

**Verdict:** PASS

## Iteration Outcome
**Overall:** FAIL (→ fix-list-1.md → Implement)
**Minors logged (not blocking):** 17 (4 test-engineering, 8 code-review, 5 security; 2 of the 19 raw minors folded into F-4)
