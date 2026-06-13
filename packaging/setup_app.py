"""py2app build configuration for Murmur.app.

Build with ``packaging/build_app.sh`` (which installs py2app into the uv tool
env and runs an alias build). Alias mode references the installed ``murmur``
package in ``~/.local`` (not TCC-protected), so we avoid both the
torch-bundling problem and the ``~/Documents`` permission wall.

The ``NSMicrophoneUsageDescription`` plist key is the whole point: a bundled
app that declares it can show the macOS Microphone prompt and obtain access.
"""

import os

from setuptools import setup

# Build from this directory (see build_app.sh): running from the repo root would
# make setuptools fold pyproject.toml's [project].dependencies into
# install_requires, which py2app rejects ("install_requires is no longer
# supported"). packaging/ has no pyproject.toml, so that never happens.
_HERE = os.path.dirname(os.path.abspath(__file__))

setup(
    app=[os.path.join(_HERE, "murmur_app.py")],
    name="Murmur",
    options={
        "py2app": {
            "argv_emulation": False,
            "packages": ["murmur"],
            "plist": {
                "CFBundleName": "Murmur",
                "CFBundleDisplayName": "Murmur",
                "CFBundleIdentifier": "com.jay.murmur",
                "CFBundleVersion": "0.1.0",
                "CFBundleShortVersionString": "0.1.0",
                # Menubar-only utility: no Dock icon. The window opens from the
                # menubar's "Open Murmur Window" item.
                "LSUIElement": True,
                "LSMinimumSystemVersion": "13.0",
                "NSMicrophoneUsageDescription": (
                    "Murmur transcribes your microphone on-device and appends the "
                    "text to your daily transcript. Audio is never sent anywhere."
                ),
                "NSHumanReadableCopyright": "Personal project — local use only.",
            },
        }
    },
)
