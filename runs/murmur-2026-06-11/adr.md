# ADR: Single-process, thread-per-stage concurrency with injectable hardware seams (spool as the durable queue)

**Run ID:** murmur-2026-06-11 · **Status:** proposed → accepted at checkpoint · **Date:** 2026-06-11

## Context

The PRD's process model says "capture, VAD, and transcription run as threads/subprocesses inside it" — deliberately ambiguous. Five concurrent concerns must coexist: a real-time audio callback (R-1: no gaps > 100 ms), CPU-bound VAD, GPU/ANE-bound Whisper transcription (R-10: ≥ 2× real-time), a global hotkey listener, and a menubar UI that AppKit requires on the main thread. Meanwhile the validation gate requires every [AUTO] AC to run on this machine with no microphone, no GUI, and no multi-GB model download — so the concurrency design and the testability design are the same decision. The GIL is the central worry: if Whisper decode holds the GIL, capture could gap.

## Decision

One Python process. Fixed thread roster with exclusive ownership: PortAudio callback thread (only `queue.put_nowait`), capture-engine thread (sole owner of the input stream, the Silero model, the segmenter, and spool writes), transcription worker thread (sole owner of spool consumption, transcript writes, WAV deletion), pynput listener thread (only calls thread-safe `Controller`), main thread (sole owner of all rumps/AppKit objects, pull-only via a 1 s timer reading `StatusBoard`). Cross-thread state is confined to three lock-guarded objects (`Controller`, `StateStore`, `StatusBoard`) plus two `queue.Queue`s. Every hardware/OS boundary is a protocol (`AudioSource`, `VoiceDetector`, `Transcriber`, `Notifier`, clock, disk-free, permission probes) with a production adapter and a test fake. The spool directory is the durable work queue: WAV filenames encode segment start time, so crash recovery (R-16/E-6) and the in-process queue are the same mechanism.

## Options Considered

| Option | Pros | Cons |
|--------|------|------|
| A (chosen) Threads in one process, protocol seams, spool-as-queue | Simplest ops (one PID, one lockfile, one launchd entry); spool gives durability for free; mlx/torch release the GIL in native kernels; callback isolated to a C-thread + 3.2 s buffered queue; fakes make the whole pipeline testable headless; designed escape hatch if GIL bites | GIL risk is probabilistic, only fully provable on hardware (MV-21); shared-memory bugs possible (mitigated by strict ownership rules); one crash takes down everything |
| B Worker as a subprocess (multiprocessing/spawn) | Hard GIL isolation for Whisper; worker crash doesn't kill capture | IPC for status/error counts/pause; two PIDs complicate the lockfile, Quit, launchd, and AC-6; model load per spawn; much harder to test; solves a problem we haven't measured yet |
| C asyncio core with thread executors | Single-threaded control logic | sounddevice callbacks, pynput, rumps, torch are all callback/thread-native — asyncio adds an event loop *plus* all the same threads; worst of both |

## Consequences

Easier: the entire core pipeline (segmenter → spool → worker → transcript) runs in-process in tests with fakes, which is what makes 26 of 30 ACs automatable and the 80% coverage bar reachable. Pause latency (AC-23 ≤ 1 s) falls out of the 0.5 s engine tick. Harder: we commit to the ownership discipline (any rumps call off the main thread, or VAD use off the capture thread, is a defect — reviewers should police this). We accept at-least-once transcription semantics (append-then-delete; crash between them duplicates one block, never loses one). Revisit trigger: if MV-21 shows capture gaps or queue-drop counters during heavy transcription, promote the worker to a subprocess — the spool boundary was chosen so that change touches only worker startup and status plumbing, not the pipeline contracts.

---

# ADR: Transcript durability via whole-file rewrite + atomic replace (not append+fsync)

**Run ID:** murmur-2026-06-11 · **Status:** proposed → accepted at checkpoint · **Date:** 2026-06-11

## Context

R-9 requires crash-safe appends: "written to a temporary location and flushed before being renamed/appended to the target file", and the Constraints section demands the daily Markdown file is "always in a consistent, readable state". A literal POSIX append cannot be made atomic via rename — those two ideas conflict, so an interpretation is forced. E-12 additionally requires recreating the file if it is deleted mid-day. The file is small by construction: a heavy 8-hour speaking day is a few hundred segments ≈ tens of KB, hard ceiling well under 1 MB.

## Decision

`TranscriptWriter.append` performs read-modify-replace: read current file content (absent → start fresh with the `# Transcript — YYYY-MM-DD` header), concatenate the new `**[HH:MM]** text` block, write the whole result to a temp file in the same directory, `flush` + `os.fsync`, then `os.replace` onto the target. One writer thread exists by ADR-1, so there is no concurrent-writer hazard.

## Options Considered

| Option | Pros | Cons |
|--------|------|------|
| A (chosen) Rewrite whole file + atomic `os.replace` | Reader (Jay, `murmur cat`, backup tools) can never observe a torn or partial file — meets the consistency constraint literally; satisfies R-9's temp+flush+rename wording; E-12 recreation is the same code path; trivially testable | O(file size) per append — irrelevant at < 1 MB; loses the theoretical "pure append" property |
| B `open(…, "a")` + flush + fsync per line | O(line) work; idiomatic | Crash mid-`write()` can leave a torn half-line — violates "always consistent"; doesn't match R-9's stated temp/rename mechanism; E-12 needs a separate path |
| C Write-ahead journal (per-segment files merged into daily file) | Exactly-once even across append/delete crash window | A second spool-like store, compaction logic, more states to test — disproportionate for a personal tool whose failure cost is one duplicated line |

## Consequences

Easier: AC-4/AC-5/E-12 tests are direct; corruption is structurally impossible rather than improbable; the writer stays ~40 lines. Harder: an O(n²) total-write pattern over a day — accepted because n is bounded by human speech (revisit trigger: if transcripts ever exceed ~5 MB/day, e.g., a future system-audio v1.5 feature, switch to append+fsync with a startup torn-line repair pass; the `TranscriptWriter` interface hides the change). We also accept that the rename briefly relies on same-directory temp placement — the temp file is always created in `transcripts_dir` to keep `os.replace` atomic on the same volume.
