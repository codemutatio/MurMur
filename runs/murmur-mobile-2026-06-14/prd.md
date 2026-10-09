# PRD — "Murmur Mobile": Android Voice Capture + Self-Hosted Transcription API
**Owner:** Jay (single user, personal tool — not shipping to anyone else)
**Devices:** Google Pixel (Android) as client; MacBook Pro M4 Pro as the transcription server
**Depends on:** existing **Murmur** macOS app (reuses its mlx-whisper transcription module)
**Builder:** Claude Code, end-to-end
**Status:** v1 spec — architecture locked, ready to build
---
## 1. Problem & Goal
Jay's accent is mis-transcribed by commercial dictation tools. The Whisper large-v3 model running locally in Murmur on his Mac handles it well, and OpenAI's `gpt-4o-transcribe` (the ChatGPT voice-input engine family) handles it well too. He wants the same quality on his phone for capturing voice notes on the go, then pulling clean text into Claude.
On-device Whisper on a Pixel is constrained to small models that degrade exactly on accented speech, so **transcription should not run on the phone**. Instead:
> **The phone captures audio and sends it to a transcription API. The primary API is Murmur on Jay's Mac (full large-v3, free, the accent quality he already trusts), reached over Tailscale. When the Mac is unreachable, the app automatically falls back to the OpenAI API. Recording always works offline; transcription is queued and resolves against whichever engine is reachable.**
### Goal
A two-part system:
1. **Murmur Server** — a small HTTP service on the Mac wrapping Murmur's transcriber.
2. **Murmur Mobile** — an Android app that records voice notes, transcribes them via the API (Mac-first, OpenAI-fallback), and lets Jay copy/share/clean the text.
### Non-goals (v1)
- No on-device transcription on the phone
- No always-on ambient recording (intentional capture sessions only — see §3)
- No real-time/streaming transcription (batch only: record → send → response)
- No public internet exposure of the Mac (Tailscale only)
- No multi-user, no account system, no Play Store distribution (sideload/personal build)
- No speaker diarization
---
## 2. Architecture
```
┌─────────────────────────── PIXEL (Murmur Mobile, Kotlin) ───────────────────────────┐
│  Record button → Foreground service (mic) → AAC/m4a, mono, 16 kHz                    │
│         │                                                                            │
│         ▼                                                                            │
│  Local Outbox (Room DB): {audio, status: pending|done|failed, text, engine, ts}      │
│         │                                                                            │
│         ▼   Transcription dispatcher (retry w/ backoff)                              │
│   1) GET  {mac_url}/health   (timeout ~2.5 s)                                         │
│   2a) healthy → POST {mac_url}/transcribe  ──────────────► Murmur Server (Mac)        │
│   2b) unreachable/error → if fallback on & key set:                                  │
│        POST https://api.openai.com/v1/audio/transcriptions (gpt-4o-transcribe)       │
│   2c) neither reachable → stay 'pending', retry later                                │
│         │                                                                            │
│         ▼                                                                            │
│  Note view: raw text + [Copy] [Share→Claude/Notion] [Clean with Claude] [engine tag] │
└──────────────────────────────────────────────────────────────────────────────────────┘
                                  │  Tailscale (WireGuard mesh, encrypted, no open ports)
                                  ▼
┌─────────────────────── MAC (Murmur Server, FastAPI, launchd) ───────────────────────┐
│  Exposed via `tailscale serve` → https://<machine>.<tailnet>.ts.net                  │
│  Bearer-token auth on every request                                                  │
│                                                                                      │
│  POST /transcribe  →  ffmpeg → 16 kHz mono WAV → mlx-whisper large-v3 (lang=en,      │
│                       initial_prompt = vocab hints) → {text, segments, ...}          │
│                       [reuses Murmur's transcription function; single-flight queue]  │
│  POST /clean       →  Anthropic API (claude-haiku-4-5) → cleaned notes (optional)    │
│  GET  /health      →  {status, model_loaded, version}                                │
└──────────────────────────────────────────────────────────────────────────────────────┘
```
**Key property — capture is decoupled from transcription.** Recording succeeds with no network. The dispatcher resolves each note when an engine is reachable, Mac preferred. So Jay never loses a thought, and the "Mac must be awake right now" fragility only affects *latency of the result*, not whether capture works.
---
## 3. The capture model (important behavioral constraint)
Android forbids silent always-on background mic capture the way the Mac allows. v1 is **intentional capture**, but a session can be long and survive backgrounding/lock:
- Recording starts only from the foreground (tap record, or a launcher/quick-settings tile that brings the app forward).
- While recording, a **foreground service with `microphone` type** runs with a **persistent notification** (mandatory on Android 14+), so recording continues if Jay switches apps or locks the screen.
- Controls live in the notification: Pause/Resume, Stop.
- Incoming phone calls interrupt the mic session; on interruption, the app finalizes the current recording (saves what it has) rather than losing it.
This is the natural shape for a phone: deliberate notes/dictation on the go, not 8-hour ambient capture.
---
## 4. Decisions locked / assumptions
| Decision | Choice |
|---|---|
| Transcription location | **Off-phone.** Mac (primary) + OpenAI (fallback) |
| Primary engine | Murmur on Mac: mlx-whisper **large-v3**, `language=en` |
| Fallback engine | OpenAI **`gpt-4o-transcribe`**, `language=en` |
| Reachability | **Tailscale** (`tailscale serve` for automatic HTTPS on the tailnet) |
| Auth | Static **bearer token** on every request (belt-and-suspenders behind Tailscale) |
| Transcription mode | **Batch** only (record → POST → response); no streaming |
| Resilience | **Local outbox** with auto-retry; capture works fully offline |
| Phone audio format | **AAC in m4a**, mono, 16 kHz (Whisper-native; also accepted by OpenAI directly) |
| Cleanup | Optional `/clean` via Anthropic **`claude-haiku-4-5`** |
| Android min SDK | API 31 (Android 12); full support for API 33 (notifications) & API 34 (FGS type) specifics |
| UI | Jetpack Compose |
| Server framework | FastAPI (Python), launchd-managed |
| Distribution | Personal sideload (no Play Store) |
**Assumptions to confirm with Jay:** exact Pixel model / Android version (affects nothing critical, but pin it before build); whether OpenAI fallback should be **on by default** (recommended on) and whether he wants the optional Claude cleanup in v1 or v1.5.
---
## 5. Component A — Murmur Server (Mac)
A thin HTTP wrapper around Murmur's existing transcription function. Reuse, don't reimplement.
### 5.1 Endpoints / API contract
#### `GET /health`
Fast liveness + readiness check the phone calls before every transcription to decide Mac-vs-fallback.
- **Auth:** bearer token.
- **200 response:**
```json
{ "status": "ok", "model_loaded": true, "model": "large-v3", "version": "1.0.0" }
```
- Must respond in well under the client's ~2.5 s timeout even while a transcription is in progress (health check must not block on the model lock).
#### `POST /transcribe`
- **Auth:** bearer token.
- **Request:** `multipart/form-data`
  - `file` (required): audio (m4a/aac, wav, mp3, ogg/opus accepted)
  - `language` (optional, default `en`)
  - `vocab_hints` (optional, comma-separated; overrides server default)
  - `model` (optional, default server's `large-v3`)
- **Processing:**
  1. Reject if file > 25 MB or duration > 30 min (return 413; client chunks — see §6.4).
  2. `ffmpeg` → 16 kHz mono PCM WAV (normalize all inputs to one format).
  3. Call Murmur's transcription function with `language`, `initial_prompt` built from `vocab_hints`, hallucination guard (drop known Whisper artifacts via `no_speech_prob`/compression-ratio thresholds — reuse Murmur's logic).
  4. Delete the temp audio after transcription (server keeps nothing by default).
