"""Out-of-process transcription: the memory sustainability layer.

The MLX Whisper model lives on Apple Silicon's unified memory via Metal. MLX
keeps a Metal buffer cache and, left unmanaged, grows it to the peak working
set and never returns it to the OS. When transcription runs on a thread inside
the always-on menubar app, that peak (observed: 15 GB idle, 81 GB peak) is
pinned for the whole process lifetime.

``SubprocessTranscriber`` moves the model into a dedicated child process and
tears it down when idle. The guarantee is structural: memory reclamation stops
depending on MLX behaving and depends only on a process exiting. Between
dictation sessions the menubar app falls back to tens of MB.

Defense in depth inside the child (see ``_child_loop``):
  * ``set_cache_limit(0)`` — do not hoard freed Metal buffers.
  * ``clear_cache()`` after every segment — return buffers to the OS.
  * ``set_memory_limit(cap)`` — a hard ceiling so no single (pathological)
    segment can push the machine into a swap storm; it fails that one segment
    instead. With 30 s segments this is a backstop that never fires in normal
    use.

IPC is a ``socketpair`` with length-prefixed pickle frames. The child is a
clean ``python -m murmur.transcribe.isolated`` interpreter (never the AppKit
app binary), so MLX/Metal is never initialised in a forked-from-GUI process.
"""

from __future__ import annotations

import contextlib
import logging
import os
import pickle
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np

from murmur.transcribe.base import TranscriptionError, TranscriptionResult

_log = logging.getLogger(__name__)

# Wire framing: 4-byte unsigned big-endian length prefix + pickled payload.
_LEN = struct.Struct("!I")
_MAX_MSG = 256 * 1024 * 1024  # 256 MB frame cap — audio segments are a few MB.


# ---------------------------------------------------------------------------
# Framed pickle IPC
# ---------------------------------------------------------------------------
def _send(sock: socket.socket, obj: object) -> None:
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    sock.sendall(_LEN.pack(len(data)) + data)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("socket closed mid-message")
        buf.extend(chunk)
    return bytes(buf)


def _recv(sock: socket.socket) -> object:
    (length,) = _LEN.unpack(_recv_exact(sock, _LEN.size))
    if length > _MAX_MSG:
        raise ValueError(f"framed message too large: {length} bytes")
    return pickle.loads(_recv_exact(sock, length))


# ---------------------------------------------------------------------------
# Interpreter + memory-limit helpers (parent side)
# ---------------------------------------------------------------------------
def _worker_python() -> str:
    """Resolve a Python interpreter that can ``import murmur`` and mlx.

    Dev/venv: ``sys.executable`` is already such a python. In the py2app
    bundle, ``sys.executable`` is the app stub, but a ``python`` symlink sits
    next to it pointing at the uv-tool interpreter that has murmur installed.

    Crucially, that symlink must be resolved *out of the bundle* before use.
    Invoked by its in-bundle path (``…/Contents/MacOS/python``), Python derives
    its prefix from the bundle and cannot import the uv-tool venv's
    site-packages (murmur/mlx) — the child dies before the handshake. We follow
    the symlink only until it leaves the ``.app`` (landing on the venv python,
    which carries its own ``pyvenv.cfg``); we do not fully ``realpath`` it,
    which would jump past the venv to the bare framework interpreter.
    """
    exe = sys.executable or ""
    if "python" in os.path.basename(exe).lower():
        cand = exe
    else:
        cand = os.path.join(os.path.dirname(exe), "python")

    seen: set[str] = set()
    while ".app/" in cand and os.path.islink(cand) and cand not in seen:
        seen.add(cand)
        target = os.readlink(cand)
        cand = (
            target
            if os.path.isabs(target)
            else os.path.normpath(os.path.join(os.path.dirname(cand), target))
        )

    if os.path.exists(cand):
        return cand
    for name in ("python3.11", "python3", "python"):
        p = os.path.join(sys.base_prefix, "bin", name)
        if os.path.exists(p):
            return p
    return exe  # last resort; spawn will surface a clear error if unusable


def _child_env() -> dict[str, str]:
    """Environment for the worker interpreter, with bundle vars stripped.

    A py2app bundle injects ``PYTHONHOME``/``PYTHONPATH`` (and ``DYLD_*``
    framework paths) pointing inside the ``.app``. The worker is a *different*
    interpreter; inheriting ``PYTHONHOME`` makes it look for its stdlib in the
    bundle and abort at startup with ``No module named 'encodings'`` (the child
    dies before the handshake → "socket closed mid-message"). Strip these so the
    worker resolves its own stdlib and site-packages.
    """
    env = dict(os.environ)
    for var in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONEXECUTABLE",
        "__PYVENV_LAUNCHER__",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
    ):
        env.pop(var, None)
    return env


