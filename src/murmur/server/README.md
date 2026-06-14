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
                                              ├── GET  /health
                                              └── POST /transcribe
                                                   └── ffmpeg → MlxWhisperTranscriber
```

## Prerequisites

```bash
# Python dependencies (already in pyproject.toml)
uv sync

# ffmpeg (required at runtime for audio decode)
brew install ffmpeg

# tailscale (required for HTTPS exposure over your tailnet)
brew install tailscale
```

## Configuration

Copy the example config to the default location:

```bash
mkdir -p "~/Library/Application Support/Murmur"
cat > "~/Library/Application Support/Murmur/server.toml" << 'EOF'
[server]
host = "127.0.0.1"
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

## Bearer Token Setup

The bearer token is **never stored in config files**. Set it via environment
variable or macOS Keychain:

**Option A — environment variable (for testing):**
```bash
export MURMUR_SERVER_TOKEN="your-secret-token"
```

**Option B — macOS Keychain (recommended for production):**
```bash
security add-generic-password -s murmur-server-token -a "$USER" -w "your-secret-token"
```

**Verify:**
```bash
security find-generic-password -s murmur-server-token -w
```

## Running the Server

```bash
# Directly (foreground — for development)
MURMUR_SERVER_TOKEN="your-token" uv run python -m murmur.server

# Via launchd (production — restarts on crash, starts at login)
# See "launchd Installation" below.
```

## launchd Installation (AC-19, R-16)

The launchd agent uses `caffeinate -i` to prevent Mac sleep during transcription
and `KeepAlive = true` to restart the server on crash.

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

## Tailscale Serve Setup (R-9)

The server binds to `127.0.0.1:8080` (loopback only). Expose it to your Tailscale
tailnet over HTTPS:

```bash
# Enable tailscale serve (proxies HTTPS → http://127.0.0.1:8080)
tailscale serve --bg 127.0.0.1:8080

# Verify HTTPS URL
tailscale status | grep https
```

Your Murmur Mobile app should then use `https://<machine>.<tailnet>.ts.net` as
the Mac URL in Settings.

## HTTP API

### GET /health

**Auth:** `Authorization: Bearer <token>`

**200 OK:**
```json
{"status": "ok", "model_loaded": true, "model": "large-v3", "version": "1.0.0"}
```

**401 Unauthorized:**
```json
{"error_code": "unauthorized", "detail": "Invalid or missing bearer token"}
```

### POST /transcribe

**Auth:** `Authorization: Bearer <token>`
**Content-Type:** `multipart/form-data`

**Fields:**
| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `file` | file (m4a/aac/wav/mp3/ogg/opus) | yes | — | audio to transcribe |
| `language` | text | no | `"en"` | BCP-47 code |
| `vocab_hints` | text (comma-separated) | no | server config defaults | pronunciation hints |

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

**Error responses:** all errors return `{"error_code": "<string>", "detail": "<message>"}`.

**Error-code registry:**
| Code | HTTP | Meaning |
|------|------|---------|
| `unauthorized` | 401 | Bad or missing bearer token |
| `model_busy` | 503 | Another transcription is in progress |
| `model_not_loaded` | 503 | Model is still loading at startup |
| `missing_file` | 400 | No `file` field in request |
| `empty_file` | 400 | File has zero bytes |
| `file_too_large` | 413 | File exceeds `max_upload_mb` |
| `unsupported_media_type` | 415 | Extension/MIME not in allowed set |
| `audio_decode_failed` | 422 | ffmpeg could not decode the audio |
| `transcription_failed` | 422 | mlx-whisper raised an error |
| `no_speech_detected` | 422 | No speech survived the hallucination guard |

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
