#!/usr/bin/env bash
# Pin htmx + alpine to local files so the app boots offline.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JS_DIR="$ROOT/src/ui/static/js"
mkdir -p "$JS_DIR"

HTMX_VERSION="1.9.12"
ALPINE_VERSION="3.14.1"

curl -fsSL "https://unpkg.com/htmx.org@${HTMX_VERSION}/dist/htmx.min.js" \
  -o "$JS_DIR/htmx.min.js"
curl -fsSL "https://unpkg.com/alpinejs@${ALPINE_VERSION}/dist/cdn.min.js" \
  -o "$JS_DIR/alpine.min.js"

echo "Vendored htmx ${HTMX_VERSION} + alpine ${ALPINE_VERSION} → $JS_DIR/"
