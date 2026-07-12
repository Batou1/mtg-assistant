"""60-card format pipeline (Standard, Pauper, Modern, Pioneer…).

There is no reliable exact-decklist source for these formats (the popular sites
are Cloudflare-blocked), so we research the *archetype* instead:

1. Brave surfaces recent meta pages for the format + wish.
2. The local LLM proposes a full 60-card decklist (multiple copies per card,
   4-of rule, non-basic lands included) plus a basic-land manabase, grounded by
   those search snippets.
3. Every proposed card is validated against Scryfall — it must exist AND be
   legal in the format, otherwise it's dropped; copy counts are re-clamped to
   the 4-of rule. Basic lands then top the deck up to exactly 60 cards, so the
   output stays a legal, complete deck even after invalid cards are removed.
4. Gap analysis vs the active profile's collection (per COPY, not per name:
   owning 2 of a 4-of leaves 2 to buy) + a budget buylist in EUR.
"""
import httpx

from . import buylist, db, llm, poolbuild, research, scryfall
from .config import settings
from .poolbuild import CATEGORY_LABELS, CATEGORY_ORDER

# Formats handled here (everything that isn't Commander).
# Premodern (4th Edition → Scourge, no reprints from later sets) is a 60-card
# eternal format like the others; Scryfall tracks its legality under the
# ``premodern`` key, so it needs no extra data source.
FORMATS = {"standard", "modern", "pioneer", "pauper", "legacy", "vintage", "premodern"}

_COLOR_WORDS = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}

DECK_SIZE = 60
_MAX_COPIES = 4  # 4-of rule; basic lands are handled separately and unlimited

# Canonical basic-land names by lowercase key, to reroute any basic the model
# put in main_deck into the basic-land pile (where counts are unlimited).
_BASIC_BY_KEY = {n.lower(): n for n in poolbuild._BASIC_NAMES}

_norm = scryfall.norm_name


def _query(fmt: str, intent: dict) -> str:
    parts = ["MTG", fmt]
    parts += intent.get("keywords") or []
    parts += [_COLOR_WORDS[c] for c in (intent.get("colors") or []) if c in _COLOR_WORDS]
    parts += ["deck", "decklist", "metagame", "2026"]
    return " ".join(parts)


def _deck_entries(archetype: dict) -> tuple[list[tuple[str, int]], dict[str, int]]:
    """Normalise the model's output into ((name, count)…, basic-land weights).

    Tolerates the pre-manabase output shape (bare ``key_cards`` names) so a
    degraded LLM answer still yields a deck instead of an empty page.
    """
    basics: dict[str, int] = {}
    raw_basics = archetype.get("basic_lands")
    if isinstance(raw_basics, dict):
        for name, count in raw_basics.items():
            canonical = _BASIC_BY_KEY.get(str(name).strip().lower())
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if canonical and count > 0:
                basics[canonical] = basics.get(canonical, 0) + count

    main = archetype.get("main_deck")
    if not isinstance(main, list):
        main = [{"name": n, "count": 1} for n in (archetype.get("key_cards") or [])]

    entries: list[tuple[str, int]] = []
    for item in main:
        if isinstance(item, str):
            name, count = item, 1
        elif isinstance(item, dict):
            name = item.get("name")
            try:
                count = int(item.get("count", 1))
            except (TypeError, ValueError):
                count = 1
        else:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        canonical = _BASIC_BY_KEY.get(name.lower())
        if canonical:
            basics[canonical] = basics.get(canonical, 0) + max(1, count)
        else:
            entries.append((name, max(1, count)))
    return entries, basics


def _decklist_text(groups: list[dict], lands: dict) -> str:
    """Plain "<qty> <name>" lines — the format Moxfield/Archidekt/ManaBox import."""
    lines = [f"{c['qty']} {c['name']}" for g in groups for c in g["cards"]]
    lines += [f"{c['qty']} {c['name']}" for c in lands["nonbasic"]]
    lines += [f"{c['qty']} {c['name']}" for c in lands["basics"]]
    return "\n".join(lines)


