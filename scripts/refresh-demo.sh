#!/usr/bin/env bash
# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Regenerate the GitHub Pages demo (docs/index.html) from a running prober.
#
# The demo is a single self-contained, fully-redacted HTML artifact — safe to
# commit. This rebuilds it so the published Pages site reflects current case
# data. Run it, then commit docs/index.html.
#
# Usage:
#   scripts/refresh-demo.sh                 # pull from the local prober (:8731)
#   PROBER_URL=http://127.0.0.1:8098 scripts/refresh-demo.sh
set -euo pipefail

PROBER_URL="${PROBER_URL:-http://127.0.0.1:8731}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/docs/index.html"

echo "Fetching demo from ${PROBER_URL}/api/export-demo ..."
curl -fsS "${PROBER_URL}/api/export-demo" -o "$OUT"

# Safety net: refuse to publish if any real receipt number slipped through.
# The all-zeros IOE0000000000 is a static placeholder baked into the bundled
# app.js (not real case data), so it's excluded from the leak check.
leaked=$(grep -oE '\bIOE[0-9]{7,}\b' "$OUT" | grep -v '^IOE0000000000$' || true)
if [ -n "$leaked" ]; then
  echo "ERROR: unmasked receipt number(s) present in $OUT — NOT publishing:" >&2
  echo "$leaked" | sort -u >&2
  exit 1
fi

bytes=$(wc -c < "$OUT")
echo "Wrote $OUT (${bytes} bytes). Review, then commit docs/index.html."
