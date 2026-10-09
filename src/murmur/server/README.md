# Murmur Server

FastAPI HTTP API that wraps the existing `mlx-whisper` transcriber and exposes
`GET /health` + `POST /transcribe` over a Tailscale HTTPS overlay.

## Architecture

```
Pixel (Murmur Mobile) ──HTTPS over Tailscale──► tailscale serve (TLS)
                                                     │
                                                     ▼
                                              127.0.0.1:8080  (server binds loopback only — R-8)
                                              FastAPI app
                                              ├── GET  /health   (lock-free, <500 ms always)
                                              └── POST /transcribe  (single-flight asyncio.Lock)
                                                   └── ffmpeg → MlxWhisperTranscriber
```

The server binds to `127.0.0.1` only. It is never directly reachable from outside
the Mac. `tailscale serve` provides the encrypted HTTPS overlay; the server does not
terminate TLS itself.

## Prerequisites

```bash
# Python dependencies (already in pyproject.toml)
uv sync

# ffmpeg (required at runtime for audio decode; not needed for tests)
brew install ffmpeg

# tailscale (required for HTTPS exposure over your tailnet; deploy-time step)
brew install tailscale
```

## Configuration

The server loads config from `server.toml`. The default path is:
```
~/Library/Application Support/Murmur/server.toml
```

Override with the `MURMUR_SERVER_CONFIG` environment variable:
```bash
export MURMUR_SERVER_CONFIG=/path/to/my-server.toml
```

If the file is missing or malformed, the server starts with all defaults and logs
a warning. It does not crash.

Copy this example to the default location and edit as needed:

```bash
mkdir -p "~/Library/Application Support/Murmur"
cat > "~/Library/Application Support/Murmur/server.toml" << 'EOF'
[server]
host = "127.0.0.1"          # loopback only — not user-overridable to non-loopback
port = 8080
max_upload_mb = 25
log_path = "~/Library/Logs/murmur-server.log"

[stt]
model = "large-v3"
language = "en"
vocab_hints = ["Rivy", "Yassir", "Refendr", "Chidera", "Kemi", "Vaasa", "GKE", "BigQuery", "FIN-FSA"]

[clean]
enabled = false   # parsed and ignored — /clean is out of scope for v1
EOF
```

### Full `server.toml` schema

| Section | Key | Default | Notes |
|---------|-----|---------|-------|
| `[server]` | `host` | `"127.0.0.1"` | Loopback only. Non-loopback values cause startup failure. |
| `[server]` | `port` | `8080` | |
| `[server]` | `max_upload_mb` | `25` | Requests exceeding this return HTTP 413. |
| `[server]` | `busy_wait_s` | `0` | Fixed 0 in v1 — immediate 503 on lock contention. |
| `[server]` | `log_path` | `"~/Library/Logs/murmur-server.log"` | |
| `[stt]` | `model` | `"large-v3"` | Passed to `MlxWhisperTranscriber`. |
| `[stt]` | `language` | `"en"` | Default BCP-47 language for transcription. |
| `[stt]` | `vocab_hints` | (PRD §5.5 list) | Used as `initial_prompt` when not supplied by the client. |
| `[clean]` | (any) | — | Entire section is parsed and silently ignored. `/clean` is not implemented (v1.5). |

Missing file → all defaults + warning log. Malformed TOML → all defaults + warning log.
Unknown keys → warning log, ignored. No crash in any of these cases.

## Bearer Token Setup

The bearer token is **never stored in config files, logs, or source code** (R-7).
It is read from the `MURMUR_SERVER_TOKEN` environment variable (checked first) or
from the macOS Keychain under service name `murmur-server-token`.

If neither source provides a token, the server refuses to start with:
```
Bearer token not configured. Set MURMUR_SERVER_TOKEN env var or add to Keychain.
```

**Option A — environment variable (development):**
```bash
export MURMUR_SERVER_TOKEN="your-secret-token"
```

**Option B — macOS Keychain (recommended for launchd production use):**
```bash
security add-generic-password -s murmur-server-token -a "$USER" -w "your-secret-token"
```

**Verify the Keychain entry:**
```bash
security find-generic-password -s murmur-server-token -w
```

Token comparison uses `hmac.compare_digest` (constant-time). The token string is
never written to any log at any level — a logging filter redacts it to `[REDACTED]`
if it appears in any log record.

## Running the Server

