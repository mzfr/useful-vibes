#!/usr/bin/env bash
# Build src/ui/static/css/tailwind.css from src/ui/static/css/input.css.
# Uses the Tailwind standalone CLI binary — no Node toolchain required.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$ROOT/.tools"
mkdir -p "$BIN_DIR"

VERSION="4.1.14"
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)
    asset="tailwindcss-linux-x64"
    checksum="bc34c301b080b6e6b98ed24118419833f966f6f347e556945d6557d36a44a56e" ;;
  Linux-aarch64)
    asset="tailwindcss-linux-arm64"
    checksum="314941f5f6e143e74e740c587ad1fbaaede5462572dd330bbe0937e611e966db" ;;
  Darwin-arm64)
    asset="tailwindcss-macos-arm64"
    checksum="e722b752f51def86d42e886b4c1171f2d09a4be1a7487a0a51e4aff8e7603ce3" ;;
  Darwin-x86_64)
    asset="tailwindcss-macos-x64"
    checksum="67b25b6103fa7677637e5a5de3327fec3335da316d90d3fdb1a4cd72bda41c0a" ;;
  *) echo "Unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

BIN="$BIN_DIR/tailwindcss"
if [ ! -x "$BIN" ]; then
  echo "Downloading Tailwind standalone CLI v$VERSION..."
  curl -fsSL \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/v$VERSION/$asset" \
    -o "$BIN.tmp"
  echo "$checksum  $BIN.tmp" | sha256sum -c -
  mv "$BIN.tmp" "$BIN"
  chmod +x "$BIN"
fi

INPUT="$ROOT/src/ui/static/css/input.css"
OUTPUT="$ROOT/src/ui/static/css/tailwind.css"

# Tailwind v4 reads its config from CSS (`@theme`, `@source`) in input.css.
if [ "${1:-}" = "--watch" ]; then
  exec "$BIN" -i "$INPUT" -o "$OUTPUT" --watch
fi

exec "$BIN" -i "$INPUT" -o "$OUTPUT" --minify
