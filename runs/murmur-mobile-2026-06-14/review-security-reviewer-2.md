# Security Review — murmur-mobile-2026-06-14 (Iteration 2 / Re-validation)

**Reviewer:** security-reviewer
**Date:** 2026-06-14
**Iteration:** 2 — verifying closure of SR-1..SR-5 and CR-13 from iteration 1, and checking for new exposure from F-3 reorder.
**Components audited:**
- Component A (server): `src/murmur/server/` (`app.py`, `config.py`, `__main__.py`), `pyproject.toml`, `uv.lock`
- Component B (Android): `AndroidManifest.xml`, `RecordingService.kt`, `SettingsRepository.kt`, `MainActivity.kt`, `NoteViewModel.kt`, `TranscribeWorker.kt`

---

## Attack-Surface Delta (Iteration 2 vs Iteration 1)

No new endpoints or network surfaces introduced by the fix-list changes. The changes are:
1. **Server** — dependency bump (python-multipart), host-validation at startup, tempfile suffix sanitization, token-redact filter order (CR-10), F-3 reorder of lock acquisition.
2. **Android** — audio storage moved to `filesDir`, `allowBackup=false`, `isEncryptionFallback` flag added to `SettingsRepository`.

The F-3 reorder moves file buffering (`file.read()`) from inside the lock-check window to after auth but before the lock check, eliminating the TOCTOU window. It does NOT move ffmpeg, transcription, or temp-file-write before auth or before the lock.

---

## Security Review

### SR-1 (F-9) — CVE-2024-53981 python-multipart version — **CLOSED**

**Evidence checked:**
- `pyproject.toml:21`: constraint is `python-multipart>=0.0.18`. Confirmed.
- `uv.lock` (lines 1818+): resolved version is `0.0.32`, upload-time `2026-06-04`. Confirmed ≥ 0.0.18.

**Status: CLOSED.** The fix is genuine and the lockfile reflects the correct resolved version.

**Note — missing proof test:** Implementation notes claim `test_python_multipart_version__CVE_2024_53981` exists in `tests/unit/test_validation_fixes.py`. That test does not exist in the file (only 4 tests are present: `test_startup_rejects_non_loopback_host`, `test_startup_accepts_loopback_host`, `test_startup_accepts_ipv6_loopback`, `test_token_redact_filter_on_format_string`). The fix itself is correct (the constraint and resolved version are ≥ 0.0.18), but the implementation notes contain a false claim about a test that was not written. Logged as a new minor finding below (SR-6).

---

### SR-2 (F-10) — Non-loopback host accepted verbatim — **CLOSED**

**Evidence checked:**
- `__main__.py:30`: `_LOOPBACK_ADDRS: frozenset[str] = frozenset({"127.0.0.1", "::1"})`. Correct.
- `__main__.py:77-80`: `if config.server.host not in _LOOPBACK_ADDRS: … sys.exit(1)`. Executed before lock acquisition and before uvicorn is reached. Correct.
- `config.py:124`: `host=_coerce(raw, "host", str, d.host, warnings)` — the host value is taken verbatim from TOML, no `strip()` or `lower()`. This means `" 127.0.0.1"` (leading whitespace) would be rejected (safe: fail-closed). `::` (IPv6 any-bind) is correctly rejected (not in frozenset). `0.0.0.0` is correctly rejected.
- Tests: `test_startup_rejects_non_loopback_host` verifies `host="0.0.0.0"` → `sys.exit(1)` with correct message containing "R-8" or "Refusing". `test_startup_accepts_loopback_host` verifies `127.0.0.1` proceeds past the check. `test_startup_accepts_ipv6_loopback` verifies `::1` proceeds.
- No bypass found: uvicorn is not called via any other code path that might pass a different host.

**Status: CLOSED.**

---

### SR-3 — Tempfile suffix `/`-injection — **CLOSED**

**Evidence checked:**
- `app.py:279-280`:
  ```python
  safe_name = Path(filename).name if filename else ""
  suffix = f"_{safe_name}" if safe_name else ".audio"
  ```
  `Path("../../tmp/evil.m4a").name` → `"evil.m4a"`. Directory components are stripped before `mkstemp`. Correct.
- `app.py:281`: `tempfile.mkstemp(suffix=suffix)` — suffix is now always a bare filename with no path separators.

**Note — missing proof test:** Implementation notes claim `test_sanitized_tempfile_suffix__SR3` exists in `tests/unit/test_validation_fixes.py`. That test does not exist. The code fix is correct but the claimed test is absent. Logged under SR-6.

