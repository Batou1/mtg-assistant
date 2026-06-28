"""60-card format pipeline (Standard, Pauper, Modern, Pioneer…).

There is no reliable exact-decklist source for these formats (the popular sites
are Cloudflare-blocked), so we research the *archetype* instead:

1. Brave surfaces recent meta pages for the format + wish.
2. The local LLM proposes an archetype with its key cards, grounded by those
   search snippets.
3. Every proposed card is validated against Scryfall — it must exist AND be
   legal in the format, otherwise it's dropped. This keeps the output factual
   even though the model named the cards.
4. Gap analysis vs the active profile's collection + a budget buylist in EUR.
"""
import httpx

from . import buylist, db, llm, research, scryfall
from .config import settings

# Formats handled here (everything that isn't Commander).
FORMATS = {"standard", "modern", "pioneer", "pauper", "legacy", "vintage"}

_COLOR_WORDS = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}


def _norm(name: str) -> str:
    return name.split("//")[0].strip().lower()


def _query(fmt: str, intent: dict) -> str:
    parts = ["MTG", fmt]
    parts += intent.get("keywords") or []
    parts += [_COLOR_WORDS[c] for c in (intent.get("colors") or []) if c in _COLOR_WORDS]
    parts += ["deck", "decklist", "metagame", "2026"]
    return " ".join(parts)


def analyze(intent: dict, profile_id: int) -> dict:
    fmt = intent.get("format")
    results = research.brave_search(_query(fmt, intent))
    context = research.context_block(results)

    archetype = llm.archetype_research(fmt, intent, context)
    if not archetype:
        return {"format": fmt, "llm_unavailable": True, "sources": results}

    names = [n for n in (archetype.get("key_cards") or []) if isinstance(n, str)]
    owned_keys = db.owned_name_keys(profile_id)
    budget = intent.get("budget_eur")

    with httpx.Client(timeout=30, headers={"User-Agent": settings.user_agent}) as client:
        resolved, _nf = scryfall.resolve_cards(names, client=client) if names else ({}, [])

        valid, dropped, seen = [], [], set()
        for name in names:
            card = resolved.get(_norm(name))
            key = _norm(name)
            if card and key not in seen and scryfall.legal_in(card, fmt):
                valid.append(card)
                seen.add(key)
            elif key not in seen:
                dropped.append(name)
                seen.add(key)

        owned = [c for c in valid if _norm(c["name"]) in owned_keys]
        missing = [c for c in valid if _norm(c["name"]) not in owned_keys]
        buy = buylist.build([c["name"] for c in missing], budget, client=client)

    return {
        "format": fmt,
        "archetype": {
            "name": archetype.get("archetype") or "Archétype",
            "colors": [c for c in (archetype.get("colors") or []) if c in _COLOR_WORDS],
            "strategy": (archetype.get("strategy") or "").strip(),
        },
        "owned_cards": [
            {"name": c["name"], "image": scryfall.image(c)} for c in owned
        ],
        "buylist": buy,
        "valid_count": len(valid),
        "owned_count": len(owned),
        "missing_count": len(missing),
        "dropped": dropped,
        "sources": results[:6],
        "budget_eur": budget,
        "llm_unavailable": False,
    }
