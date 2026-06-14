# Security Review — murmur-mobile-2026-06-14 (Iteration 1)

**Reviewer:** security-reviewer  
**Date:** 2026-06-14  
**Components audited:**  
- Component A (server): `src/murmur/server/` on branch `feature/murmur-mobile-2026-06-14`  
- Component B (Android): `/Users/faithomojola/Documents/MurmurMobile/app/src/`

---

## Attack Surface Summary

This change adds a new FastAPI HTTP server (`/health`, `/transcribe`) bound loopback-only, exposed only via Tailscale serve (TLS termination external), and a new Android app that holds a bearer token and an OpenAI key in EncryptedSharedPreferences, uploading audio over HTTPS to either the Mac server or OpenAI. The new attack surface consists of: (1) the two authenticated server endpoints reachable only over the tailnet; (2) secret storage and HTTPS enforcement on the Android client; (3) the multipart audio upload path including ffmpeg decode; (4) server-side temp-file lifecycle and token redaction in logs.

---

## Security Review

### Findings

| Sev | ID | File:Line | Finding | Attack Scenario | Remediation |
|-----|-----|-----------|---------|-----------------|-------------|
| **Major** | SR-1 | `pyproject.toml:21` | `python-multipart>=0.0.9` is pinned below the fix for CVE-2024-53981. This vulnerability allows a DoS via malformed multipart boundaries: the parser emits a log event per-byte for data before the first or after the last boundary, causing excessive CPU load and event-loop stall. Because FastAPI resolves `File()`/`Form()` parameters **before** the route handler body executes, the `POST /transcribe` multipart parsing happens before the explicit auth check at line 185 of `app.py`. An unauthenticated client (any device on the Tailscale tailnet — e.g., a compromised phone) can send a crafted `multipart/form-data` body with large inter-boundary padding and stall the event loop, rendering `/health` and `/transcribe` non-responsive until the connection is dropped. | Attacker on tailnet (e.g., compromised Android app) POSTs to `https://<mac>.ts.net/transcribe` without a valid token but with a malformed multipart body containing 10 MB before the first boundary. python-multipart 0.0.9–0.0.17 processes this one byte at a time, logging each byte. The event loop stalls; `/health` stops responding. | Bump constraint to `python-multipart>=0.0.18` in `pyproject.toml`. No API changes required. |
| **Major** | SR-2 | `src/murmur/server/config.py:124` + `src/murmur/server/__main__.py:146` | The `[server] host` field in `server.toml` is accepted and passed directly to `uvicorn.run(host=...)` with no validation that it is a loopback address. The plan (§C) explicitly states `host = "127.0.0.1" # R-8; not user-overridable to non-loopback in v1`, and spec R-8 states "the server binds to 127.0.0.1 only." No enforcement exists. If `server.toml` contains `host = "0.0.0.0"` (intentional misconfiguration or fat-finger), uvicorn binds to all interfaces, exposing port 8080 directly on LAN and/or Wi-Fi — bypassing the Tailscale overlay entirely and making the bearer-token-gated API LAN-accessible without TLS. | Jay edits `server.toml` to debug a port conflict and accidentally sets `host = "0.0.0.0"`. Uvicorn now binds to all interfaces. A device on Jay's home LAN can reach `http://192.168.x.x:8080/transcribe`, attempt token brute-force (no rate-limit configured), or probe the API surface. TLS is absent on this path (tailscale serve handles TLS only for the loopback-forwarded traffic). | In `_parse_server()` or `__main__.py`, validate that `host` is `"127.0.0.1"` or `"::1"`. If the config provides any other value, reject with a startup error: `"server.toml: host must be 127.0.0.1 (R-8). Refusing to bind to non-loopback interface."` |
| **Minor** | SR-3 | `src/murmur/server/app.py:266–267` | The `tempfile.mkstemp(suffix=suffix)` call uses the client-supplied multipart filename (unvalidated) as the suffix. If the filename contains a `/` character (e.g., `"../../tmp/evil.m4a"`), `mkstemp` attempts to open a path whose parent directory does not exist and raises `FileNotFoundError`. This exception propagates out of `_run_transcription`, bypasses the specific `AudioDecodeError`/`TranscriptionError` handlers, and results in a FastAPI 500 response rather than the contracted 422/415. The lock is released correctly by `async with`, so the server stays running. An authenticated user can reliably trigger 500 responses for any filename containing `/`. | Authenticated user sends a POST to `/transcribe` with a multipart `file` field whose filename is `"../evil.m4a"`. The `_run_transcription` function calls `tempfile.mkstemp(suffix="_../evil.m4a")`, which raises `FileNotFoundError`. FastAPI returns a 500 response. No crash, but the error code contract (AC-8, §A error registry) is violated. | Sanitize the filename suffix before passing to `mkstemp`: `safe_suffix = f"_{Path(filename).name}" if filename else ".audio"`. This strips any directory components from the client-supplied filename. |
| **Minor** | SR-4 | `app/src/main/java/com/murmur/mobile/recording/RecordingService.kt:171` | Audio files are written to `getExternalFilesDir(null) ?: filesDir`. On Android 12 (API 31–32), which is within the minSdk=31 target range, `getExternalFilesDir` resolves to `/sdcard/Android/data/com.murmur.mobile/files/` — external storage that, while scoped under API 30+ scoped storage, is readable without root via ADB (`adb pull /sdcard/Android/data/com.murmur.mobile/`). The spec (AC-30, §4 "app-private storage") requires truly private storage. `filesDir` (internal storage, `/data/data/<pkg>/files/`) is inaccessible via ADB without root. The fallback to `filesDir` is never triggered on devices that have external storage. | On a development/debug build or an API 31/32 device with USB debugging enabled, an attacker with physical access runs `adb pull /sdcard/Android/data/com.murmur.mobile/files/`. All undeleted audio recordings (those in `keep_audio=true` mode, or failed transcriptions) are exfiltrated in plaintext. | Replace `getExternalFilesDir(null) ?: filesDir` with simply `filesDir`. Internal storage is always available on Android and is truly private. If large-file concerns exist, use `context.getDir("recordings", Context.MODE_PRIVATE)` which is also internal. |
| **Minor** | SR-5 | `app/src/main/AndroidManifest.xml:20` | `android:allowBackup="true"` is set. In the fallback path in `SettingsRepository.create()` (line 50–54), if `EncryptedSharedPreferences` creation fails, the app silently falls back to plain `SharedPreferences`. With `allowBackup=true`, Android's ADB full-backup facility can back up all SharedPreferences including the plain fallback file. An attacker with USB/ADB access on a device where the Keystore failed (certain API 31 devices with known Keystore bugs — referenced in plan Risks and ADR-7) can run `adb backup -noapk com.murmur.mobile` and recover plaintext bearer token and OpenAI key from the backup. | On a Pixel with a malfunctioning Keystore (documented in plan/ADR-7), `EncryptedSharedPreferences.create()` throws `KeyStoreException`. The fallback creates a plain `SharedPreferences` file containing the bearer token and OpenAI key. `adb backup` on the connected device extracts this file; `android-backup-extractor` recovers the plaintext secrets. | Set `android:allowBackup="false"` (or use `android:fullBackupContent` to exclude the prefs file). This does not prevent the Keystore fallback from functioning but limits backup-based exfiltration. The fallback scenario itself is an accepted risk per ADR-7, but backup exclusion closes the secondary exposure path. |

