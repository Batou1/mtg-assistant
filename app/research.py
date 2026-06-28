"""Web research via the Brave Search API.

For 60-card formats there is no reliable decklist API (MTGGoldfish / mtgdecks
deck pages are Cloudflare-blocked). So instead of scraping exact lists, we use
Brave to surface *recent* meta pages and feed their titles/snippets to the LLM
as grounding context. Every card the LLM then proposes is validated against
Scryfall (existence + format legality) downstream, so the output stays factual.
"""
import re

import httpx

from .config import settings

_TAG_RE = re.compile(r"<[^>]+>")


def _strip(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def brave_search(query: str, count: int = 8) -> list[dict]:
    """Return [{title, url, description}] from Brave, or [] if unavailable."""
    if not settings.brave_api_key:
        return []
    try:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": count},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": settings.brave_api_key,
            },
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError:
        return []

    results = []
    for res in (payload.get("web") or {}).get("results", []) or []:
        results.append(
            {
                "title": _strip(res.get("title", "")),
                "url": res.get("url", ""),
                "description": _strip(res.get("description", "")),
            }
        )
    return results


def context_block(results: list[dict], limit: int = 8) -> str:
    """Compact title+snippet text to ground the LLM in the current meta."""
    lines = []
    for r in results[:limit]:
        line = r["title"]
        if r["description"]:
            line += f" — {r['description']}"
        lines.append(line)
    return "\n".join(lines)
