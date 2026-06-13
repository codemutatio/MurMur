# Murmur

Personal always-on voice transcription for macOS. Captures microphone audio continuously, transcribes speech locally using Whisper large-v3 on Apple Silicon, and appends every utterance — timestamped — to a daily Markdown file at `~/Transcripts/YYYY-MM-DD.md`.

Zero cloud cost by default. No recurring fees. All processing is on-device.

---

## Requirements

- macOS, Apple Silicon (M-series chip required — mlx-whisper uses Metal/ANE)
- Python 3.11+
- uv 0.11+
- ~3 GB free disk space for the model weights (downloaded on first run)

---

## Install

```sh
git clone <repo>
cd murmur
uv sync
uv run murmur --help
```

```
usage: murmur [-h] <command> ...

Personal always-on voice transcription for macOS.

positional arguments:
  <command>
    run            Start the murmur daemon.
    cat            Print transcript to stdout.
    copy           Copy transcript to clipboard.
    bench          Benchmark transcription RTF on a WAV file.
    install-agent  Install launchd user agent.
    uninstall-agent
                   Uninstall launchd user agent.
```

---

## First-run permissions

Murmur requires three one-time permission grants. All three can be completed before the first `murmur run`.

**Microphone** — required for capture:
1. Open **System Settings → Privacy & Security → Microphone**
2. Enable the terminal app (Terminal, iTerm2, or similar) that runs `uv run murmur run`
3. If installed via launchd, grant access to the `murmur` binary path directly

**Accessibility** — required for the global hotkey only:
1. Open **System Settings → Privacy & Security → Accessibility**
2. Enable the terminal app or the `murmur` binary
3. Without this, the app still runs; the hotkey simply has no effect

**Notifications** — one-time prompt on first notification:
- macOS will show a permission dialog the first time a notification fires
- Allow it; notifications are attributed to "Script Editor" (osascript), not "Murmur" — this is expected (see Known Limitations)
- If denied, the app continues silently; no crash, no blocking wait

---

## Quick start

```sh
# Run with menubar icon (mic starts after model loads ~3 GB on first run)
uv run murmur run

# Run headless (no menubar, Ctrl+C to stop)
uv run murmur run --headless

# Print today's transcript
uv run murmur cat today

# Copy today's transcript to the clipboard
uv run murmur copy today
```

---

## Configuration

All configuration lives in a single TOML file. The default location is:

```
~/Library/Application Support/Murmur/config.toml
```

Override with `--config` on any subcommand:

```sh
uv run murmur run --config /path/to/my.toml
```

Copy `config.example.toml` to the default location and edit as needed.

### Full config reference

Every key with its default and meaning, verified against `src/murmur/config.py`:

```toml
[capture]
device          = "default"   # Input device name. "default" = macOS default input.
vad_threshold   = 0.5         # Silero VAD confidence threshold (0..1). Lower = more sensitive.
silence_split_s = 1.5         # Trailing silence duration that closes a speech segment (seconds).
max_segment_s   = 120.0       # Maximum segment duration before a force-split (seconds).

[stt]
local_model  = "large-v3"     # "large-v3" (highest accuracy) or "large-v3-turbo" (~4x faster).
api_model    = "gpt-4o-transcribe"  # OpenAI model for API fallback (requires api_fallback=true).
api_fallback = false          # Retry failed local transcriptions via OpenAI API.
                              # Requires OPENAI_API_KEY in env or Keychain (see below).
language     = "en"           # BCP-47 language code passed to Whisper.
vocab_hints  = [              # Vocabulary hints sent to Whisper as initial_prompt.
    "Rivy",                   # Add domain-specific terms here to improve accuracy.
    "Yassir",
    "BigQuery",
    ...
]

[storage]
transcripts_dir = "~/Transcripts"       # Where to write daily Markdown files.
spool_dir       = "~/Transcripts/spool" # Temporary WAV spool (transient; auto-created).
keep_audio      = false                 # true = keep WAVs after transcription (for debugging).
retention_days  = 365                   # Delete transcripts older than N days. 0 = keep forever.

[hotkey]
toggle = "ctrl+alt+cmd+m"   # Global shortcut to toggle pause/resume.
                            # Supported modifiers: ctrl, alt, cmd, shift, super.

[notes]
enabled = false             # v2 only — ignored in v1.
```

