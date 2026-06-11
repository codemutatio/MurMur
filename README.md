# Murmur

Personal always-on voice transcription for macOS. Captures microphone audio, runs
Whisper large-v3 locally on Apple Silicon, and appends transcribed speech to a daily
Markdown file at `~/Transcripts/YYYY-MM-DD.md`.

Zero cloud cost by default. No recurring fees. Works with a Nigerian accent.

## Requirements

- macOS (Apple Silicon M-series chip required — mlx-whisper uses Apple Neural Engine / Metal)
- Python 3.11+
- uv 0.11+

## Install

```sh
git clone <repo>
cd murmur
uv sync
uv run murmur --help
```

## Quick start

```sh
# Run the daemon (menubar mode — mic starts after model loads)
uv run murmur run

# Run headless (no menubar, Ctrl+C to stop)
uv run murmur run --headless

# Print today's transcript to stdout
uv run murmur cat today

# Copy today's transcript to the clipboard
uv run murmur copy today
```

## Configuration

All configuration lives in a single TOML file:

```sh
# Default location:
~/Library/Application Support/Murmur/config.toml

# Or override with --config:
uv run murmur run --config /path/to/my.toml
```

Copy `config.example.toml` to the default location and edit as needed.
All defaults are safe to start with.

Key options:

```toml
[capture]
silence_split_s = 1.5     # trailing silence to close a segment (s)
max_segment_s   = 120.0   # force-split after this duration (s)
vad_threshold   = 0.5     # Silero VAD threshold (0..1)

[stt]
local_model  = "large-v3"                  # or "large-v3-turbo"
api_fallback = false                        # true = retry via OpenAI on local failure
vocab_hints  = ["Rivy", "BigQuery", "LLM"] # fed as Whisper initial_prompt

[storage]
transcripts_dir = "~/Transcripts"
spool_dir       = "~/Transcripts/spool"
keep_audio      = false   # true = keep WAV files after transcription
retention_days  = 365     # 0 = keep forever

[hotkey]
toggle = "ctrl+alt+cmd+m"  # global pause/resume hotkey
```

## API keys

API keys are **never** stored in config files. Use the macOS Keychain or environment:

```sh
# Keychain (recommended — persists under launchd):
security add-generic-password -s murmur-openai -a "$USER" -w
# Paste your sk-... key at the prompt

# Or set in the shell (not available to launchd agents):
export OPENAI_API_KEY=sk-...
```

## Login autostart (launchd)

```sh
# Install (registers com.jay.murmur launchd user agent):
uv run murmur install-agent

# Uninstall:
uv run murmur uninstall-agent
```

The agent starts at login, restarts on crash, and uses the Keychain for the API key.
Grant Microphone and Accessibility permissions to the murmur binary path if prompted.

## Benchmarking

```sh
# Measure transcription RTF (real-time factor) on a WAV file.
# Prints: Audio s, Wall s, RTF (pass = RTF ≥ 2.0x), segment count, transcript.
uv run murmur bench recording.wav
```

## Bake-off

Run a side-by-side accuracy comparison across four engines:

```sh
# Transcribe the same audio through all 4 engines:
uv run python scripts/bakeoff.py --input recording.wav --output bakeoff-result.md

# Record fresh audio (prompts before recording, default 10 min):
uv run python scripts/bakeoff.py --duration 600 --output bakeoff-$(date +%Y-%m-%d).md
```

Output is a single Markdown file with four labeled `## <engine>` sections.
Use this to decide whether to use `large-v3` or `large-v3-turbo` in config.

**Model decision rule:** if `large-v3-turbo` produces equal or better accuracy
in the bake-off (MV-26), set `local_model = "large-v3-turbo"` in config — it is
approximately 4× faster.

## Architecture notes

- Single process, four threads: PortAudio callback → capture engine → spool (WAV files) → transcription worker → `~/Transcripts/YYYY-MM-DD.md`
- Spool doubles as crash-recovery queue: WAVs from prior runs are processed at startup (R-16)
- Transcript writes are atomic (tmp + fsync + os.replace) — file is always in a consistent state (R-9, ADR-2)
- All hardware/OS boundaries are injectable protocols — the full pipeline is testable without mic/GPU/model weights

## Manual verification

See [`docs/manual-verification.md`](docs/manual-verification.md) for scripted procedures covering
AC-20 (throughput), AC-21 (8-hour run), AC-22 (AirPods recovery), AC-23 (hotkey),
AC-24 (accent accuracy), AC-25 (sleep/wake), AC-26 (bake-off), AC-27 (mic permission),
AC-28 (model download + checksum).

## Development

```sh
# Run tests (default suite, no hardware/model required):
uv run pytest

# Lint + format check:
uv run ruff check . && uv run ruff format --check .

# Type check:
uv run mypy src

# Run hardware tests (requires mic, model weights):
uv run pytest -m hardware
```
