#!/usr/bin/env bash
# Launch MTG Assistant locally.
set -euo pipefail
cd "$(dirname "$0")"

# Load local secrets (e.g. MTG_BRAVE_API_KEY) from .env if present (gitignored).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

PORT="${MTG_PORT:-8000}"
echo "MTG Assistant → http://127.0.0.1:${PORT}"
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "LLM (Anthropic) → ${MTG_ANTHROPIC_MODEL:-claude-sonnet-5}"
else
  echo "LLM → aucune clé ANTHROPIC_API_KEY : parsing heuristique, pas de chat complet"
fi
exec uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" "$@"