**Missing or unparseable config:** the app starts with all defaults and logs a warning naming the expected path. It does not crash.

**Unknown keys:** logged as warnings, ignored.

**$MURMUR_HOME:** set this environment variable to relocate the app support directory (default `~/Library/Application Support/Murmur`). Useful for testing.

---

## API key setup

API keys are never stored in config files. Use the macOS Keychain or an environment variable.

**Keychain (recommended — works under launchd):**

```sh
security add-generic-password -s murmur-openai -a "$USER" -w
# Paste your sk-... key at the prompt
```

The key is stored under service name `murmur-openai`. Murmur looks it up via:
```sh
security find-generic-password -s murmur-openai -w
```

**Environment variable (terminal sessions only — not available to launchd):**

```sh
export OPENAI_API_KEY=sk-...
```

Lookup order: `OPENAI_API_KEY` env var first, then Keychain. If `api_fallback = true` and no key is found, the app logs a warning and treats fallback as disabled for that session; a notice appears in the menubar.

---

## Login autostart (launchd)

```sh
# Install — registers com.jay.murmur as a launchd user agent:
uv run murmur install-agent

# Uninstall:
uv run murmur uninstall-agent
```

The installed plist is written to `~/Library/LaunchAgents/com.jay.murmur.plist` and bootstrapped with `launchctl bootstrap gui/$UID`. The agent starts at login and captures to the configured transcripts directory.

**Note:** `KeepAlive = false`. The Quit menu item stops the agent permanently until the next login. If you want crash-restart, reinstall after manually editing the plist to set `KeepAlive = true`.

**Permissions under launchd:** TCC permissions (Microphone, Accessibility) attach to the binary path, not the terminal. Grant them for the installed `murmur` binary path. Run `which murmur` after `uv sync` to find the path.

---

## CLI reference

All subcommands verified against `src/murmur/cli.py`:

### `murmur run [--headless] [--config PATH]`

Start the daemon. Without `--headless`, opens the rumps menubar on the main thread. With `--headless`, runs until SIGINT/SIGTERM or Ctrl+C.

Exit codes: `0` on clean stop, `1` if another instance is already running (`Murmur already running (PID: N)` on stderr), `2` on usage error.

### `murmur cat today [--config PATH]`

Print the current day's transcript to stdout. Reads `storage.transcripts_dir/YYYY-MM-DD.md`.

Exit codes: `0` if the file exists, `1` if not (message on stderr).

### `murmur copy today [--config PATH]`

Copy the current day's transcript to the macOS clipboard via `pbcopy`.

Exit codes: `0` on success, `1` if today's file does not exist.

### `murmur bench FILE.WAV [--config PATH]`

Benchmark transcription on a 16 kHz mono WAV file using the configured local model. Downloads model weights if not already cached.

Prints:
```
Audio:    N.N s
Wall:     N.N s
RTF:      N.NNx
Segments: N
```
followed by the transcript text.

Exit codes: `0` if RTF ≥ 2.0 (throughput requirement met), `1` if RTF < 2.0 or file not found.

**Requires model weights (~3 GB) and Apple Silicon.** Verify flags with `uv run murmur bench --help` before running on a machine without weights.

### `murmur install-agent`

Render and install the launchd user agent plist to `~/Library/LaunchAgents/com.jay.murmur.plist`, then bootstrap it.

### `murmur uninstall-agent`

Bootout the agent and remove the plist. Does not raise if the agent is not currently loaded.

---

## Bake-off: choosing between large-v3 and large-v3-turbo

The bake-off script records audio (or loads an existing WAV) and runs it through all four engines side by side:

```sh
# Use an existing recording:
uv run python scripts/bakeoff.py --input recording.wav --output bakeoff-$(date +%Y-%m-%d).md

# Record fresh audio (prompts before recording, default 10 min):
uv run python scripts/bakeoff.py --duration 600 --output bakeoff-$(date +%Y-%m-%d).md
```

