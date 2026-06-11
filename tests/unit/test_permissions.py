"""Tests for mac.permissions — T-17, R-18, R-19."""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from murmur.mac.permissions import PermissionStatus


# ---------------------------------------------------------------------------
# microphone_permission
# ---------------------------------------------------------------------------


def test_microphone_granted_returns_granted() -> None:
    """authorizationStatus=3 (Authorized) → PermissionStatus.GRANTED."""
    mock_av = MagicMock()
    mock_av.AVCaptureDevice.authorizationStatusForMediaType_.return_value = 3

    with patch.dict(sys.modules, {"AVFoundation": mock_av}):
        from importlib import reload
        import murmur.mac.permissions as perms

        reload(perms)
        result = perms.microphone_permission()

    assert result == PermissionStatus.GRANTED


def test_microphone_denied_returns_denied() -> None:
    """authorizationStatus=2 (Denied) → PermissionStatus.DENIED."""
    mock_av = MagicMock()
    mock_av.AVCaptureDevice.authorizationStatusForMediaType_.return_value = 2

    with patch.dict(sys.modules, {"AVFoundation": mock_av}):
        from importlib import reload
        import murmur.mac.permissions as perms

        reload(perms)
        result = perms.microphone_permission()

    assert result == PermissionStatus.DENIED


def test_microphone_restricted_returns_denied() -> None:
    """authorizationStatus=1 (Restricted) → PermissionStatus.DENIED."""
    mock_av = MagicMock()
    mock_av.AVCaptureDevice.authorizationStatusForMediaType_.return_value = 1

    with patch.dict(sys.modules, {"AVFoundation": mock_av}):
        from importlib import reload
        import murmur.mac.permissions as perms

        reload(perms)
        result = perms.microphone_permission()

    assert result == PermissionStatus.DENIED


def test_microphone_undetermined_returns_undetermined() -> None:
    """authorizationStatus=0 (NotDetermined) → PermissionStatus.UNDETERMINED."""
    mock_av = MagicMock()
    mock_av.AVCaptureDevice.authorizationStatusForMediaType_.return_value = 0

    with patch.dict(sys.modules, {"AVFoundation": mock_av}):
        from importlib import reload
        import murmur.mac.permissions as perms

        reload(perms)
        result = perms.microphone_permission()

    assert result == PermissionStatus.UNDETERMINED


def test_microphone_permission_import_failure_returns_undetermined() -> None:
    """Import failure for AVFoundation → UNDETERMINED (never crashes)."""
    with patch.dict(sys.modules, {"AVFoundation": None}):
        from importlib import reload
        import murmur.mac.permissions as perms

        reload(perms)
        result = perms.microphone_permission()

    assert result == PermissionStatus.UNDETERMINED


# ---------------------------------------------------------------------------
# accessibility_trusted
# ---------------------------------------------------------------------------


def test_accessibility_trusted_true() -> None:
    """AXIsProcessTrusted=True → accessibility_trusted() returns True."""
    mock_as = MagicMock()
    mock_as.AXIsProcessTrusted.return_value = True

    with patch.dict(sys.modules, {"ApplicationServices": mock_as}):
        from importlib import reload
        import murmur.mac.permissions as perms

        reload(perms)
        result = perms.accessibility_trusted()

    assert result is True


def test_accessibility_trusted_false() -> None:
    """AXIsProcessTrusted=False → accessibility_trusted() returns False."""
    mock_as = MagicMock()
    mock_as.AXIsProcessTrusted.return_value = False

    with patch.dict(sys.modules, {"ApplicationServices": mock_as}):
        from importlib import reload
        import murmur.mac.permissions as perms

        reload(perms)
        result = perms.accessibility_trusted()

    assert result is False


def test_accessibility_import_failure_returns_false() -> None:
    """Import failure for ApplicationServices → False (never crashes)."""
    with patch.dict(sys.modules, {"ApplicationServices": None}):
        from importlib import reload
        import murmur.mac.permissions as perms

        reload(perms)
        result = perms.accessibility_trusted()

    assert result is False


def test_status_mapping() -> None:
    """PermissionStatus enum has correct string values."""
    assert PermissionStatus.GRANTED == "GRANTED"
    assert PermissionStatus.DENIED == "DENIED"
    assert PermissionStatus.UNDETERMINED == "UNDETERMINED"
