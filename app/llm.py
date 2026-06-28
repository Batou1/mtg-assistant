"""Thin client for a local Ollama server.

The LLM's only job in Phase 1 is to turn a free-text wish (in French) into a
structured intent. Card selection stays deterministic elsewhere, so the model
never needs to name real cards — this keeps hallucinations out of results.
"""
import json

import httpx

from .config import settings


def is_available() -> bool:
    """True if the Ollama server answers. Used to fall back gracefully."""
    try:
        resp = httpx.get(f"{settings.ollama_url}/api/tags", timeout=2)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def chat_json(system: str, user: str) -> dict | None:
    """Send a chat request asking for a strict JSON object back.

    Returns the parsed dict, or None if Ollama is unreachable or the reply
    wasn't valid JSON (caller falls back to a heuristic).
    """
    payload = {
        "model": settings.ollama_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.1},
    }
    try:
        resp = httpx.post(
            f"{settings.ollama_url}/api/chat",
            json=payload,
            timeout=settings.ollama_timeout,
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
    except httpx.HTTPError:
        return None

    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