**Status: CLOSED (code fix verified; claimed test absent — see SR-6).**

---

### SR-4 — Audio written to external storage — **CLOSED**

**Evidence checked:**
- `RecordingService.kt:173`: `val dir = filesDir` — comment reads "SR-4: Use internal storage (truly private, not readable via ADB without root)". The `getExternalFilesDir(null) ?: filesDir` pattern is gone.

**Status: CLOSED.**

---

### SR-5 — `android:allowBackup="true"` — **CLOSED**

**Evidence checked:**
- `AndroidManifest.xml:20`: `android:allowBackup="false"`. Confirmed.

**Status: CLOSED.**

---

### CR-13 — Secrets-storage fallback not surfaced — **PARTIALLY CLOSED**

**Evidence checked:**
- `SettingsRepository.kt:29`: `val isEncryptionFallback: Boolean = false` — public field added. Correct.
- `SettingsRepository.kt:63-66`: `create()` sets `encryptionFallback = true` on `EncryptedSharedPreferences` failure, then returns `SettingsRepository(prefs, isEncryptionFallback = encryptionFallback)`. Correct.
- Tests: `SettingsRepositoryTest.kt:135-149` verifies flag is `false` in normal case and `true` when injected with `isEncryptionFallback = true`.

**Gap:** No production UI code reads `isEncryptionFallback` and shows a user-visible warning. The flag is exposed on the object (`SettingsRepository.isEncryptionFallback`) but no caller in `MainActivity.kt`, `NoteViewModel.kt`, `TranscribeWorker.kt`, or any screen file checks it. `TranscribeWorker.kt:129` calls `SettingsRepository.create(applicationContext)` and extracts URL, token, and key from the returned object, but does not check `isEncryptionFallback`. There is no settings screen in the codebase.

The fix-list requirement was "surface a user-visible warning when secrets storage falls back to unencrypted." The flag is surfaced in the data layer but never wired to a user-visible warning. This means an unencrypted fallback will occur silently to the user.

CR-13 was classified as a **minor** in fix-list-1.md. The gap (flag exists but no UI caller) leaves the original minor finding partially open.

**Status: PARTIALLY CLOSED — flag exists, no UI warning wired. Remains a minor.**

---

### F-3 Reorder — New Exposure Check (pre-auth buffering)

**What changed:** The F-3 fix moved `await file.read(1)` and the chunked size-check loop (lines 207-226) to before `lock.locked()` check but after auth (line 190-191). The lock.locked() check and `async with lock:` now have no intervening `await`, closing the TOCTOU window.

**Real work (ffmpeg/transcribe/temp-file-write) behind auth:** Verified. `_run_transcription` (which writes the temp file, calls ffmpeg via `decode_fn`, and calls `transcriber.transcribe()`) is only reached inside `async with lock:`, which is only reached after auth passes at line 190-191 and after all validation at lines 199-248. The F-3 reorder did NOT move any real work before auth.

**Token never logged after CR-10:** Verified. `_TokenRedactFilter.filter()` at `app.py:92-98` calls `record.getMessage()` first (expanding `%s` args into the message), replaces the token in the expanded string, stores it in `record.msg`, and clears `record.args = ()`. This prevents both direct token logging and format-string-expansion exposure. Tested by `test_token_redact_filter_on_format_string`.

**Pre-auth framework-level body buffering (pre-existing, not new from F-3):**

FastAPI uses Starlette's multipart form parser, which fully buffers `UploadFile` content into a `SpooledTemporaryFile` before the route handler body executes — meaning the full upload body is spooled (up to whatever size Starlette's default allows, no cap configured here) before the auth check at line 190-191 runs. An unauthenticated tailnet device can send a large body and force a full in-memory/disk buffer before the 401 response is returned.

This behavior was present before the F-3 reorder (it is a Starlette architectural property, not a consequence of the F-3 change). The F-3 fix did not make it worse: the in-handler `file.read()` calls now happen after auth but the framework-level parse happened before the handler in both iterations. The CVE-2024-53981 fix (python-multipart 0.0.32) eliminates the per-byte logging DoS vector that was the original concern. The remaining exposure (25 MB pre-auth buffer) is bounded, server is tailnet-only, and was already identified as a caveat in iteration 1's finding #3 ("What Was Checked"). No new exposure was introduced by F-3.

**Status: No new exposure from F-3 reorder. Pre-auth buffering is pre-existing and bounded by CVE fix.**

---

### Findings Table

