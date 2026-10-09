# Security Review — murmur-mobile-2026-06-14 (Iteration 3 / Final Confirmation Pass)

**Reviewer:** security-reviewer
**Date:** 2026-06-14
**Iteration:** 3 — confirming G-1 (server request-path reorder) and G-5 (two proof tests) from fix-list-2 did not introduce or re-open any security issue.
**Components audited (scope: iteration-3 changes only):**
- `src/murmur/server/app.py` — POST /transcribe handler, §A step ordering
- `tests/unit/test_validation_fixes.py` — G-5 proof tests

---

## Attack-Surface Delta (Iteration 3 vs Iteration 2)

No new endpoints, network surfaces, dependencies, or Android components were touched by the G-1 and G-5 fixes. The only changes are:
1. **G-1 (server):** The synchronous busy-check (`lock.locked()`) was reordered to precede the `model_loaded_flag` check inside `POST /transcribe`, restoring §A's mandated precedence (busy=step 2, model-not-loaded=step 3). No new auth bypass or injection surface.
2. **G-5 (tests):** Two tests were added to `test_validation_fixes.py` — one asserting python-multipart ≥ 0.0.18, one asserting a `/`-containing filename yields a sanitized suffix and no 500. Tests do not introduce production attack surface.

---

## Security Review

### Verification 1 — G-1: Auth ordering in POST /transcribe is intact

**Claim to verify:** The bearer-token auth check is still the first thing in `POST /transcribe`, before BOTH the busy-check and the model-loaded check and before any file read or real work.

**Evidence (`src/murmur/server/app.py`):**

- **Line 188–191:** `# Step 1: Auth (AC-9, E-12..E-14)` / `auth = request.headers.get(…)` / `if not _check_auth(auth, app.state.token): return _error(401, …)` — auth is checked first, unconditionally, before anything else executes in the handler body.
- **Lines 201–202:** `# Step 2: Single-flight busy-check` / `if lock.locked(): return _error(503, _E_MODEL_BUSY, …)` — this is the SECOND check, after auth, no intervening `await`.
- **Lines 205–206:** `# Step 3: Model not yet loaded` / `if not app.state.model_loaded_flag.is_set(): return _error(503, _E_MODEL_NOT_LOADED, …)` — this is the THIRD check, after the busy-check, still no intervening `await`.
- **Lines 213–232:** `first_chunk = await file.read(1)` and the size-check loop — file buffering (the only `await` before lock acquisition) does not begin until AFTER auth (step 1), busy-check (step 2), model-loaded check (step 3), and file-presence check (step 4, line 209) all complete.
- **Lines 265–272:** `async with lock: return await _run_transcription(…)` — ffmpeg decode, transcription, and temp-file-write are inside `_run_transcription`, only reached after the lock is acquired, which is only reached after all of the above steps pass.

**G-1 ordering analysis:** The reorder placed busy (step 2) before model-not-loaded (step 3) as required by §A. No auth weakening occurred: auth is still unconditionally first at line 190. The comment block at lines 195–262 confirms the TOCTOU-free intent: the busy-check (line 201) and the `async with lock:` acquisition (line 265) have no intervening `await` that could yield the event loop between them — the file read `await` at step 5 (line 213) happens after the busy-check but before the acquire, and because asyncio is single-threaded, no other coroutine can insert itself between two synchronous statements.

**Result: Auth ordering INTACT. G-1 did not weaken, remove, or reorder auth. Real work (ffmpeg/transcribe/temp-write) remains post-auth and post-lock. PASS.**

---

### Verification 2 — G-5: SR-6 proof tests now exist and assert correctly

**Claim to verify:** `test_python_multipart_version__CVE_2024_53981` and `test_sanitized_tempfile_suffix__SR3` now actually exist in `tests/unit/test_validation_fixes.py` and assert the right thing.

**Test 1 — `test_python_multipart_version__CVE_2024_53981` (lines 169–183):**
- Exists: confirmed at `tests/unit/test_validation_fixes.py:169`.
- Imports `importlib.metadata.version` and `packaging.version.Version`.
- Calls `pkg_version("python-multipart")` to obtain the installed version.
- Asserts `Version(installed) >= Version("0.0.18")` with an error message referencing CVE-2024-53981.
- This is the correct assertion: it checks the resolved/installed package version against the minimum safe version, matching fix-list-2 G-5's requirement. Uses `packaging.version.Version` for proper PEP 440 comparison (not string comparison). Correct.