def default_memory_limit_bytes(fraction: float = 0.7) -> int | None:
    """A backstop cap: ``fraction`` of physical RAM.

    Keeps the MLX allocator inside physical memory so a runaway can never swap
    the machine to death, while sitting far above any legitimate 30 s working
    set. Returns None if physical memory cannot be determined.
    """
    try:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    if total <= 0:
        return None
    return int(total * fraction)


class SubprocessTranscriber:
    """Transcriber that runs MLX Whisper in a recyclable child process.

    Implements the :class:`~murmur.transcribe.base.Transcriber` protocol.
    ``transcribe`` is called serially by the single worker thread; a background
    reaper terminates the child after ``idle_timeout_s`` of inactivity so the
    OS reclaims all GPU memory. The next call transparently respawns it.
    """

    def __init__(
        self,
        model_ref: str,
        *,
        idle_timeout_s: float = 120.0,
        cache_limit_bytes: int = 0,
        memory_limit_bytes: int | None = None,
        startup_timeout_s: float = 180.0,
        request_timeout_s: float = 600.0,
        command: list[str] | None = None,
    ) -> None:
        self._model_ref = model_ref
        self._idle_timeout_s = idle_timeout_s
        self._cache_limit_bytes = cache_limit_bytes
        self._memory_limit_bytes = memory_limit_bytes
        self._startup_timeout_s = startup_timeout_s
        self._request_timeout_s = request_timeout_s
        # Command prefix; the socket fd is appended per spawn. Overridable for tests.
        self._command = command or [_worker_python(), "-m", "murmur.transcribe.isolated"]

        self._lock = threading.RLock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self._last_activity = time.monotonic()
        self._closed = False

        self._reaper = threading.Thread(
            target=self._reap_loop, name="transcribe-reaper", daemon=True
        )
        self._reaper.start()

    # -- Transcriber protocol ------------------------------------------------
    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str,
        initial_prompt: str | None,
    ) -> TranscriptionResult:
        with self._lock:
            if self._closed:
                raise TranscriptionError("transcriber is closed")
            self._ensure_child_locked()
            sock = self._sock
            assert sock is not None  # _ensure_child_locked sets it or raises
            try:
                _send(
                    sock,
                    (
                        "transcribe",
                        np.asarray(audio, dtype=np.float32),
                        language,
                        initial_prompt,
                    ),
                )
                sock.settimeout(self._request_timeout_s)
                reply = _recv(sock)
            except (OSError, EOFError, ValueError, pickle.PickleError) as exc:
                # Broken pipe / timeout / corrupt frame → the child is unusable.
                # Kill it so the next call respawns a fresh one.
                self._kill_locked()
                raise TranscriptionError(f"transcription subprocess failed: {exc}") from exc
            finally:
                with contextlib.suppress(OSError):
                    if self._sock is not None:
                        self._sock.settimeout(None)

            self._last_activity = time.monotonic()

            if isinstance(reply, tuple) and len(reply) == 2 and reply[0] == "ok":
                result = reply[1]
                if isinstance(result, TranscriptionResult):
                    return result
                raise TranscriptionError(f"malformed subprocess result: {result!r}")
            # ("error", msg): the child is healthy but failed this segment.
            # Surface as a retryable error; the worker's retry/fallback handles it.
            msg = reply[1] if isinstance(reply, tuple) and len(reply) == 2 else reply
            raise TranscriptionError(str(msg))

    @property
    def pid(self) -> int | None:
        """The worker's pid while it is running (None when released or dead)."""
        proc = self._proc  # no lock: transcribe() holds it for whole requests
        return proc.pid if proc is not None and proc.poll() is None else None

    # -- Lifecycle -----------------------------------------------------------
    def close(self) -> None:
        """Terminate the child and stop the reaper. Idempotent."""
        with self._lock:
            self._closed = True
            self._kill_locked()

    def _ensure_child_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None and self._sock is not None:
            return
        self._kill_locked()  # clear any dead remnants

        parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            proc: subprocess.Popen[bytes] = subprocess.Popen(
                [*self._command, str(child_sock.fileno())],
                pass_fds=(child_sock.fileno(),),
                stdin=subprocess.DEVNULL,
                env=_child_env(),
            )
        except OSError as exc:
            parent_sock.close()
            child_sock.close()
            raise TranscriptionError(f"could not spawn transcription subprocess: {exc}") from exc
        finally:
            child_sock.close()  # parent keeps only its end

        parent_sock.settimeout(self._startup_timeout_s)
        try:
            _send(
                parent_sock,
                {
                    "model_ref": self._model_ref,
                    "cache_limit": self._cache_limit_bytes,
                    "memory_limit": self._memory_limit_bytes,
                },
            )
            reply = _recv(parent_sock)
        except (OSError, EOFError, ValueError, pickle.PickleError) as exc:
            parent_sock.close()
            self._terminate_proc(proc)
            raise TranscriptionError(
                f"transcription subprocess failed to start: {exc}"
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                parent_sock.settimeout(None)

        if not (isinstance(reply, tuple) and len(reply) == 2 and reply[0] == "ready"):
            detail = reply[1] if isinstance(reply, tuple) and len(reply) == 2 else reply
            parent_sock.close()
            self._terminate_proc(proc)
            raise TranscriptionError(f"transcription subprocess startup error: {detail}")

        self._proc = proc
        self._sock = parent_sock
        self._last_activity = time.monotonic()
        _log.info("Transcription subprocess started (pid=%s)", proc.pid)

    def _kill_locked(self) -> None:
        sock = self._sock
        self._sock = None
        proc, self._proc = self._proc, None
        if sock is not None:
            # Ask the child to exit on its own first so it runs atexit cleanup
            # (mlx/torch semaphores) instead of being SIGTERM'd mid-flight.
            with contextlib.suppress(Exception):
                _send(sock, ("shutdown",))
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
            with contextlib.suppress(Exception):
                sock.close()
        if proc is not None:
            self._terminate_proc(proc)  # no-op if the child already exited

    @staticmethod
    def _terminate_proc(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is not None:
            return
        with contextlib.suppress(Exception):
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)

    def _reap_loop(self) -> None:
        interval = max(1.0, min(5.0, self._idle_timeout_s))
        while not self._closed:
            time.sleep(interval)
            with self._lock:
                if self._closed:
                    return
                if (
                    self._proc is not None
                    and (time.monotonic() - self._last_activity) > self._idle_timeout_s
                ):
                    _log.info(
                        "Transcription idle > %.0fs — releasing subprocess to free memory",
                        self._idle_timeout_s,
                    )
                    self._kill_locked()


# ---------------------------------------------------------------------------
# Child process (run as `python -m murmur.transcribe.isolated <fd>`)
# ---------------------------------------------------------------------------
def _mlx_call(mx: object, name: str, *args: object) -> None:
    """Call an MLX memory-management function, tolerating API differences.

    Newer MLX exposes these at the top level (``mx.clear_cache``); older
    versions under ``mx.metal`` (``mx.metal.clear_cache``). Missing entirely →
    no-op (the subprocess isolation still bounds memory on its own).
    """
    fn = getattr(mx, name, None)
    if fn is None:
        metal = getattr(mx, "metal", None)
        fn = getattr(metal, name, None) if metal is not None else None
    if fn is not None:
        with contextlib.suppress(Exception):
            fn(*args)


def _child_loop(sock: socket.socket) -> int:
    try:
        init = _recv(sock)
    except (OSError, EOFError):
        return 1
    if not isinstance(init, dict):
        return 1

    try:
        import mlx.core as mx

        _mlx_call(mx, "set_cache_limit", int(init.get("cache_limit", 0) or 0))
        mem = init.get("memory_limit")
        if mem:
            _mlx_call(mx, "set_memory_limit", int(mem))

        from murmur.transcribe.local import MlxWhisperTranscriber

        transcriber = MlxWhisperTranscriber(init["model_ref"])
    except Exception as exc:  # startup failure → report and exit
        with contextlib.suppress(Exception):
            _send(sock, ("startup_error", repr(exc)))
        return 1

    with contextlib.suppress(Exception):
        _send(sock, ("ready", None))

    while True:
        try:
            req = _recv(sock)
        except (OSError, EOFError):
            break
        if not isinstance(req, tuple) or not req:
            break
        cmd = req[0]
        if cmd == "shutdown":
            break
        if cmd != "transcribe":
            with contextlib.suppress(Exception):
                _send(sock, ("error", f"unknown command: {cmd!r}"))
            continue

        _, audio, language, initial_prompt = req
        try:
            result = transcriber.transcribe(
                audio, language=language, initial_prompt=initial_prompt
            )
            _send(sock, ("ok", result))
        except TranscriptionError as exc:
            with contextlib.suppress(Exception):
                _send(sock, ("error", str(exc)))
        except Exception as exc:  # never let the child die on one bad segment
            with contextlib.suppress(Exception):
                _send(sock, ("error", f"unexpected: {exc!r}"))
        finally:
            _mlx_call(mx, "clear_cache")  # return Metal buffers to the OS

    return 0


def _run_child(argv: list[str]) -> int:
    if not argv:
        return 2
    try:
        fd = int(argv[0])
    except ValueError:
        return 2
    sock = socket.socket(fileno=fd)
    try:
        return _child_loop(sock)
    finally:
        with contextlib.suppress(Exception):
            sock.close()


if __name__ == "__main__":
    raise SystemExit(_run_child(sys.argv[1:]))
