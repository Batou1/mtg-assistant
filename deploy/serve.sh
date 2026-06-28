#!/usr/bin/env bash
# Foreground server process launched by the launchd service.
# Assumes the virtualenv already exists (install-service.sh creates it).
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

# Load secrets (ANTHROPIC_API_KEY, MTG_BRAVE_API_KEY) from .env if present.
if [ -f "$APP_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$APP_DIR/.env"
  set +a
fi

PORT="${MTG_PORT:-8771}"

# Bind to localhost only — the Cloudflare tunnel connects locally; the app is
# never exposed directly on the network.
exec "$APP_DIR/.venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT"
