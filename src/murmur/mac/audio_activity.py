"""Which apps are using the microphone or playing audio right now.

Uses CoreAudio's per-process audio objects (macOS 14.2+) via ctypes — no extra
dependency and no permission prompt. Each process object reports its PID,
bundle ID, and whether it is currently running audio *input* or *output*.

That gives two cheap, reliable context signals for a segment of captured audio:

* another app is using the mic (Zoom, Meet in a browser, a Slack huddle …)
  → the user is on a **call**;
* otherwise, an app is playing audio (a browser video, Music, a movie …)
  → whatever the mic picked up is likely **media**.

Calls win over media: during a call the call app is also playing audio.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import struct
from dataclasses import dataclass
from typing import Literal

_log = logging.getLogger(__name__)

ContextKind = Literal["call", "media"]

_SYSTEM_OBJECT = 1  # kAudioObjectSystemObject
_UTF8 = 0x08000100  # kCFStringEncodingUTF8


def _fourcc(code: str) -> int:
    value: int = struct.unpack(">I", code.encode("ascii"))[0]
    return value


_GLOBAL = _fourcc("glob")  # kAudioObjectPropertyScopeGlobal
_PROCESS_LIST = _fourcc("prs#")  # kAudioHardwarePropertyProcessObjectList
_PID = _fourcc("ppid")  # kAudioProcessPropertyPID
_BUNDLE_ID = _fourcc("pbid")  # kAudioProcessPropertyBundleID
_RUNNING_INPUT = _fourcc("piri")  # kAudioProcessPropertyIsRunningInput
_RUNNING_OUTPUT = _fourcc("piro")  # kAudioProcessPropertyIsRunningOutput


@dataclass(frozen=True)
class AudioProcess:
    pid: int
    bundle_id: str
    input: bool
    output: bool


class _Address(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 — ctypes requires a plain class attribute
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


class _CoreAudio:
    def __init__(self) -> None:
        ca_path = ctypes.util.find_library("CoreAudio")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not ca_path or not cf_path:
            raise OSError("CoreAudio/CoreFoundation not available")
        self._ca = ctypes.CDLL(ca_path)
        self._cf = ctypes.CDLL(cf_path)
        addr_p = ctypes.POINTER(_Address)
        u32_p = ctypes.POINTER(ctypes.c_uint32)
        self._ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, addr_p, ctypes.c_uint32, ctypes.c_void_p, u32_p,
        ]
        self._ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, addr_p, ctypes.c_uint32, ctypes.c_void_p, u32_p, ctypes.c_void_p,
        ]
        self._cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
        ]
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]

    def _get(self, obj: int, selector: int, size: int | None = None) -> bytes | None:
        addr = _Address(selector, _GLOBAL, 0)
        n = ctypes.c_uint32(0)
        if size is None:
            if self._ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(n)):
                return None
        else:
            n.value = size
        buf = ctypes.create_string_buffer(max(n.value, 1))
        if self._ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(n), buf):
            return None
        return buf.raw[: n.value]

    def _u32(self, obj: int, selector: int) -> int | None:
        raw = self._get(obj, selector, 4)
        return struct.unpack("I", raw)[0] if raw and len(raw) == 4 else None

    def _bundle_id(self, obj: int) -> str:
        raw = self._get(obj, _BUNDLE_ID, ctypes.sizeof(ctypes.c_void_p))
        if not raw:
            return ""
        ref = struct.unpack("P", raw)[0]
        if not ref:
            return ""
        try:
            out = ctypes.create_string_buffer(512)
            ok = self._cf.CFStringGetCString(ref, out, 512, _UTF8)
            return out.value.decode("utf-8", "replace") if ok else ""
        finally:
            self._cf.CFRelease(ref)  # the getter follows the Copy rule

    def processes(self) -> list[AudioProcess]:
        raw = self._get(_SYSTEM_OBJECT, _PROCESS_LIST)
        if raw is None:
            raise OSError("CoreAudio process list unavailable (needs macOS 14.2+)")
        result = []
        for obj in struct.unpack(f"{len(raw) // 4}I", raw):
            ri, ro = self._u32(obj, _RUNNING_INPUT), self._u32(obj, _RUNNING_OUTPUT)
            if not ri and not ro:
                continue
            pid = self._u32(obj, _PID)
            result.append(
                AudioProcess(
                    pid=int(ctypes.c_int32(pid).value) if pid is not None else -1,
                    bundle_id=self._bundle_id(obj),
                    input=bool(ri),
                    output=bool(ro),
                )
            )
        return result


_core: _CoreAudio | None = None


def list_audio_processes() -> list[AudioProcess]:
    """Processes currently running audio input or output.

    Raises:
        OSError: if the CoreAudio process API is unavailable on this system.
    """
    global _core
    if _core is None:
        _core = _CoreAudio()
    return _core.processes()


# Apps whose holding the mic means "on a call". Matched as a bundle-ID prefix so
# helper processes count too (e.g. "com.google.Chrome.helper" for Meet).
# Plenty of other things hold the mic without a call — the system "Hey Siri"
# listener (com.apple.CoreSpeech) does so almost constantly, dictation tools
# (Claude desktop voice input, macOS Dictation) while you talk to them — so
# mic use by anything NOT listed here is ignored rather than called a call.
CHAT_APPS: tuple[str, ...] = (
    "us.zoom.xos",
    "com.microsoft.teams",
    "com.microsoft.teams2",
    "com.tinyspeck.slackmacgap",  # Slack huddles
    "com.apple.FaceTime",
    "com.cisco.webexmeetingsapp",
    "Cisco-Systems.Spark",  # Webex
    "net.whatsapp.WhatsApp",
    "com.hnc.Discord",
    "ru.keepcoder.Telegram",
    "com.skype.skype",
)
BROWSERS: tuple[str, ...] = (
    # A browser on the mic is almost always a web call (Google Meet, etc.).
    "com.google.Chrome",
    "com.apple.Safari",
    "com.apple.WebKit",  # Safari's media/web-content helpers
    "org.mozilla.firefox",
    "com.microsoft.edgemac",
    "company.thebrowser.Browser",  # Arc
    "com.brave.Browser",
)
CALL_APPS: tuple[str, ...] = CHAT_APPS + BROWSERS

# Apps whose sound output is notifications/ringing, not media. Chat apps (and
# Chromium-based ones in particular) keep the output stream open for several
# seconds after a ding, so without this anything said right after a Slack
# notification would be tagged "media". When they hold the mic too, it's a call.
NOT_MEDIA_APPS: tuple[str, ...] = CHAT_APPS + (
    "com.apple.MobileSMS",  # Messages
    "com.apple.mail",
    "com.apple.iCal",
    "com.apple.notificationcenterui",
)


def _matches(bundle_id: str, prefixes: tuple[str, ...]) -> bool:
    return any(bundle_id == p or bundle_id.startswith(p + ".") for p in prefixes)


def is_call_app(bundle_id: str) -> bool:
    return _matches(bundle_id, CALL_APPS)


def classify(
    procs: list[AudioProcess], self_pid: int | None = None
) -> tuple[ContextKind | None, str | None]:
    """Map active audio processes to ("call"|"media"|None, app bundle id).

    Our own process (the recorder, which always holds the mic) is ignored, and
    only known call apps holding the mic count as a call (see CALL_APPS).
    """
    own = os.getpid() if self_pid is None else self_pid
    others = [p for p in procs if p.pid != own]
    callers = [p for p in others if p.input and is_call_app(p.bundle_id)]
    if callers:
        return "call", callers[0].bundle_id or f"pid:{callers[0].pid}"
    players = [p for p in others if p.output and not _matches(p.bundle_id, NOT_MEDIA_APPS)]
    if players:
        return "media", players[0].bundle_id or f"pid:{players[0].pid}"
    return None, None
