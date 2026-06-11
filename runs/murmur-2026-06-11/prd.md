# PRD — "Murmur": Personal Always-On Voice Transcription for macOS
**Owner:** Jay (single user, personal tool — not shipping to anyone else)
**Target machine:** MacBook Pro, Apple M4 Pro, macOS (current version)
**Builder:** Claude Code, end-to-end
**Status:** v1 spec — decisions locked, ready to build
---
## 1. Problem & Goal
Jay speaks English with a Nigerian accent. Commercial dictation/transcription tools he has bought mis-transcribe him badly. OpenAI's Whisper lineage (the engine behind ChatGPT voice input) handles his accent well.
**Goal:** A background macOS app that listens to the **microphone only**, all workday, detects speech, transcribes it locally with Whisper large-v3, and appends it to a daily Markdown transcript that Jay can paste into Claude (or have auto-summarized). It should be invisible until needed, pausable instantly, and cost ~€0/month to run.
### Non-goals (v1)
- **No system-audio / meeting capture** (other call participants are out of scope; flagged as v1.5)
- No real-time on-screen transcription display
- No speaker diarization
- No multi-user features, no cloud sync, no mobile app
- No fine-tuning of Whisper
---
## 2. Architecture Overview
```
Mic (CoreAudio via sounddevice, 16 kHz mono)
   │  continuous ring buffer
   ▼
Silero VAD (local, streaming)
   │  emits speech segments, split on ≥1.5 s silence,
   │  max segment length 120 s (force-split at quietest point)
   ▼
Segment queue (WAV files on disk, ./spool/)
   ▼
Transcription worker (single background process)
   ├─ PRIMARY: local Whisper large-v3 via mlx-whisper (Apple Silicon / Metal)
   └─ FALLBACK: OpenAI API gpt-4o-transcribe (config flag, off by default)
   ▼
Daily transcript file  ~/Transcripts/2026-06-11.md
   ▼  (v2, scheduled nightly)
Claude API (claude-haiku-4-5) → ~/Transcripts/notes/2026-06-11-notes.md
```
**Process model:** one Python application run as a macOS menubar app (rumps), started at login via a launchd agent. Capture, VAD, and transcription run as threads/subprocesses inside it. If transcription falls behind (e.g., heavy meeting day), the spool queue absorbs it — transcription does not need to be real-time, only same-day.
---
## 3. Tech Stack (decided)
| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Best Whisper/VAD ecosystem; fastest to build |
| Audio capture | `sounddevice` (PortAudio) | Simple, reliable mic streaming on macOS |
| VAD | `silero-vad` (torch, CPU) | Industry standard, tiny, accurate |
| Local STT | `mlx-whisper` with `whisper-large-v3` (or `large-v3-turbo` if accuracy holds — see §8 bake-off) | Native Apple Silicon/Metal; faster than real-time on M4 Pro; free |
| API STT fallback | OpenAI `gpt-4o-transcribe` (and `gpt-4o-mini-transcribe` as cheap option), `language="en"` | Same engine family as ChatGPT voice input; ~$0.006/min and $0.003/min |
| Menubar UI | `rumps` | Minimal native menubar app in Python |
| Global hotkey | `pynput` global listener (requires Accessibility permission) | Pause/resume from anywhere |
| Autostart | launchd user agent (`~/Library/LaunchAgents/com.jay.murmur.plist`) | Survives reboot |
| Notes generation (v2) | Anthropic API, model `claude-haiku-4-5` | Cheap, good at cleanup/summarization |
| Config | Single `config.toml` in app dir | One user, keep it simple |
| Packaging | Plain venv + launchd is fine; `py2app` optional polish | Personal tool |
**macOS permissions to handle:** Microphone (mandatory), Accessibility (for global hotkey), and Notifications. The app must detect missing permissions and show clear instructions rather than failing silently.
---
## 4. Functional Requirements
### FR1 — Capture & VAD
1. Continuously capture default input device at 16 kHz mono.
2. Silero VAD gates audio; only speech is written to disk. Silence is discarded immediately (never stored).
3. Segments close after **1.5 s of trailing silence** or **120 s max length**; include 0.3 s pre-roll and post-roll padding so words aren't clipped.
4. Handle device changes (AirPods connect/disconnect, headset plug) by reopening the stream on the new default device without crashing.
5. On wake from sleep, capture resumes automatically.
### FR2 — Transcription
1. Worker watches the spool directory; transcribes segments in order with `mlx-whisper`, `language="en"`, with Whisper's `initial_prompt` set from a user-editable **vocabulary hints** field in config (names/terms like: Rivy, Yassir, Refendr, Chidera, Kemi, Vaasa, GKE, Pub/Sub, BigQuery, FIN-FSA — improves proper-noun accuracy).
2. Hallucination guard: drop segments whose output matches known Whisper hallucination patterns (e.g., repeated phrases, "thank you for watching", empty audio artifacts) using `no_speech_prob`/compression-ratio thresholds.
3. On success: append to daily file, then **delete the WAV** (configurable `keep_audio = false` by default).
4. On repeated local failure: if `api_fallback = true` in config and `OPENAI_API_KEY` is set, retry that segment via API; otherwise leave the WAV in spool and surface an error count in the menubar.
5. Throughput requirement: must sustain ≥2× real-time on M4 Pro so a full day always finishes same-day.
### FR3 — Output format
Daily file `~/Transcripts/YYYY-MM-DD.md`:
```markdown
# Transcript — 2026-06-11
**[09:14]** So for the Solarman integration the retry logic needs to...
**[09:31]** Note to self: email the Kemi landlord about the August first start...
```
One block per segment, timestamped with segment start time (local time). Append-only, crash-safe (write-then-flush). A `murmur cat today` CLI command prints today's file; `murmur copy today` copies it to clipboard for pasting into Claude.
### FR4 — Control (menubar + hotkey)
1. Menubar icon shows state: ● recording / ⏸ paused / ⚠ error (with queue depth in the dropdown).
2. Dropdown: Pause/Resume, "Pause for 30 min / 1 h / rest of day", Open today's transcript, Open transcripts folder, Generate notes now (v2), Quit.
3. Global hotkey (default `⌃⌥⌘M`) toggles pause/resume with a macOS notification confirming state — this is the "stop listening" requirement.
4. State persists across restarts (if paused "for rest of day", relaunching doesn't resume).
### FR5 — Privacy & retention
1. Everything stays on disk locally; nothing leaves the machine unless API fallback (off by default) or v2 notes generation is explicitly enabled.
2. Raw audio deleted after successful transcription by default.
3. `retention_days` config (default 365) prunes old transcripts; `0` = keep forever.
4. Spool and transcripts live under `~/Transcripts/` so they're covered by normal disk encryption/backups.
### FR6 — Notes generation (v2)
1. Nightly at a configured time (default 18:30), send the day's transcript to `claude-haiku-4-5` with a fixed prompt: clean up transcription errors in context, then produce sections — Summary, Decisions, Action items, Ideas, Personal notes.
2. Output to `~/Transcripts/notes/YYYY-MM-DD-notes.md`. Never modify the raw transcript.
3. Chunk if transcript exceeds context comfort (~150k characters per call, then merge).
4. Manual trigger from menubar.
---
## 5. Configuration (`config.toml` defaults)
```toml
[capture]
device = "default"
vad_threshold = 0.5
silence_split_s = 1.5
max_segment_s = 120
[stt]
engine = "local"            # "local" | "api"
local_model = "large-v3"    # or "large-v3-turbo" pending bake-off
api_model = "gpt-4o-transcribe"
api_fallback = false
language = "en"
vocab_hints = ["Rivy", "Yassir", "Refendr", "Chidera", "Kemi", "BigQuery", "FIN-FSA"]
[storage]
transcripts_dir = "~/Transcripts"
keep_audio = false
retention_days = 365
[hotkey]
toggle = "ctrl+alt+cmd+m"
[notes]   # v2
enabled = false
time = "18:30"
model = "claude-haiku-4-5"
```
API keys via environment / macOS Keychain — never in config or code.
---
## 6. Error Handling & Edge Cases
- Mic permission revoked → menubar ⚠ + notification with fix instructions.
- Model files missing → first-run downloads large-v3 weights with progress in menubar; verify checksum.
- Disk low (<2 GB free) → pause capture, notify.
- Crash recovery: on launch, transcribe any leftover WAVs in spool before resuming capture.
- Two instances: refuse to start if a lockfile/PID is already active.
- Long silence days: empty daily file is fine; don't create it until first segment.
---
## 7. Milestones (build order for Claude Code)
**M1 — Core pipeline as CLI (the real product).** `murmur run` records mic → VAD → mlx-whisper → daily Markdown. Prove accuracy and throughput. Includes config, vocab hints, hallucination guard, crash-safe append.
**M2 — Daemonize.** Menubar app, global hotkey, pause timers, launchd autostart, permissions flow, error surfacing.
**M3 — Notes (v2).** Nightly Claude Haiku summarization + manual trigger.
**M1.5 (parallel, tiny) — Bake-off script.** See §8; informs the `local_model` choice.
### Acceptance criteria (v1 done)
- Runs 8 h unattended; CPU mostly idle when nobody is speaking; fan stays quiet.
- A normal speaking day fully transcribed by end of day; queue drains to zero.
- Hotkey pause takes effect <1 s; nothing recorded while paused.
- Pull AirPods mid-day → recovers without restart.
- Raw audio never persists after transcription (default config).
- Jay reads a day's transcript and judges accuracy on his accent ≥ his ChatGPT voice-input experience.
---
## 8. Bake-off (do this first, ~30 min)
A standalone script `bakeoff.py`: record 10 minutes of Jay's normal work speech once, then transcribe the same audio through:
1. `mlx-whisper` large-v3 (local)
2. `mlx-whisper` large-v3-turbo (local, ~4× faster)
3. OpenAI `gpt-4o-mini-transcribe` (API, ~$0.03)
4. OpenAI `gpt-4o-transcribe` (API, ~$0.06)
Output all four transcripts side-by-side in one Markdown file for Jay to eyeball. Decision rule: pick the cheapest/most-local option whose accuracy Jay rates acceptable; that sets `local_model` / `engine` in config.
---
## 9. v1.5+ Backlog (explicitly out of scope now)
- System-audio capture for meetings (ScreenCaptureKit) with separate "Meeting mode" toggle
- Speaker diarization
- Live "what did I just say" peek window
- Search across transcripts (`murmur grep`)
- Direct Claude Desktop / MCP integration instead of copy-paste