- **200 response:**
```json
{
  "text": "So for the Solarman retry logic we should...",
  "segments": [
    { "start": 0.0, "end": 4.2, "text": "So for the Solarman retry logic we should..." }
  ],
  "language": "en",
  "duration_sec": 37.4,
  "model": "large-v3",
  "engine": "mac"
}
```
- **Errors (JSON):** `400` bad request, `401` bad/missing token, `413` too large, `415` unsupported media, `422` transcription failed, `503` model not loaded / busy-timeout.
#### `POST /clean` (optional — v1.5)
- **Auth:** bearer token.
- **Request (JSON):** `{ "text": "<raw transcript>", "style": "notes" }`
- Calls Anthropic API `claude-haiku-4-5` with a fixed prompt: fix transcription errors in context, then produce **Summary / Decisions / Action items / Ideas / Personal notes**.
- **200 response:** `{ "cleaned": "<markdown>", "model": "claude-haiku-4-5" }`
- Anthropic key from Mac env / Keychain — never sent from the phone.
### 5.2 Concurrency & performance
- Load the Whisper model **once at startup**, keep warm.
- **Single-flight queue** (one transcription at a time) guarded by a lock; mlx-whisper holds one model instance — serialize to avoid memory pressure. `/health` bypasses the lock.
- Target: a 1–2 min note returns in a few seconds on the M4 Pro (large-v3 via Metal runs several× real-time).
### 5.3 Networking (Tailscale)
- Install Tailscale on Mac and Pixel (same tailnet).
- Bind FastAPI to **localhost** only (e.g., `127.0.0.1:8080`), then expose over the tailnet with TLS:
  - `tailscale serve --bg https / http://127.0.0.1:8080`
  - Phone uses `https://<machine-name>.<tailnet>.ts.net`.
