# Implementation Notes

**Run ID:** murmur-mobile-2026-06-14

Created by the implementer in Stage 4; appended by the implementer during fix-list work and by the doc-writer in Stage 6. Append-only — never rewrite earlier entries.

## Build Log

| Task | Note |
|------|------|
| T-1 | `load_server_config` mirrors `config.py` pattern exactly. Known top-level sections: `server`, `stt`, `clean`. `[clean]` dict is stored in `ServerConfig.clean_raw` (a `dict[str, object]`) and never acted on (OQ-3/OQ-5). `get_server_token` mirrors `mac/keychain.py::get_openai_api_key` verbatim — env first, then `security find-generic-password -s murmur-server-token -w`. `verify_token` wraps `hmac.compare_digest` and handles None safely. |
| T-2 | `decode_to_pcm` takes an injected `run` callable (same pattern as `keychain.py`). Calls `ffmpeg -i <path> -ac 1 -ar 16000 -f f32le -` and reads raw float32 bytes from stdout. Wraps `FileNotFoundError` (binary missing), non-zero returncode, and `TimeoutExpired` in `AudioDecodeError`. |
| T-3 | App factory `create_app(transcriber, token, config, *, model_loaded_flag)`. `model_loaded_flag` is a `threading.Event` — set when model is ready, checked without acquiring any lock. `/health` never references `asyncio.Lock`. Token stored in app state for comparison via `verify_token`. |
| T-4 | Happy-path wiring: ffmpeg decode injected via `decode_fn` parameter on the router. `vocab_hints` form field may be repeated (list) or comma-separated string; both parsed. `build_initial_prompt` called with merged hints. `clean_result` called on result; `None` → 422 `no_speech_detected`. `duration_sec` = `len(pcm) / 16000`. |
| T-5 | Validation order matches §A exactly: auth → single-flight check → model-loaded check → file presence → file size → MIME/ext check → ffmpeg decode → transcribe → clean. Temp file written to `tempfile.mkstemp` and deleted in `finally`. Size check streams the upload in chunks rather than reading all bytes into memory first. |
| T-6 | `asyncio.Lock` stored in app state. `POST /transcribe` checks `lock.locked()` before `async with lock:`. Inside the lock, transcribe runs via `asyncio.get_event_loop().run_in_executor(None, ...)` to keep the event loop free for concurrent `/health`. Single-flight test uses `httpx.AsyncClient` with parallel tasks. |
| T-7 | A `logging.Filter` subclass `_TokenRedactFilter` is attached to the root logger in `create_app`; it scans `record.getMessage()` for the token string and replaces it with `[REDACTED]`. Token is never passed to any log call directly. |
| T-8 | `render_server_plist` produces `KeepAlive=true` (differs from `mac/launchd.py` which uses `false`). `ProgramArguments` = `[caffeinate, -i, <python>, -m, murmur.server]`. `install_server_agent` / `uninstall_server_agent` follow the same shape as `mac/launchd.py`. Single-instance belt reuses `SingleInstanceLock` from `murmur.lock` on `/tmp/murmur-server.pid`. |
| T-9 | `tests/integration/test_server_live.py` contains a single `hardware`-marked test stub that skips when ffmpeg/tailscale are absent. Server README added as `src/murmur/server/README.md`. |

## Deviations from Plan

| # | Plan said | Done instead | Why | Flagged |
|---|-----------|--------------|-----|---------|
| 1 | `anyio.to_thread.run_sync` for offloading blocking transcribe | Used `asyncio.get_event_loop().run_in_executor(None, ...)` instead | `anyio` is not in the dependency tree; `run_in_executor` achieves the identical result (offloads to the default ThreadPoolExecutor). Adding `anyio` as a dep would be unnecessary. | Yes — minor; semantically equivalent. |
| 2 | `httpx>=0.27` added to dev group | Added `httpx>=0.27` and `pytest-asyncio>=0.23` | `pytest-asyncio` is required to run async test functions with `@pytest.mark.asyncio`; not listed in plan but essential for the single-flight tests. | Yes — additive only. |

## Fix-List Work

(none yet)

## Documentation

(doc-writer appends here)