```bash
# Directly (foreground — for development/testing)
MURMUR_SERVER_TOKEN="your-token" uv run python -m murmur.server
```

For production use, install the launchd agent (see below).

## launchd Installation

The launchd agent keeps the server running at login and restarts it on crash.
`caffeinate -i` prevents the Mac from sleeping during transcription.

The plist label is `com.jay.murmur-server`. It is written to
`~/Library/LaunchAgents/com.jay.murmur-server.plist` with:
- `KeepAlive = true` (restarts on crash)
- `RunAtLoad = true` (starts at login)
- `ProgramArguments = [/usr/bin/caffeinate, -i, <python>, -m, murmur.server]`

```bash
# Install the server launchd agent
uv run python -c "from murmur.server.launchd_server import install_server_agent; install_server_agent()"

# Verify it is running
launchctl list | grep murmur-server

# View logs
tail -f ~/Library/Logs/murmur-server.log

# Uninstall
uv run python -c "from murmur.server.launchd_server import uninstall_server_agent; uninstall_server_agent()"
```

> Note: the bearer token must be in the Keychain (not the env var) for launchd to
> pick it up. Environment variables set in your shell session are not inherited by
> launchd agents.

## Tailscale Serve Setup

The server binds to `127.0.0.1:8080` (loopback only). Expose it to your Tailscale
tailnet over HTTPS using `tailscale serve`. This is a one-time setup step that
runs on the Mac:

```bash
# (Deploy-time step — requires Tailscale installed and authenticated)
tailscale serve --bg https / http://127.0.0.1:8080
```

This proxies `https://<machine>.<tailnet>.ts.net` → `http://127.0.0.1:8080`. TLS
is terminated by Tailscale; the server handles plain HTTP behind the proxy.

Your Murmur Mobile app should then use `https://<machine>.<tailnet>.ts.net` as
the Mac URL in Settings.

Verify from another device on the tailnet:
```bash
# (Deploy-time verification — requires Tailscale network access)
curl -H "Authorization: Bearer <your-token>" \
  https://<machine>.<tailnet>.ts.net/health
# Expected: {"status":"ok","model_loaded":true,"model":"large-v3","version":"1.0.0"}
```

## HTTP API

### GET /health

**Auth:** `Authorization: Bearer <token>` (required)

**200 OK:**
```json
{"status": "ok", "model_loaded": true, "model": "large-v3", "version": "1.0.0"}
```

`model_loaded` is `false` while the model is still loading at startup. Health
**never** acquires the transcription lock — it reads a non-blocking `threading.Event`
flag and returns in well under 500 ms regardless of any in-progress transcription.

**401 Unauthorized:**
```json
{"error_code": "unauthorized", "detail": "Invalid or missing bearer token"}
```

### POST /transcribe

**Auth:** `Authorization: Bearer <token>` (required)
**Content-Type:** `multipart/form-data`

**Fields:**
| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `file` | file (m4a/aac/wav/mp3/ogg/opus) | yes | — | audio to transcribe |
| `language` | text | no | `"en"` | BCP-47 code |
| `vocab_hints` | text (comma-separated) | no | server `[stt]` defaults | pronunciation hints passed as `initial_prompt` |

**Validation order (each step short-circuits before the next):**
1. Auth check → 401 if missing/wrong
2. Single-flight lock check → 503 `model_busy` if another transcription is in progress
3. Model loaded check → 503 `model_not_loaded` if startup is incomplete
4. File field presence → 400 `missing_file`
5. File size zero → 400 `empty_file`
6. File size > `max_upload_mb` → 413 `file_too_large`
7. Extension/MIME check → 415 `unsupported_media_type`
8. ffmpeg decode → 422 `audio_decode_failed` on failure
9. `MlxWhisperTranscriber.transcribe` → 422 `transcription_failed` on error
10. `clean_result()` hallucination guard → 422 `no_speech_detected` if nothing survives
11. Success → 200

**200 OK:**
```json
{
  "text": "transcribed text",
  "segments": [{"text": "...", "no_speech_prob": 0.0, "compression_ratio": 1.0, "avg_logprob": -0.3}],
  "language": "en",
  "duration_sec": 30.0,
  "model": "large-v3",
  "engine": "mac"
}
```

`duration_sec` is derived from the decoded PCM length (`len(pcm) / 16000`), not
from container metadata. `engine` is always `"mac"`.

