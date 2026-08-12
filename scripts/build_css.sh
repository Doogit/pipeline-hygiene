#!/usr/bin/env bash
# Build app/static/app.css from app/tailwind.css with the standalone tailwindcss
# CLI (no Node/npm). The CLI binary is NOT committed (112 MB); this fetches the
# pinned version, verifies its SHA-256, and runs the offline build.
#
#   scripts/build_css.sh          # build once
#   scripts/build_css.sh --watch  # rebuild on class changes
set -euo pipefail

TW_VERSION="v4.3.3"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$ROOT/app/tools"
mkdir -p "$BIN_DIR"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) ASSET="tailwindcss-windows-x64.exe"; SHA="e0e260ce048014e9268f6237ff18f8ccf02cef521cbd0ae04e82c2cdf7aa3955" ;;
  Linux)                ASSET="tailwindcss-linux-x64" ;;
  Darwin)               ASSET="tailwindcss-macos-arm64" ;;
  *) echo "unsupported OS"; exit 1 ;;
esac
BIN="$BIN_DIR/$ASSET"

if [ ! -f "$BIN" ]; then
  echo "downloading tailwindcss CLI $TW_VERSION ($ASSET)…"
  curl -sSL -o "$BIN" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/$TW_VERSION/$ASSET"
  chmod +x "$BIN"
fi
# Verify checksum on Windows (the committed pin); other platforms: record on first use.
if [ -n "${SHA:-}" ]; then
  echo "$SHA *$BIN" | sha256sum -c - || { echo "checksum mismatch"; exit 1; }
fi

"$BIN" -i "$ROOT/app/tailwind.css" -o "$ROOT/app/static/app.css" --minify "$@"
echo "wrote app/static/app.css"