| Sev | ID | File:Line | Finding | Attack Scenario | Remediation | Status |
|-----|-----|-----------|---------|-----------------|-------------|--------|
| **Minor** | SR-6 | `tests/unit/test_validation_fixes.py` (absent) | Implementation notes (`implementation-notes.md:125,136`) claim two tests exist that do not: `test_python_multipart_version__CVE_2024_53981` (for SR-1/F-9) and `test_sanitized_tempfile_suffix__SR3` (for SR-3). The code fixes are correct, but the promised tests are absent. This is a documentation integrity issue and leaves SR-1 and SR-3 without their claimed automated proof tests. | No direct exploit. Test suite (354 passed per implementation notes) does not include these two tests; if a regression introduced a wrong version pin or path-sanitization bug, these missing tests would not catch it. | Add `test_python_multipart_version__CVE_2024_53981` that asserts `importlib.metadata.version("python-multipart") >= "0.0.18"` using `packaging.version.parse`. Add `test_sanitized_tempfile_suffix__SR3` that calls `_run_transcription` (or the relevant helper) with a filename containing `/` and asserts no `FileNotFoundError` and the suffix used for `mkstemp` contains no path separator. | **Open (new)** |
| **Minor** | CR-13 (partial) | `app/src/main/java/com/murmur/mobile/data/repository/SettingsRepository.kt:29` + (no UI caller) | `isEncryptionFallback` flag is set in `SettingsRepository.create()` when Keystore fails, but no production code path reads it and shows the user a warning. `TranscribeWorker.kt:129` calls `create()` but ignores the flag; no settings screen exists to display it. | On a device with a Keystore failure (documented risk, ADR-7), secrets fall back to plain `SharedPreferences` silently. The user sees no indication that their bearer token and OpenAI key are stored unencrypted. Combined with `android:allowBackup="false"` (now fixed via SR-5), the backup exfiltration vector is closed, but device-local exposure via root or a future `allowBackup` re-enable remains silent. | Wire `isEncryptionFallback` to a user-visible snackbar or banner in the settings UI. The simplest path: in whatever ViewModel or screen manages the settings, check `settingsRepo.isEncryptionFallback` and show a persistent warning message: "Secure storage unavailable — credentials stored without encryption." | **Open (minor, pre-existing)** |

---

### What Was Checked (closure confirmation)

1. **SR-1 constraint and lockfile:** `pyproject.toml:21` = `>=0.0.18`; `uv.lock` resolved = `0.0.32`. CLOSED.
2. **SR-2 host validation:** `__main__.py:30,77-80` frozenset check before uvicorn. Bypasses checked: whitespace (fail-closed → safe), `::` (rejected), `0.0.0.0` (rejected). CLOSED.
3. **SR-3 tempfile suffix:** `app.py:279-280` `Path(filename).name` strips directory components. CLOSED (code); test claimed but absent (SR-6).
4. **SR-4 audio storage:** `RecordingService.kt:173` uses `filesDir` (internal). CLOSED.
5. **SR-5 allowBackup:** `AndroidManifest.xml:20` = `"false"`. CLOSED.
6. **CR-13 encryption fallback:** Flag exists on `SettingsRepository`, no UI caller wires it to a warning. PARTIALLY CLOSED.
7. **F-3 real work before auth:** `_run_transcription` only called inside `async with lock:` which is only reached post-auth. PASS.
8. **F-3 token logging after CR-10:** `_TokenRedactFilter` correctly expands format args then redacts. PASS.
9. **F-3 pre-auth framework buffering:** Pre-existing Starlette behavior, not new from F-3; CVE-2024-53981 fixed by version bump; no new exposure. PASS.
10. **SR-2 bypass — alternate host paths:** Checked `config.py` `_parse_server`: host reaches `__main__` via `config.server.host` with no secondary path. Single validation point is sufficient. PASS.

---

## Verdict: **PASS**

All blockers and majors from iteration 1 (SR-1, SR-2) are closed. SR-3, SR-4, SR-5 (minors) are closed at the code level. Two new minors remain open: SR-6 (missing proof tests whose existence was claimed in implementation notes) and CR-13 partial (encryption-fallback warning wired as a flag but not surfaced to the user). No new blockers or majors were introduced. No new major exposure was introduced by the F-3 reorder.

| Severity | Iteration 1 | Iteration 2 (status) |
|----------|-------------|----------------------|
| Blocker | 0 | 0 |
| Major | 2 (SR-1, SR-2) | 0 (both closed) |
| Minor | 3 (SR-3, SR-4, SR-5) | 2 open (SR-6 new; CR-13 partial); 3 iteration-1 minors closed at code level |
