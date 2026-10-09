#!/usr/bin/env bash
# Build Murmur.app (py2app alias mode) and install it to /Applications.
#
# Why alias mode: it references the murmur package in the uv tool env
# (~/.local — NOT TCC-protected), so we sidestep both the torch-bundling
# nightmare and the ~/Documents permission wall that killed the plain-python
# launchd agent. The bundle's Info.plist declares NSMicrophoneUsageDescription,
# which is what lets the app obtain Microphone permission.
#
# We build from packaging/ (not the repo root): running py2app where
# pyproject.toml lives makes setuptools fold [project].dependencies into
# install_requires, which py2app rejects. setuptools is pinned < 80 to match
# py2app 0.28's expectations.
#
# Re-run this after code changes (alias mode picks up the reinstalled package).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing murmur + py2app into the uv tool env (~/.local)…"
# Pinned to uv.lock + .python-version (see scripts/install_app_env.sh for why).
"$REPO/scripts/install_app_env.sh" --with py2app --with 'setuptools<80'

TOOL_PY="$(uv tool dir)/murmur/bin/python"
if [[ ! -x "$TOOL_PY" ]]; then
  echo "error: tool python not found at $TOOL_PY" >&2
  exit 1
fi

echo "==> Building Murmur.app (py2app alias mode)…"
cd "$REPO/packaging"
rm -rf build dist
"$TOOL_PY" setup_app.py py2app -A

APP="$REPO/packaging/dist/Murmur.app"
[[ -d "$APP" ]] || { echo "error: build did not produce $APP" >&2; exit 1; }

DEST="/Applications/Murmur.app"
echo "==> Installing to $DEST…"
rm -rf "$DEST"
cp -R "$APP" "$DEST"

echo "==> Ad-hoc signing…"
codesign --force --deep --sign - "$DEST"

cat <<'NOTE'

✅ Built and installed /Applications/Murmur.app

Next:
  1. open -a Murmur
  2. Approve the "Murmur" Microphone prompt when it appears.
  3. Speak — text lands in ~/Transcripts; open the window from the menubar
     ("Open Murmur Window").

For always-on at login:  murmur install-app-agent

Notes:
  • Ad-hoc-signed alias build for THIS machine (references the ~/.local tool
    env). A self-signed/Developer ID cert makes the mic grant survive rebuilds.
  • Not distributable to other Macs — that needs a full standalone + notarize.
NOTE