def analyze(intent: dict, profile_id: int) -> dict:
    fmt = intent.get("format")
    results = research.brave_search(_query(fmt, intent))
    context = research.context_block(results)

    archetype = llm.archetype_research(fmt, intent, context)
    if not archetype:
        return {"format": fmt, "llm_unavailable": True, "sources": results}

    entries, basic_weights = _deck_entries(archetype)
    colors = [c for c in (archetype.get("colors") or []) if c in _COLOR_WORDS]
    # Only copies not already sleeved in another of the user's decks count as
    # available; the rest is surfaced separately (in_deck_qty) so the user
    # knows buying replaces borrowing.
    quantities = db.owned_quantities(profile_id)
    owned_qty = {k: max(0, qty - deck_qty) for k, (qty, deck_qty) in quantities.items()}
    in_deck_qty = {k: deck_qty for k, (_qty, deck_qty) in quantities.items()}
    budget = intent.get("budget_eur")

    with httpx.Client(timeout=30, headers={"User-Agent": settings.user_agent}) as client:
        names = [n for n, _ in entries]
        resolved, _nf = scryfall.resolve_cards(names, client=client) if names else ({}, [])

        # Validate + merge duplicates, clamping to the 4-of rule.
        merged: dict[str, dict] = {}
        order: list[str] = []
        dropped: list[str] = []
        for name, count in entries:
            card = resolved.get(_norm(name))
            if not card or not scryfall.legal_in(card, fmt):
                dropped.append(name)
                continue
            key = _norm(card["name"])
            if key in merged:
                merged[key]["count"] = min(_MAX_COPIES, merged[key]["count"] + count)
            else:
                merged[key] = {"name": card["name"], "card": card,
                               "count": min(_MAX_COPIES, count)}
                order.append(key)

        chosen = [merged[k] for k in order]
        spells = [c for c in chosen if not poolbuild._is_land(c["card"])]
        nonbasic_lands = [c for c in chosen if poolbuild._is_land(c["card"])]

        # Top up to exactly DECK_SIZE with basic lands (dropping invalid cards
        # must not leave a short deck); trim low-priority spells if the model
        # overshot despite the prompt.
        nonbasic_total = sum(c["count"] for c in spells) + sum(c["count"] for c in nonbasic_lands)
        need = DECK_SIZE - nonbasic_total
        if need < 0:
            spells = poolbuild._trim(spells, -need)
            need = 0
        basics = poolbuild._distribute_basics(colors, need, basic_weights or None)

        def deck_item(entry: dict) -> dict:
            card, count = entry["card"], entry["count"]
            key = _norm(entry["name"])
            have = owned_qty.get(key, 0)
            owned = min(count, have)
            return {
                "name": entry["name"],
                "image": scryfall.image(card),
                "qty": count,
                "owned_qty": owned,
                "missing_qty": count - owned,
                # Missing copies the user does own — but locked in other decks.
                "in_deck_qty": min(count - owned, in_deck_qty.get(key, 0)),
                "owned": owned >= count,
                "price_eur": scryfall.price_eur(card),
                "cmc": card.get("cmc"),
            }

        by_cat: dict[str, list] = {}
        for c in spells:
            by_cat.setdefault(poolbuild._category(c["card"]), []).append(deck_item(c))
        groups = [
            {"label": CATEGORY_LABELS[cat], "cards": by_cat[cat]}
            for cat in CATEGORY_ORDER if by_cat.get(cat)
        ]
        nonbasic_items = [deck_item(c) for c in nonbasic_lands]
        # Basic lands are treated as freely available (same convention as the
        # Commander generator): they cost pennies and everyone has piles.
        basic_items = [
            {"name": n, "qty": q, "owned_qty": q, "missing_qty": 0, "owned": True,
             "is_basic": True}
            for n, q in basics
        ]

        all_items = [c for g in groups for c in g["cards"]] + nonbasic_items
        missing = [(c["name"], c["missing_qty"]) for c in all_items if c["missing_qty"] > 0]
        buy = buylist.build(missing, budget,
                            max_card_price=intent.get("max_card_price_eur"), client=client)

    basics_total = sum(q for _, q in basics)
    lands_total = sum(c["qty"] for c in nonbasic_items) + basics_total
    spells_total = sum(c["qty"] for g in groups for c in g["cards"])
    owned_copies = sum(c["owned_qty"] for c in all_items) + basics_total
    total = spells_total + lands_total
    lands = {"nonbasic": nonbasic_items, "basics": basic_items, "total": lands_total}

    deck = {
        "groups": groups,
        "lands": lands,
        "counts": {
            "total": total,
            "spells": spells_total,
            "lands": lands_total,
            "owned": owned_copies,
            "to_buy": sum(c["missing_qty"] for c in all_items),
        },
        "deck_size": DECK_SIZE,
        "decklist_text": _decklist_text(groups, lands),
    }

    return {
        "format": fmt,
        "archetype": {
            "name": archetype.get("archetype") or "Archétype",
            "colors": colors,
            "strategy": (archetype.get("strategy") or "").strip(),
        },
        "deck": deck,
        "buylist": buy,
        # Completion counters (copies owned / deck size) — also read by the
        # chat artifact templates, which fall back to the pre-deck shape when
        # "deck" is absent (old persisted artifacts).
        "valid_count": total,
        "owned_count": owned_copies,
        "missing_count": deck["counts"]["to_buy"],
        "dropped": dropped,
        "sources": results[:6],
        "budget_eur": budget,
        "llm_unavailable": False,
    }
