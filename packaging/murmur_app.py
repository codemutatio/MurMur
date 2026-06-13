"""Murmur.app entry point — launches the menubar app in GUI mode.

Bundled by py2app (see setup_app.py / build_app.sh). The .app's Info.plist
declares ``NSMicrophoneUsageDescription``, which is what lets the app obtain
Microphone permission — bare framework Python cannot, which is why the prior
launchd-on-plain-python deployment could never capture audio.
"""

from murmur.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["run"]))
