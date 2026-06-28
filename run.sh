#!/usr/bin/env bash
# Launch MTG Assistant locally.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

PORT="${MTG_PORT:-8000}"
echo "MTG Assistant → http://127.0.0.1:${PORT}"
echo "LLM (Ollama)  → ${MTG_OLLAMA_URL:-http://127.0.0.1:11434} / ${MTG_OLLAMA_MODEL:-qwen2.5:7b-instruct}"
exec uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" "$@"
