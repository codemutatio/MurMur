"""Per-process memory footprint on macOS (the number Activity Monitor shows).

Uses libproc's ``proc_pid_rusage`` rather than RSS: RSS undercounts compressed
and swapped pages, which is exactly where a slow leak hides on a busy Mac.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
from dataclasses import dataclass

_RUSAGE_INFO_V4 = 4
# rusage_info_v4: a 16-byte uuid, then uint64 fields. Indexes into those:
_PHYS_FOOTPRINT = 7
_LIFETIME_MAX_PHYS_FOOTPRINT = 28
_N_FIELDS = 64  # generous: the kernel writes only sizeof(rusage_info_v4)


class _RusageInfo(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16), ("v", ctypes.c_uint64 * _N_FIELDS)]


@dataclass(frozen=True)
class Footprint:
    current: int  # bytes
    peak: int  # bytes, over the process lifetime


_libproc: ctypes.CDLL | None = None


def _lib() -> ctypes.CDLL | None:
    global _libproc
    if _libproc is None and sys.platform == "darwin":
        path = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
        try:
            _libproc = ctypes.CDLL(path, use_errno=True)
        except OSError:
            return None
    return _libproc


def footprint(pid: int) -> Footprint | None:
    """Memory footprint of ``pid``, or None if unavailable (gone, not macOS…)."""
    lib = _lib()
    if lib is None:
        return None
    info = _RusageInfo()
    if lib.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(_RUSAGE_INFO_V4), ctypes.byref(info)) != 0:
        return None
    return Footprint(int(info.v[_PHYS_FOOTPRINT]), int(info.v[_LIFETIME_MAX_PHYS_FOOTPRINT]))
