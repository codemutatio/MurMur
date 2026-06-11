# Murmur

Personal always-on voice transcription for macOS. Captures microphone audio, runs
Whisper large-v3 locally on Apple Silicon, and appends transcribed speech to a daily
Markdown file.

## Requirements

- macOS (Apple Silicon, M-series chip required for mlx-whisper)
- Python 3.11+
- uv 0.11+

## Install

```sh
uv sync
uv run murmur --help
```

## Usage

```sh
# Start the daemon (menubar mode)
uv run murmur run

# Start headless (terminal)
uv run murmur run --headless

# Print today's transcript
uv run murmur cat today

# Copy today's transcript to clipboard
uv run murmur copy today

# Benchmark transcription RTF on a WAV file
uv run murmur bench recording.wav

# Install/uninstall launchd agent (login autostart)
uv run murmur install-agent
uv run murmur uninstall-agent
```

## Configuration

Copy `config.example.toml` to `~/Library/Application Support/Murmur/config.toml`
and edit as needed. All defaults are safe to start with.

## API keys (for OpenAI fallback)

API keys are never stored in config files. Store in Keychain:

```sh
security add-generic-password -s murmur-openai -a "$USER" -w
```

Or set `OPENAI_API_KEY` in the environment. Under launchd the environment variable
is not inherited from the shell, so Keychain is the recommended path for the daemon.

## Manual verification procedures

See `docs/manual-verification.md` for scripted procedures for AC-20 through AC-28.

## Bake-off

```sh
uv run python scripts/bakeoff.py --input recording.wav --output bakeoff-result.md
```

Transcribes the same audio through mlx large-v3, mlx large-v3-turbo, gpt-4o-mini-transcribe,
and gpt-4o-transcribe for side-by-side accuracy comparison.
