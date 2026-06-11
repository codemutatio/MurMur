# Manual Verification Procedures

**Run ID:** murmur-2026-06-11

These procedures cover the [MANUAL] acceptance criteria (AC-20 through AC-28). Each procedure has an unambiguous pass condition. Complete them in order on the target machine (MacBook Pro M4 Pro).

---

## One-time setup (before running any procedure)

### Grant permissions

1. Grant Microphone permission to the terminal/Python binary:
   - Open **System Settings → Privacy & Security → Microphone**
   - Enable access for the terminal app running murmur (e.g., Terminal, iTerm2)
   - If running under launchd, grant access to the `murmur` binary path directly

2. Grant Accessibility permission:
   - Open **System Settings → Privacy & Security → Accessibility**
   - Enable access for the terminal app or the `murmur` binary
   - Required for the `⌃⌥⌘M` global hotkey to work

3. Store the OpenAI API key in Keychain (for API fallback procedures):
   ```sh
   security add-generic-password -s murmur-openai -a "$USER" -w
   # Paste your sk-... key when prompted
   ```

4. Install murmur:
   ```sh
   cd /path/to/murmur
   uv sync
   uv run murmur --help   # smoke check
   ```

---

## MV-20: Throughput ≥ 2× real-time (AC-20)

**Spec:** Transcription worker sustains ≥ 2× real-time on M4 Pro (R-10).

### Procedure

1. Obtain a 60-minute mono WAV at 16 kHz. Record it directly with sounddevice:
   ```sh
   uv run python3 -c "
   import sounddevice as sd, wave
   print('Recording 60 min — speak naturally...')
   audio = sd.rec(60*60*16000, samplerate=16000, channels=1, dtype='int16')
   sd.wait()
   with wave.open('/tmp/bench-60min.wav', 'wb') as w:
       w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
       w.writeframes(audio.tobytes())
   print('Done: /tmp/bench-60min.wav')
   "
   ```
   Alternatively, loop a representative 10-minute segment six times using `sox` or any audio tool to produce a 60-minute file.

   Note: `bakeoff.py` transcribes audio in memory and does not save the recorded WAV to disk. It is not usable for this step.

2. Run the bench command:
   ```sh
   uv run murmur bench /tmp/bench-60min.wav
   ```
3. Observe the printed output.

### Pass condition

The command prints `RTF: N.NNx` where N.NN ≥ 2.00, and exits with code 0.

---

## MV-21: 8-hour unattended run (AC-21)

**Spec:** Process stays alive all day; spool drains; CPU < 5% during silence.

### Procedure

1. Start murmur at the beginning of a normal workday:
   ```sh
   uv run murmur run
   ```
2. Speak naturally throughout the day.
3. At end of day, check the following:
   - (a) Process is still running: `pgrep -f "murmur run"` returns a PID
   - (b) Spool is empty: `ls ~/Transcripts/spool/` shows no `.wav` files
   - (c) CPU is low during silence: open Activity Monitor, find Murmur, verify CPU < 5% when no one is speaking
   - (d) Fan audit: confirm the fan did not audibly spin up during confirmed silence periods

### Pass condition

All four conditions (a)–(d) are satisfied.

---

## MV-22: AirPods / device change recovery (AC-22)

**Spec:** On default input device change, speech appears in transcript within 30 s (R-22, E-3).

### Procedure

1. Start murmur with recording active (menubar shows ●).
2. Disconnect AirPods or unplug a connected headset so the default input device changes.
3. Wait 10 s.
4. Speak a distinct test phrase (e.g., "AirPods recovery test").
5. Wait up to 30 s.

### Pass condition

The test phrase appears in `~/Transcripts/YYYY-MM-DD.md` within 30 s of speaking, and the app has not crashed or entered a permanent ⚠ error state.

---

## MV-23: Hotkey latency ≤ 1 s (AC-23)

**Spec:** `⌃⌥⌘M` toggles pause/resume and fires a macOS notification within 1 s (R-13).

### Procedure

1. Start murmur with recording active (menubar shows ●).
2. Press `⌃⌥⌘M` (Control + Option + Command + M).
3. Observe:
   - A macOS notification saying "Paused" appears.
   - The menubar icon changes to ⏸.
4. Speak a test phrase and wait 10 s; verify nothing is appended to today's transcript.
5. Press `⌃⌥⌘M` again.
6. Observe:
   - A notification saying "Recording" appears.
   - The menubar icon changes to ●.

### Pass condition

Both notifications appear within 1 s of keypress. After step 4, no speech is appended. After step 5, speech resumes being transcribed.

---

## MV-24: Accent accuracy gate (AC-24)