```
usage: bakeoff.py [-h] [--input FILE.wav] [--duration SECONDS] [--output FILE.md]
```

Output is a single Markdown file with four labeled `## <engine>` sections, each showing wall time and RTF. Missing `OPENAI_API_KEY` → API sections contain "SKIPPED: …"; the script still completes.

**Decision rule:** if `large-v3-turbo` produces equal or better accuracy, set `local_model = "large-v3-turbo"` in config — it is approximately 4× faster. Record the result in `docs/manual-verification.md` (bake-off decision table).

**Requires model weights (~3 GB per model) and Apple Silicon.** Use `--help` to verify flags without triggering a download.

---

## Menubar

The menubar icon reflects the current state:

| Icon | State |
|------|-------|
| `●` | Recording |
| `⏸` | Paused |
| `⚠` | Error (mic denied, no input device, checksum failure, etc.) |

The dropdown header shows queue depth and error count. The dropdown exposes:

- Pause / Resume
- Pause for 30 min
- Pause for 1 hour
- Pause for rest of day (persists across restarts until midnight)
- Open today's transcript (opens in default Markdown viewer)
- Open transcripts folder
- Generate notes now (disabled in v1 — v2 item)
- Quit

The global hotkey `⌃⌥⌘M` (default) toggles pause/resume from any application and fires a notification confirming the new state. Requires Accessibility permission.

---

## Transcript format

Each line in `~/Transcripts/YYYY-MM-DD.md`:

```
**[HH:MM]** <transcribed text>
```

`HH:MM` is the local wall-clock time of the segment's first sample. The file starts with a header line:

```
# Transcript — YYYY-MM-DD
```

The file is created on the first segment of the day and never created on a silent day.

---

## Model weights

Weights are downloaded from Hugging Face on first use via `huggingface_hub.snapshot_download`:

| Key | Hugging Face repo | Approx. size |
|-----|-------------------|--------------|
| `large-v3` | `mlx-community/whisper-large-v3-mlx` | ~3 GB |
| `large-v3-turbo` | `mlx-community/whisper-large-v3-turbo` | ~1.5 GB |

After download, sha256 checksums are verified against HF LFS metadata. A marker file is written to `~/.cache/huggingface/murmur-model-verified-<model>.json` so subsequent launches skip re-hashing (fast startup). See Known Limitations for the caveats on this.

Once the marker is present, subsequent launches resolve the cached snapshot **fully offline** (`local_files_only=True`) — no outbound request is made, so a verified install satisfies the no-network-during-normal-operation privacy constraint. The first-run download is the only sanctioned network call. If the local snapshot is ever missing despite the marker, the next launch falls back to a network re-download and re-verifies.

---

## State and data locations

| Path | Contents |
|------|---------|
| `~/Library/Application Support/Murmur/config.toml` | User config (optional) |
| `~/Library/Application Support/Murmur/state.json` | Persisted pause state |
| `~/Library/Application Support/Murmur/murmur.lock` | Single-instance flock |
| `~/Library/Application Support/Murmur/murmur.log` | Application log |
| `~/Transcripts/YYYY-MM-DD.md` | Daily transcript files |
| `~/Transcripts/spool/*.wav` | Transient audio segments (normally empty) |
| `~/.cache/huggingface/murmur-model-verified-*.json` | Model checksum marker |

Override the `~/Library/Application Support/Murmur` root with `$MURMUR_HOME`.

---

## Troubleshooting

**Mic permission denied — menubar shows ⚠**
- Open **System Settings → Privacy & Security → Microphone**
- Enable access for the terminal or the `murmur` binary
- The app checks every 60 s and recovers automatically once permission is granted

**Accessibility not granted — hotkey has no effect**
- Open **System Settings → Privacy & Security → Accessibility**
- Enable access for the terminal or `murmur`
- The app runs normally without it; the menubar will show a notice

**Model checksum failure on download**
- The corrupt snapshot is deleted automatically; the menubar shows ⚠
- Re-launch to retry the download
- If it recurs, check disk space (`df -h ~`) and network connectivity

