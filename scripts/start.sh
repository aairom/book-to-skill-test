#!/usr/bin/env bash
# ─── Book-to-Skill Web App — Start script ─────────────────────────────────────
# Starts the FastAPI backend in detached mode and prints the access URL.
# Usage: ./scripts/start.sh

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# ── Load .env if present (strip \r so Windows-edited files don't break bash) ─
if [ -f "$PROJECT_ROOT/.env" ]; then
  _ENV_TMP="$(mktemp)"
  # Remove carriage returns and skip blank lines / comments
  sed 's/\r//g; /^\s*#/d; /^\s*$/d' "$PROJECT_ROOT/.env" > "$_ENV_TMP"
  set -a
  # shellcheck disable=SC1090
  source "$_ENV_TMP"
  set +a
  rm -f "$_ENV_TMP"
fi

APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8080}"
VENV_DIR="$PROJECT_ROOT/.venv"
LOG_FILE="$PROJECT_ROOT/server.log"
PID_FILE="$PROJECT_ROOT/.server.pid"

# ── Virtual environment ──────────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
  echo "⚙  Creating virtual environment…"
  uv venv "$VENV_DIR"
fi

echo "📦 Installing / syncing dependencies…"
uv pip install -r "$PROJECT_ROOT/requirements.txt" --quiet

# ── Install book-to-skill library ────────────────────────────────────────────
REPO_DIR="${BOOK_TO_SKILL_REPO:-$PROJECT_ROOT/book_to_skill_repo}"
LOCAL_ZIP="$PROJECT_ROOT/_sources/book-to-skill-master.zip"

if [ -d "$REPO_DIR" ]; then
  echo "📚 Installing book-to-skill from $REPO_DIR…"
  uv pip install -e "$REPO_DIR" --quiet 2>/dev/null || \
    uv pip install "$REPO_DIR" --quiet
elif [ -f "$LOCAL_ZIP" ]; then
  echo "📚 Extracting book-to-skill from local zip…"
  mkdir -p "$REPO_DIR"
  unzip -q "$LOCAL_ZIP" -d "$PROJECT_ROOT/_sources/tmp_extract"
  # The zip typically extracts to book-to-skill-master/
  EXTRACTED=$(find "$PROJECT_ROOT/_sources/tmp_extract" -maxdepth 1 -type d | grep -v "^$PROJECT_ROOT/_sources/tmp_extract$" | head -1)
  if [ -n "$EXTRACTED" ]; then
    cp -r "$EXTRACTED/." "$REPO_DIR/"
  fi
  rm -rf "$PROJECT_ROOT/_sources/tmp_extract"
  uv pip install -e "$REPO_DIR" --quiet 2>/dev/null || \
    uv pip install "$REPO_DIR" --quiet
else
  echo "📚 Cloning book-to-skill repository…"
  git clone --depth 1 https://github.com/virgiliojr94/book-to-skill.git "$REPO_DIR"
  uv pip install -e "$REPO_DIR" --quiet 2>/dev/null || \
    uv pip install "$REPO_DIR" --quiet
fi

# ── Stop any existing server ─────────────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE")"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "🛑 Stopping previous server (PID $OLD_PID)…"
    kill "$OLD_PID" || true
    sleep 1
  fi
  rm -f "$PID_FILE"
fi

# ── Launch server ─────────────────────────────────────────────────────────────
RELOAD_FLAG=""
if [ "${ENV:-production}" = "development" ]; then
  RELOAD_FLAG="--reload"
fi

echo "🚀 Starting Book-to-Skill UI…"
"$VENV_DIR/bin/uvicorn" backend.main:app \
  --host "$APP_HOST" \
  --port "$APP_PORT" \
  $RELOAD_FLAG \
  > "$LOG_FILE" 2>&1 &

SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# ── Wait briefly and confirm ──────────────────────────────────────────────────
sleep 2
if kill -0 "$SERVER_PID" 2>/dev/null; then
  echo ""
  echo "✅ Server started successfully!"
  echo ""
  echo "   🌐  UI:  http://localhost:${APP_PORT}"
  echo "   📋  API: http://localhost:${APP_PORT}/docs"
  echo "   📝  Log: $LOG_FILE"
  echo "   🆔  PID: $SERVER_PID"
  echo ""
  echo "   To stop: ./scripts/stop.sh"
else
  echo "❌ Server failed to start. Check $LOG_FILE for details."
  cat "$LOG_FILE"
  exit 1
fi
