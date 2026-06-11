"""Audio capture constants and sub-package."""

SAMPLE_RATE: int = 16_000
"""Capture sample rate in Hz: mono 16 kHz (R-1)."""

FRAME_SAMPLES: int = 512
"""Silero v5+ requires exactly 512-sample windows at 16 kHz → 32 ms frames."""

PRE_ROLL_S: float = 0.3
"""Pre-roll padding: 10 frames = 320 ms (within AC-15 ±50 ms tolerance)."""

POST_ROLL_S: float = 0.3
"""Post-roll padding."""

WATCHDOG_STALL_S: float = 3.0
"""No-frame threshold before stream reopen (seconds)."""

DEVICE_POLL_S: float = 2.0
"""Device-signature poll interval (seconds)."""

DISK_MIN_FREE_BYTES: int = 2 * 1024**3
"""Minimum free disk space before capture pauses (R-20): 2 GiB."""

DISK_POLL_S: float = 60.0
"""Disk-free check interval when gate is active (seconds)."""

PERMISSION_RECHECK_S: float = 60.0
"""Microphone permission recheck interval (seconds)."""

MAX_LOCAL_FAILURES: int = 3
"""Consecutive local transcription failures before API fallback (E-9)."""
