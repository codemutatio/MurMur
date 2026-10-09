#!/usr/bin/env bash
# Install/refresh the uv-tool environment that Murmur.app runs on, pinned to
# uv.lock and the repo's Python version — so the app gets exactly the versions
# the test suite ran against.
#
# Why this exists: `uv tool install .` ignores uv.lock and resolves fresh, and
# picks whatever Python it likes. That silently put the app on Python 3.12 (the
# .app, built for 3.11, then crashed on launch) and on torch 2.14 with
# torchaudio 2.11 (a mismatched pair) while tests ran on the locked versions.
#
# Usage:  scripts/install_app_env.sh [extra `uv tool install` args, e.g. --with py2app]
# Then restart Murmur.app for the change to take effect.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY_VERSION="$(tr -d '[:space:]' < "$REPO/.python-version")"
CONSTRAINTS="$(mktemp -t murmur-constraints.XXXXXX)"
trap 'rm -f "$CONSTRAINTS"' EXIT

cd "$REPO"
# setuptools is left unpinned: py2app needs setuptools<80 at build time
# (packaging/build_app.sh passes --with 'setuptools<80'), and nothing in
# Murmur depends on a specific setuptools at runtime.
uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt 2>/dev/null \
  | grep -v '^setuptools==' > "$CONSTRAINTS"

echo "==> Installing murmur into the uv tool env (Python ${PY_VERSION}, pinned to uv.lock)…"
uv tool install --reinstall --python "$PY_VERSION" --constraints "$CONSTRAINTS" "$@" .

TOOL_PY="$(uv tool dir)/murmur/bin/python"
"$TOOL_PY" - "$PY_VERSION" <<'PY'
import importlib.metadata as m
import sys

want = sys.argv[1]
got = f"{sys.version_info[0]}.{sys.version_info[1]}"
pkgs = ("torch", "torchaudio", "numpy", "speechbrain", "mlx-whisper")
print("==> Murmur app env: python", sys.version.split()[0], "|",
      ", ".join(f"{p} {m.version(p)}" for p in pkgs))
torch_mm = ".".join(m.version("torch").split(".")[:2])
ta_mm = ".".join(m.version("torchaudio").split(".")[:2])
if got != want or torch_mm != ta_mm:
    sys.exit(f"!! environment mismatch: python {got} (want {want}), torch {torch_mm} vs torchaudio {ta_mm}")
PY
echo "==> Done. Restart Murmur.app to pick it up."
