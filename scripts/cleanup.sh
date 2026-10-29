#!/usr/bin/env bash
# ─── Book-to-Skill Web App — Cleanup script ───────────────────────────────────
# Removes build artifacts: .venv, node_modules (none here), __pycache__,
# cloned repo (optional), pulled container images.
# Usage: ./scripts/cleanup.sh [--full]
#   --full  also removes the cloned book-to-skill repository

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
FULL_CLEAN=false
if [[ "${1:-}" == "--full" ]]; then FULL_CLEAN=true; fi

echo "🧹 Cleaning up Book-to-Skill project…"

# ── Stop server if running ─────────────────────────────────────────────────
"$SCRIPT_DIR/stop.sh" 2>/dev/null || true

# ── Python artifacts ───────────────────────────────────────────────────────
echo "   Removing .venv…"
rm -rf "$PROJECT_ROOT/.venv"

echo "   Removing __pycache__ directories…"
find "$PROJECT_ROOT" -type d -name "__pycache__" -not -path "*/book_to_skill_repo/*" \
  -exec rm -rf {} + 2>/dev/null || true

echo "   Removing *.pyc files…"
find "$PROJECT_ROOT" -name "*.pyc" -not -path "*/book_to_skill_repo/*" \
  -delete 2>/dev/null || true

echo "   Removing .pytest_cache…"
rm -rf "$PROJECT_ROOT/.pytest_cache"

echo "   Removing server.log…"
rm -f "$PROJECT_ROOT/server.log"

# ── Optional: remove cloned repo ────────────────────────────────────────────
if [ "$FULL_CLEAN" = true ]; then
  REPO_DIR="$PROJECT_ROOT/book_to_skill_repo"
  if [ -d "$REPO_DIR" ]; then
    echo "   Removing $REPO_DIR…"
    rm -rf "$REPO_DIR"
  fi
fi

echo ""
echo "✅ Cleanup complete!"
echo "   Note: input/ and output/ directory contents are preserved."
echo "   Run ./scripts/start.sh to reinstall and restart."