**"Murmur already running (PID: N)" on launch**
- Another instance is running; find it with `pgrep -f "murmur run"`
- Stop it via the menubar Quit or `kill <PID>`
- If the PID is stale (process dead but lock not released), the lock is automatically cleared on next launch — flock is released by the OS when the file descriptor closes

**WAV files stranded in `~/Transcripts/spool/`**
- WAVs with non-conforming filenames (not matching `YYYYMMDD-HHMMSS-NNN.wav`) are skipped each launch and never auto-cleaned
- Remove them manually: `rm ~/Transcripts/spool/*.wav` (after confirming the daemon is stopped)
- WAVs with conforming names that were dropped from the in-process queue (F-8 scenario) will be picked up on next launch

**Notifications not appearing**
- Notification permission may not have been granted yet
- On the first notification, macOS prompts; allow it
- Notifications appear attributed to "Script Editor" (osascript) — this is expected behavior (see Known Limitations)

---

## Known limitations

These are logged minors from validation that affect daily operation:

1. **Stranded bad-filename WAVs.** If a WAV file lands in `~/Transcripts/spool/` with a filename that does not match the pattern `YYYYMMDD-HHMMSS-NNN.wav`, the startup scanner skips it with a logged warning and does not process or clean it up. It will be skipped again on every subsequent launch. Manual removal is required: `rm ~/Transcripts/spool/<bad-file>.wav` (stop the daemon first).

2. **Same-run dropped WAV recovery.** When the in-process work queue is full (bounded at 256 entries, ~8+ minutes of segments), the capture engine logs a warning and skips enqueueing the WAV path. The WAV file remains on disk and will be picked up by the startup scanner on the next launch. It is not processed during the current run.

3. **Notification attribution.** All notifications (Paused, Recording, errors) are sent via `osascript display notification`. Until notification permission is granted once in System Settings, they may be silently dropped or attributed to "Script Editor" rather than "Murmur". This is a consequence of using a Python script without a signed .app bundle. Granting notification permission once resolves the display attribution for that session.

4. **Model checksum re-verification trusts marker file.** Once the verified-marker JSON file is written to `~/.cache/huggingface/murmur-model-verified-<model>.json`, subsequent launches skip sha256 re-hashing and trust the marker. If the weights are subsequently corrupted on disk (without a re-download), the corruption will not be detected until mlx-whisper fails to load them. Delete the marker file to force re-verification: `rm ~/.cache/huggingface/murmur-model-verified-<model>.json`.

5. **launchctl bootstrap test is xfail.** The integration test `test_launchctl_load_inert_roundtrip` is marked expected-failure. `launchctl bootstrap gui/$UID` returns error 5 when called outside an Aqua (GUI) session (i.e., from a terminal). Plist syntax is validated by `plutil -lint` in a passing test. The full bootstrap/bootout path is exercised by `murmur install-agent` in the manual verification runbook.

---

## Benchmarking

```sh
# Measure transcription speed on a WAV file.
# Exit 0 if RTF >= 2.0x (throughput requirement), exit 1 otherwise.
uv run murmur bench recording.wav
```

---

## Manual verification procedures

See [`docs/manual-verification.md`](docs/manual-verification.md) for scripted procedures covering:

- MV-20: Throughput ≥ 2× real-time (AC-20)
- MV-21: 8-hour unattended run (AC-21)
- MV-22: AirPods / device change recovery (AC-22)
- MV-23: Hotkey latency ≤ 1 s (AC-23)
- MV-24: Accent accuracy gate (AC-24)
- MV-25: Sleep/wake recovery (AC-25)
- MV-26: Bake-off output (AC-26)
- MV-27: Missing mic permission (AC-27)
- MV-28: Model weight download + checksum (AC-28)

---

## Development

```sh
# Run tests (no hardware, no model required):
uv run pytest

# Lint + format check:
uv run ruff check . && uv run ruff format --check .

# Type check:
uv run mypy src

# Run hardware tests (requires mic, model weights):
uv run pytest -m hardware
```

Coverage bar: ≥ 80% on `src/murmur`. Coverage gate is enforced by `pytest-cov`.