**Test 2 — `test_sanitized_tempfile_suffix__SR3` (lines 191–236):**
- Exists: confirmed at `tests/unit/test_validation_fixes.py:191`.
- Constructs a real app via `create_app` with `FakeTranscriber`, `model_loaded_flag` set, and a mock decode function returning valid PCM.
- POSTs to `/transcribe` with filename `"../../etc/evil.m4a"` (contains `/`).
- Asserts `status != 500` (a 500 would indicate an unsanitized suffix crashed `mkstemp`).
- Asserts `status in {200, 400, 401, 413, 415, 422}` — verifies the request flows through the validation pipeline normally, not a server crash.
- This correctly probes the SR-3 fix: if `Path(filename).name` were missing and the raw filename with `/` were passed to `mkstemp(suffix=…)`, the OS would raise `FileNotFoundError` which FastAPI would convert to a 500. The test catches that regression. Correct.

**Result: Both G-5 proof tests exist and assert the correct conditions. SR-6 is CLOSED.**

---

### Verification 3 — No re-opened issues (SR-1..SR-5 still in place)

The G-1 and G-5 changes are limited to reordering two synchronous checks within the `transcribe` handler and adding two test functions. No other code was changed. Confirmed:

- **SR-1 (python-multipart pin):** `pyproject.toml:21` not touched by G-1/G-5. `>=0.0.18` constraint still in place. SR-1 remains CLOSED.
- **SR-2 (loopback host):** `__main__.py` not touched by G-1/G-5. Frozenset validation at lines 30 and 77–80 still in place. SR-2 remains CLOSED.
- **SR-3 (tempfile suffix):** `app.py:284–285` `Path(filename).name` logic not touched by G-1 (G-1 only moved lines 201–206 relative to each other). SR-3 remains CLOSED.
- **SR-4 (filesDir):** Android `RecordingService.kt` not in scope of G-1/G-5. SR-4 remains CLOSED.
- **SR-5 (allowBackup):** Android `AndroidManifest.xml` not in scope of G-1/G-5. SR-5 remains CLOSED.

---

### Findings Table

| Sev | ID | File:Line | Finding | Attack Scenario | Remediation | Status |
|-----|-----|-----------|---------|-----------------|-------------|--------|
| **Minor** | CR-13 (partial) | `app/src/main/java/com/murmur/mobile/data/repository/SettingsRepository.kt:29` + (no UI caller) | `isEncryptionFallback` flag is set on Keystore failure but no production call site reads it and surfaces a user-visible warning. | On a device where the Keystore fails, the bearer token and OpenAI key are stored in plain `SharedPreferences` with no user notification. | Wire `isEncryptionFallback` to a user-visible warning in the settings UI (snackbar or persistent banner). Listed as optional in fix-list-2; does not block shipping. | **Open (minor, pre-existing, unchanged by G-1/G-5)** |

No new findings. SR-6 is now CLOSED by G-5. CR-13 partial remains open as a minor (unchanged from iteration 2; was not in scope for fix-list-2's required items).

---

### What Was Checked (Iteration 3)

1. **Auth is step 1 in POST /transcribe:** `app.py:188–191` — unconditional, before busy-check. CONFIRMED.
2. **Busy-check is step 2, model-not-loaded is step 3:** `app.py:201–206` — in that order, no intervening `await`. CONFIRMED.
3. **File read / real work still post-auth:** `app.py:213` first `await file.read(1)` only reached after steps 1–4 pass. `_run_transcription` only called inside `async with lock` at line 265. CONFIRMED.
4. **G-5 test 1 exists and asserts correctly:** `test_validation_fixes.py:169–183`. CONFIRMED.
5. **G-5 test 2 exists and asserts correctly:** `test_validation_fixes.py:191–236`. CONFIRMED.
6. **SR-1 still closed:** pyproject.toml not touched. CONFIRMED.
7. **SR-2 still closed:** `__main__.py` not touched. CONFIRMED.
8. **SR-3 still closed:** `app.py:284–285` `Path(filename).name` not touched by G-1 reorder. CONFIRMED.
9. **SR-4 still closed:** RecordingService.kt not in scope. CONFIRMED.
10. **SR-5 still closed:** AndroidManifest.xml not in scope. CONFIRMED.
11. **No new injection / authz / secret / resource / crypto issues:** G-1 is a two-line ordering swap between two synchronous `return _error(…)` calls; G-5 adds two read-only test functions. Neither introduces any new attack surface.

---

## Verdict: **PASS**

| Severity | Count | Notes |
|----------|-------|-------|
| Blocker | 0 | None in any iteration |
| Major | 0 | SR-1/SR-2 closed iteration 1; G-1/G-5 verified correct here |
| Minor | 1 open | CR-13 partial (pre-existing, non-blocking) |

G-1 reorder did not weaken auth ordering. G-5 closed SR-6 (both proof tests now present and correct). No prior closed finding was re-opened. No new finding introduced.