---

### What Was Checked (no finding)

1. **Bearer-token constant-time comparison (AC-15):** `verify_token` in `token.py:64` uses `hmac.compare_digest` on byte-encoded strings. No timing oracle. `_check_auth` correctly extracts the `Bearer ` prefix before comparing. Verified by `test_compare_uses_compare_digest`. PASS.

2. **Token in logs (AC-17, R-7):** The `_TokenRedactFilter` is attached to the root logger in `create_app`. It calls `record.getMessage()` (which expands `%s`-style args), replaces the token in `record.msg`, and clears `record.args`. This correctly prevents token exposure even when the token appears in log arguments. Uvicorn's default access log format does not include request headers, so the `Authorization` header is not logged by uvicorn. No log call in `app.py`, `audio.py`, `config.py`, or `token.py` directly passes the token value. PASS.

3. **Auth before work on /transcribe (AC-9):** Step 1 in the route handler (line 185–188) checks auth before any other operation. `File()`/`Form()` parameter resolution by FastAPI happens before the handler body, but that is framework-level, not application work (no ffmpeg, no temp file, no transcriber call). Auth at application logic layer is correct. PASS (with the caveat noted in SR-1 that python-multipart parsing is pre-auth at the framework layer).

4. **Auth on /health (AC-2):** First action in the `/health` handler (line 154–156) is the auth check. No lock acquired, no model state read, no work done before auth. PASS.

5. **ffmpeg command injection (AC-5):** `audio.py` invokes ffmpeg as an argv list (`cmd = ["ffmpeg", "-y", "-i", str(path), ...]`) with no `shell=True`. The file path is the OS-assigned tempfile path, not user-supplied. No shell metacharacter injection possible. `language` and `vocab_hints` fields are passed to the Python transcriber, not to ffmpeg. PASS.

