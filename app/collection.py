"""Collection enrichment: attach locally-cached Scryfall data (image, price,
type, color identity) to a profile's owned cards.

Reads only from the local card cache (``db.get_cards``) — never hits the
network — so this stays fast even for large collections and works offline.
Cards Scryfall hasn't resolved yet (e.g. just imported, before the background
bulk-data refresh has caught up) simply show up with no image/price.
"""
import json

from . import db, scryfall

STATS_META_PREFIX = "collection_stats:"

# name_key -> small text-view dict, shared across requests. Card text almost
# never changes, and one chat page can render hundreds of card views — without
# this every one of them would be a SQLite query + a ~5 KB JSON parse. Cleared
# after a bulk-data import (app/bulk_data.py) so refreshed oracle text lands.
_TEXT_CACHE_MAX = 20000
_text_cache: dict[str, dict] = {}


def enrich(profile_id: int) -> list[dict]:
    """Owned cards for ``profile_id``, each with image/price/type/colors."""
    names = db.collection_names(profile_id)
    cards = db.get_cards(name_key for _, name_key, _ in names)
    out = []
    for raw_name, name_key, qty in names:
        card = cards.get(name_key)
        price = scryfall.price_eur(card) if card else None
        out.append({
            "name": raw_name,
            "name_key": name_key,
            "qty": qty,
            "image": scryfall.image(card) if card else None,
            "image_small": scryfall.image_small(card) if card else None,
            "price_eur": price,
            "line_total": round(price * qty, 2) if price is not None else None,
            "type_line": (card.get("type_line") if card else "") or "",
            "mana_cost": scryfall.mana_cost(card) if card else "",
            "oracle_text": scryfall.oracle_text(card) if card else "",
            "power_toughness": scryfall.power_toughness(card) if card else "",
            "colors": scryfall.color_identity(card) if card else [],
        })
    return out


def card_text_info(name: str) -> dict | None:
    """Text-view fields for one card name, from the local cache (memoized).

    Used by the ``_cardview.html`` macro for screens where the caller only has
    a name + image URL (chat artifacts, results, deck pages). Misses are not
    memoized so a card resolved later (bulk refresh) shows up without waiting
    for a cache clear.
    """
    key = (name or "").strip().lower()
    info = _text_cache.get(key)
    if info is None:
        card = db.get_card(key)
        if card is None:
            return None
        info = {
            "mana_cost": scryfall.mana_cost(card),
            "type_line": card.get("type_line") or "",
            "oracle_text": scryfall.oracle_text(card),
            "power_toughness": scryfall.power_toughness(card),
        }
        if len(_text_cache) < _TEXT_CACHE_MAX:
            _text_cache[key] = info
    return info


def clear_text_cache() -> None:
    _text_cache.clear()


def total_value_eur(rows: list[dict]) -> float:
    return round(sum(r["line_total"] or 0 for r in rows), 2)


def color_breakdown(rows: list[dict]) -> dict[str, int]:
    """Card count (by quantity) per WUBRG color, plus 'C' for colorless."""
    counts = {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0}
    for r in rows:
        colors = r["colors"]
        if not colors:
            counts["C"] += r["qty"]
        else:
            for c in colors:
                if c in counts:
                    counts[c] += r["qty"]
    return counts


def stats(profile_id: int) -> dict:
    """Home-page stats (counts, total value, color breakdown), cached in meta.

    Enriching the whole collection just to show three numbers on ``/`` was the
    single slowest thing the app did on every visit. The cached entry is
    dropped on collection import (``db.replace_collection``) and after a
    bulk-data refresh (prices move), then lazily recomputed here.
    """
    key = f"{STATS_META_PREFIX}{int(profile_id)}"
    raw = db.get_meta(key)
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    rows = enrich(profile_id)
    result = {
        "distinct": len(rows),
        "total": sum(r["qty"] for r in rows),
        "total_value": total_value_eur(rows),
        "color_breakdown": color_breakdown(rows),
    }
    db.set_meta(key, json.dumps(result))
    return result
