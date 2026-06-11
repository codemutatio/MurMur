"""macOS permission probes: microphone (AVFoundation) and Accessibility."""

from __future__ import annotations

from enum import StrEnum


class PermissionStatus(StrEnum):
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    UNDETERMINED = "UNDETERMINED"


def microphone_permission() -> PermissionStatus:
    """Return current microphone TCC permission status via AVFoundation."""
    try:
        import AVFoundation

        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_("soun")
        # 0=NotDetermined, 1=Restricted, 2=Denied, 3=Authorized
        if status == 3:
            return PermissionStatus.GRANTED
        if status == 2 or status == 1:
            return PermissionStatus.DENIED
        return PermissionStatus.UNDETERMINED
    except Exception:
        return PermissionStatus.UNDETERMINED


def request_microphone_access() -> None:
    """Trigger TCC microphone permission prompt (fire-and-forget).

    Has no effect if permission is already determined.
    """
    try:
        import AVFoundation

        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            "soun", lambda _: None
        )
    except Exception:
        pass


def accessibility_trusted() -> bool:
    """Return True if the process has Accessibility (AX) trust.

    Required for pynput GlobalHotKeys to register (R-19).
    """
    try:
        import ApplicationServices

        return bool(ApplicationServices.AXIsProcessTrusted())
    except Exception:
        return False