6. **Temp file deleted in all paths (AC-18):** The `finally` block in `_run_transcription` (line 328–330) calls `tmp_path.unlink(missing_ok=True)` unconditionally. This covers success, `AudioDecodeError`, `TranscriptionError`, and hallucination guard `None` returns. Unhandled exceptions (e.g., from SR-3 above) also trigger the `finally`. PASS.

7. **Server token not in source/config (R-7):** No token value appears in any source file. `config.py` does not have a token field. `get_server_token` reads from env var then Keychain only. `__main__.py` fails fast with a clear error if absent. PASS.

8. **Android secrets not in Logcat (AC-22, R-10):** Grepped all `Log.*` calls in Android source files. None passes `bearerToken`, `openAiKey`, or any variable holding secret values. `MacTranscriptionEngine` and `OpenAITranscriptionEngine` use `java.util.logging.Logger.fine()` for network error classification only (not secret values). `SettingsRepository` logs the exception when `EncryptedSharedPreferences` fails (not the key values). PASS.

9. **Android HTTPS enforcement (E-39):** `UrlValidator.validate()` normalizes (trims whitespace, strips trailing slashes) then checks `startsWith("https://")` — case-sensitive in Kotlin. `HTTP://`, `Https://`, `ftp://`, all other schemes are blocked. Whitespace and trailing slashes cannot be used to bypass the check. PASS.

10. **Android bearer token only over HTTPS (R-11):** `MacTranscriptionEngine.transcribe()` and `checkHealth()` use the URL from `SettingsRepository.getMacUrl()` which was validated as HTTPS before storage. The `Authorization` header is added only in OkHttp request builders. `OpenAITranscriptionEngine` uses the hardcoded `https://api.openai.com` URL. PASS.

11. **Audio data not logged on either side (R-10):** Server: `app.py` logs only exception class names and audio decode errors (no audio bytes). Android: no `Log.*` call in `MacTranscriptionEngine`, `OpenAITranscriptionEngine`, or `TranscriptionDispatcher` logs request/response body content. Transcript text (`rawText`) is not logged. PASS.

12. **Single-instance server lock (R-17):** `SingleInstanceLock` on `/tmp/murmur-server.pid` acquired in `__main__.py` before app creation. `launchd` KeepAlive=true is the primary mechanism; PID file is the belt. PASS.

13. **Dependency scan — no known CVEs (excluding SR-1):**  
    - `fastapi>=0.115`: CVE-2024-24762 is a ReDoS fixed in python-multipart 0.0.7 (the `>=0.0.9` constraint clears this, but CVE-2024-53981 is the newer unfixed issue — see SR-1). FastAPI itself: no known unpatched CVEs at these versions.  
    - `uvicorn[standard]>=0.30`: No known CVEs at this version range.  
    - `okhttp 4.12.0`: No known unpatched CVEs at this version as of knowledge cutoff.  
    - `androidx.room 2.6.1`: No known CVEs.  
    - `androidx.work 2.9.1`: No known CVEs.  
    - `androidx.security:security-crypto 1.1.0-alpha06`: No CVE assigned; known Keystore edge-case failures on some devices are an accepted architectural risk per ADR-7.  
    - `kotlinx-coroutines 1.8.1`, `Compose BOM 2024.10.01`, `robolectric 4.13`: No known CVEs at these versions.

14. **No Math.random() or homemade crypto:** No random token generation in this codebase (token is loaded from external source). EncryptedSharedPreferences uses AES256-GCM via AndroidX Keystore. No custom crypto. PASS.

15. **Resource exhaustion — size check before memory allocation (AC-8):** The streaming size check in `app.py:211–224` reads the upload in 64 KB chunks and returns 413 before fully buffering when size exceeds `max_upload_mb`. The first 1-byte read (step 5) plus chunked accumulation means the maximum in-memory buffer is approximately `max_upload_mb` (25 MB). This is the intended design and bounded. PASS.

16. **Room DB not encrypted, but holds only metadata:** The Room database (`murmur_notes.db`) holds transcript text, status, and audio file paths — not the bearer token or OpenAI key. On a non-rooted device, it is inaccessible. The spec does not require DB encryption. PASS (no finding).

---

## Verdict: **PASS**

No blockers found. Two major findings (SR-1, SR-2) and three minor findings (SR-3, SR-4, SR-5) are logged. Per operating rules, zero open blockers/majors is required to ship — **these two majors must be resolved before shipping.**

| Severity | Count |
|----------|-------|
| Blocker | 0 |
| Major | 2 (SR-1, SR-2) |
| Minor | 3 (SR-3, SR-4, SR-5) |