- This opens **no router ports** and exposes nothing to the public internet. Tailscale ACLs are the network boundary; the bearer token is defense-in-depth.
### 5.4 Process management
- Run as a **launchd** user agent (`com.jay.murmur-server.plist`), auto-start at login, restart on crash.
- Wrap with `caffeinate -i` (or `pmset`/the app sets a power assertion) so the Mac stays awake **while serving** — necessary for it to act as a server. Document that the laptop must be powered/lid-open to serve; otherwise the phone uses fallback.
- Single instance (PID/lockfile).
- Structured logs to `~/Library/Logs/murmur-server.log`; **never log audio bytes, transcript text, or the token.**
### 5.5 Config (`server.toml`)
```toml
[server]
bind = "127.0.0.1:8080"
max_upload_mb = 25
max_duration_min = 30
[stt]
model = "large-v3"
language = "en"
vocab_hints = ["Rivy", "Yassir", "Refendr", "Chidera", "Kemi", "Vaasa", "GKE", "BigQuery", "FIN-FSA"]
[clean]              # optional, v1.5
enabled = false
model = "claude-haiku-4-5"
```
Bearer token and Anthropic key via env / macOS Keychain (never in the file).
---
## 6. Component B — Murmur Mobile (Android)
### 6.1 Screens
1. **Home / Record** — one large record button; below it, the recent-notes list. Recording state shows elapsed time + waveform/level meter. Stop returns to the note view.
2. **Note detail** — raw transcript, status (pending/transcribing/done/failed), engine tag (`Mac` / `OpenAI`), audio playback (if kept), actions: **Copy**, **Share** (Android share sheet → Claude app, Notion, Gmail, etc.), **Clean with Claude** (optional), **Retry** (if failed), **Delete**.
3. **Settings** — Mac URL, bearer token, OpenAI key, **fallback toggle (default on)**, keep-audio toggle (default off), audio quality, vocab hints, optional Claude cleanup toggle, "Test connection" button (hits `/health`).
### 6.2 Capture
- `AudioRecord`/`MediaRecorder` → mono, 16 kHz, AAC in m4a.
- **Foreground service**, `foregroundServiceType="microphone"` (Android 14+), persistent notification with Pause/Resume/Stop.
- Survives app backgrounding and screen lock for the duration of a session.
- On audio-focus loss / incoming call: finalize and save the current recording (never discard).
- Write audio to app-private storage; create an Outbox row immediately on stop.
### 6.3 Transcription dispatcher (the core logic)
For each `pending` note, in a background worker (WorkManager, with network constraint + exponential backoff):
1. If `mac_url` set → `GET {mac_url}/health` with ~2.5 s timeout and the bearer token.
2. **Healthy** → `POST {mac_url}/transcribe` (multipart: file + language + vocab_hints). On 200, store `text`, `segments`, `engine="mac"`, mark `done`.
3. **Unreachable / timeout / 5xx** → if **fallback enabled** and OpenAI key present → `POST https://api.openai.com/v1/audio/transcriptions` with `model=gpt-4o-transcribe`, `language=en`, `prompt=<vocab hints>`, the m4a file. On success, store `text`, `engine="openai"`, mark `done`.
4. **Neither available** (Mac down + no key/no network) → leave `pending`; WorkManager retries when connectivity returns.
5. Surface the engine used on the note so Jay can see when fallback kicked in.
- **Auth failures (401):** mark note `failed` with a clear "check token/key in Settings" message; do not silently retry forever.
> Note: from the phone's perspective the Mac API and OpenAI API are interchangeable ("POST audio → get text"). Keep a single `TranscriptionEngine` interface with two implementations so adding/removing engines is trivial.
### 6.4 Long recordings
- If a recording exceeds the server/OpenAI limits (25 MB / 30 min), **split on silence** client-side into sub-25 MB chunks, transcribe each, concatenate results in order. v1 may instead just **cap a single note at 25 min and warn**; chunking is v1.5 if needed.
### 6.5 Storage & retention
- Room DB: notes (id, created_at, duration, audio_path?, raw_text, cleaned_text?, engine, status).
- **Audio deleted after successful transcription** by default (`keep_audio=false`).
- Notes stored locally on the phone; nothing leaves the device except to the Mac (over Tailscale) or OpenAI (only on fallback, only if enabled).
- Optional `retention_days` prune (default keep).
### 6.6 Permissions
`RECORD_AUDIO`, `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_MICROPHONE` (API 34+), `POST_NOTIFICATIONS` (API 33+), `INTERNET`. Runtime-permission flow with clear rationale; degrade gracefully if notifications are denied (still record, but warn the service may be less reliable).
### 6.7 Secrets
Bearer token, OpenAI key, optional Anthropic key stored in **EncryptedSharedPreferences / Android Keystore**. Never logged. Sent only over HTTPS.
---
## 7. Cross-cutting requirements
### 7.1 Accent fidelity (the whole point)
- **Mac path:** full large-v3, the model Jay already validated on his voice.
- **OpenAI path:** `gpt-4o-transcribe`, same engine family as ChatGPT voice input (strong on his accent).
- **Both paths** pass vocab hints as the prompt/`initial_prompt` for proper nouns (Rivy, Yassir, Refendr, Chidera, Kemi, BigQuery, FIN-FSA, …). Keep the hint list identical across phone and server.
### 7.2 Security & privacy
- Tailscale is the network boundary; no public exposure; bearer token on every request; constant-time token compare server-side.
- TLS via `tailscale serve` (no self-signed cert handling on the phone).
- No audio/transcript/secret ever written to logs on either side.
- Fallback to OpenAI sends audio off-device — make this explicit in Settings (it's why fallback is a visible toggle), default on for reliability.
### 7.3 Error UX
- Clear, human messages: "Mac asleep — used OpenAI", "No connection — saved, will transcribe later", "Auth failed — check Settings".
- Never lose a recording: capture → Outbox is atomic; failures keep the audio until resolved or explicitly deleted.
---
## 8. Milestones (build order for Claude Code)
**M0 — Murmur Server (Mac).** FastAPI with `/health` + `/transcribe` wrapping Murmur's transcriber; ffmpeg normalization; bearer-token auth; single-flight queue; launchd + caffeinate; `tailscale serve`. **Done when:** `curl` from the Pixel over Tailscale returns an accurate transcript of a real voice note.
**M1 — Murmur Mobile core.** Compose UI; foreground-service recording (AAC/m4a, mono 16 kHz); Settings (Mac URL, token, "Test connection"); Outbox + dispatcher hitting the Mac; note view with Copy/Share. **Done when:** record on phone → text back in-app via the Mac, and Share into the Claude app works.
**M2 — Resilience & fallback.** `/health` gating; OpenAI fallback (gpt-4o-transcribe) with engine tag; WorkManager retry/backoff; offline capture that resolves later; auth-error handling. **Done when:** with the Mac asleep, a note still transcribes via OpenAI; with no network, it records and auto-completes once connectivity returns.
**M3 — Cleanup & history (optional).** `/clean` via claude-haiku-4-5 + "Clean with Claude" button; notes history/search; keep-audio & retention settings; long-recording chunking. **Done when:** a raw note can be one-tapped into clean Summary/Action-items markdown.
### v1 acceptance criteria (system done)
- Record a 2-min note on the Pixel → accurate transcript (Jay's accent) returns in-app within seconds when the Mac is awake.
- Mac asleep → same note transcribes automatically via OpenAI, clearly tagged.
- Airplane mode → note records fine, shows `pending`, and completes itself when back online.
- Recording continues through an app-switch and a screen-lock; an incoming call doesn't lose the recording.
- Nothing is exposed to the public internet; all Mac traffic rides Tailscale with a token.
- Audio is gone after transcription (default); transcripts live only on the phone (plus OpenAI only on fallback).
- Share sheet puts clean text into the Claude app for follow-up.
---
## 9. Setup checklist (one-time, document in README)
1. Install Tailscale on Mac + Pixel; sign into the same tailnet.
2. On Mac: generate a random bearer token; store in Keychain/env; set Anthropic key if using `/clean`.
3. Start Murmur Server (launchd); run `tailscale serve --bg https / http://127.0.0.1:8080`.
4. Note the Mac's tailnet URL (`https://<machine>.<tailnet>.ts.net`).
5. In Murmur Mobile Settings: paste Mac URL + token, add OpenAI key, keep fallback ON; tap "Test connection" (expect `/health` ok).
6. Record a test note; confirm transcript + engine tag.
---
## 10. Backlog / explicitly out of scope (v1)
- On-device Whisper on the phone (only worth it if Jay wants Mac-independence and accepts a turbo accent re-test)
- Always-on / ambient capture
- Streaming/live transcription
- Speaker diarization
- Play Store distribution, multi-user, sync of notes back to the Mac/desktop Murmur
- Widget / quick-settings tile / Assistant intent to start a recording faster
- Full-text search across notes; tagging/folders