**Spec:** Jay judges Murmur accuracy ≥ ChatGPT voice input for same vocabulary (go/no-go).

### Procedure

1. Use the transcript generated during MV-21 (8-hour run) as the primary sample.
2. Compare a representative 15-minute segment of the Murmur transcript against the same audio transcribed via ChatGPT voice input.
3. Jay reads both transcripts and rates them on: missed words, mis-transcriptions, proper nouns (Rivy, BigQuery, LLM, etc.).

### Pass condition

Jay judges the Murmur transcript to be of equal or better accuracy than ChatGPT voice input for the same audio. This is a subjective go/no-go gate.

**If this fails:** Review bake-off results from MV-26. If `large-v3-turbo` scored better in the bake-off, change `local_model = "large-v3-turbo"` in config.toml and re-run MV-21/MV-24.

---

## MV-25: Sleep/wake recovery (AC-25)

**Spec:** After wake from sleep, speech appears in transcript within 60 s (R-21, E-5).

### Procedure

1. Start murmur with recording active.
2. Close the MacBook lid and wait at least 60 s.
3. Open the lid.
4. Speak a distinct test phrase (e.g., "Wake recovery test").
5. Wait up to 60 s.

### Pass condition

The test phrase appears in today's transcript within 60 s of speaking, with no crash or manual restart required.

---

## MV-26: Bake-off output (AC-26)

**Spec:** `bakeoff.py` completes and produces one MD file with 4 labeled sections (R-27, E-23).

### Procedure

1. Ensure `OPENAI_API_KEY` is set (env or Keychain, see one-time setup).
2. Run the bake-off against a 10-minute recording of Jay's normal work speech:
   ```sh
   # Record fresh audio (will prompt before recording):
   uv run python scripts/bakeoff.py --duration 600 --output bakeoff-$(date +%Y-%m-%d).md

   # Or use a pre-existing recording:
   uv run python scripts/bakeoff.py --input recording.wav --output bakeoff-$(date +%Y-%m-%d).md
   ```
3. Open the output `.md` file.

### Pass condition

The script completes without error and the output file contains exactly four sections labeled:
- `## mlx large-v3`
- `## mlx large-v3-turbo`
- `## gpt-4o-mini-transcribe`
- `## gpt-4o-transcribe`

Each section contains a transcript or "SKIPPED: …" if the API key is unavailable.

**Decision rule:** If `large-v3-turbo` produces equal or better accuracy than `large-v3` in Jay's judgment, update `config.toml`:
```toml
[stt]
local_model = "large-v3-turbo"
```

---

## MV-27: Missing microphone permission (AC-27)

**Spec:** Murmur shows ⚠ and actionable instructions when Microphone permission is revoked (R-18, E-1).

### Procedure

1. Go to **System Settings → Privacy & Security → Microphone**.
2. Revoke Microphone permission for the murmur/terminal binary.
3. Launch `uv run murmur run`.
4. Observe the menubar and any notifications.

### Pass condition

- The menubar shows ⚠ (error state).
- A macOS notification appears with text mentioning Microphone and System Settings.
- The app does not crash or hang.
- When permission is re-granted and waited ~60 s, the app recovers to ● state automatically.

---

## MV-28: Model weight download and checksum (AC-28)

**Spec:** On first run with no cached weights, download completes with checksum verification (R-24, E-18).

### Procedure

1. Delete or move the cached model weights:
   ```sh
   # Find the mlx-whisper cache
   ls ~/.cache/huggingface/hub/ | grep whisper
   # Move it temporarily
   mv ~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-mlx /tmp/whisper-backup
   # Delete the verified marker
   rm -f ~/.cache/huggingface/murmur-model-verified-large-v3.json
   ```
2. Launch `uv run murmur run`.
3. Observe the menubar for download progress.
4. Wait for download to complete.
5. Check the log:
   ```sh
   tail -f ~/Library/Application\ Support/Murmur/murmur.log
   ```

### Pass condition

- The menubar shows download progress (percentage or byte count) during download.
- The log contains a line matching: `Model large-v3 verified OK` or similar checksum success message.
- Transcription begins normally after download.
- If checksum fails (simulate by corrupting a file): the log shows "Checksum mismatch", the corrupt weights are deleted, and the menubar shows ⚠ prompting to retry.

### Restore cached weights

```sh
mv /tmp/whisper-backup ~/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-mlx
```

---

## Bake-off model decision record

| Date | Model | RTF | Jay's accuracy rating | Decision |
|------|-------|-----|----------------------|----------|
| (to be filled after MV-26) | large-v3 | — | — | — |
| (to be filled after MV-26) | large-v3-turbo | — | — | — |

Update this table after running MV-26 and MV-24. Record the final model choice in `config.toml`.
