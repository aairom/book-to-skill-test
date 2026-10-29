#!/usr/bin/env bash
# ─── Book-to-Skill Web App — Stop script ──────────────────────────────────────
# Gracefully stops the background server.
# Usage: ./scripts/stop.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_ROOT/.server.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "⚠  No PID file found — server may not be running."
  exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  echo "🛑 Stopping server (PID $PID)…"
  kill -TERM "$PID"
  # Wait up to 5 s for graceful shutdown
  for _ in $(seq 1 10); do
    sleep 0.5
    kill -0 "$PID" 2>/dev/null || break
  done
  if kill -0 "$PID" 2>/dev/null; then
    echo "⚠  Force-killing PID $PID"
    kill -KILL "$PID" || true
  fi
  echo "✅ Server stopped."
else
  echo "ℹ  Process $PID is not running."
fi

rm -f "$PID_FILE"