**Error body shape (all non-2xx):**
```json
{"error_code": "<string>", "detail": "<human message>"}
```

**Error-code registry:**
| Code | HTTP | Meaning |
|------|------|---------|
| `unauthorized` | 401 | Bad or missing bearer token |
| `model_busy` | 503 | Another transcription is in progress (immediate; no queuing) |
| `model_not_loaded` | 503 | Model is still loading at startup |
| `missing_file` | 400 | No `file` field in request |
| `empty_file` | 400 | File has zero bytes |
| `file_too_large` | 413 | File exceeds `max_upload_mb` (default 25 MB) |
| `unsupported_media_type` | 415 | Extension/MIME not in `{m4a, aac, wav, mp3, ogg, opus}` |
| `audio_decode_failed` | 422 | ffmpeg could not decode the audio (or ffmpeg is not installed) |
| `transcription_failed` | 422 | `MlxWhisperTranscriber` raised `TranscriptionError` |
| `no_speech_detected` | 422 | No speech survived the `clean_result()` hallucination guard |

**Edge cases:**
- `application/octet-stream` MIME type with a valid extension (e.g., `.m4a`) is accepted (E-5).
- A file that is exactly `max_upload_mb` bytes is accepted; one byte over is rejected.
- `vocab_hints` absent → server uses `[stt] vocab_hints` defaults. Empty/whitespace entries are stripped before passing to `build_initial_prompt()`.

## Concurrency design

Only one transcription runs at a time (single-flight). The design — as built and
validated — works as follows:

1. `POST /transcribe` checks `lock.locked()` at step 2 (synchronous, no await).
2. If the lock is free: `await lock.acquire()` immediately follows with no
   intervening await expression. In CPython's asyncio, `Lock.acquire()` on an
   uncontested lock sets `_locked=True` and returns without suspending — the
   event loop does not yield to any other coroutine between the check and the
   acquire. The first request atomically owns the lock.
3. All subsequent steps (file reads, ffmpeg decode, transcription) run under the
   held lock. A concurrent request seeing `lock.locked()==True` returns 503
   `model_busy` immediately, before reading any upload bytes.
4. The lock is released in a `finally` block regardless of success or failure.
5. `GET /health` never references the lock. It reads a `threading.Event` flag
   (`model_loaded_flag.is_set()`) and always returns within 500 ms, even while a
   transcription is running.

The blocking `MlxWhisperTranscriber.transcribe()` call runs in a thread pool via
`asyncio.get_event_loop().run_in_executor(None, ...)` so the event loop remains
free for concurrent `/health` requests during transcription (ADR-2).

This design is enforced by `test_concurrent_transcribe_race_proof__R3_1_AC14` in
`tests/unit/test_server_singleflight.py`, which injects a guaranteed event-loop
yield at the file-read point to prove no TOCTOU window exists.

## Runtime dependency: ffmpeg

`ffmpeg` is required at runtime for `POST /transcribe`. It is invoked as a
subprocess:
```
ffmpeg -i <input> -ac 1 -ar 16000 -f f32le -
```
to decode uploaded audio to float32 mono 16 kHz PCM, which `MlxWhisperTranscriber`
expects.

If `ffmpeg` is not installed, the server still starts and `/health` returns 200.
`POST /transcribe` returns 422 `audio_decode_failed` with a log message indicating
the binary is missing.

Install: `brew install ffmpeg`

## Logs

Structured logs are written to `~/Library/Logs/murmur-server.log` by default
(also to stderr). The log path is configured via `[server] log_path` in
`server.toml`.

The bearer token, audio bytes, and transcript text are **never** logged at any
level (R-7, R-10). A logging filter redacts the token to `[REDACTED]` if it
appears in any log record.

## Testing

```bash
# Run all non-hardware tests (no ffmpeg, tailscale, or model weights needed)
uv run pytest

# Run the live smoke test (requires running server + Tailscale + real audio file)
MURMUR_TEST_URL="https://mymac.tailXXXX.ts.net" \
MURMUR_TEST_TOKEN="your-token" \
MURMUR_TEST_AUDIO="/path/to/voice-note.m4a" \
uv run pytest -m hardware tests/integration/test_server_live.py -v
```

Server tests use `FakeTranscriber` (from `tests/fakes.py`) and a mocked ffmpeg
subprocess — no ffmpeg binary, Tailscale, or model weights are needed in CI.
