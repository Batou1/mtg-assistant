"""Collection enrichment: attach locally-cached Scryfall data (image, price,
type, color identity) to a profile's owned cards.

Reads only from the local card cache (``db.get_card``) — never hits the
network — so this stays fast even for large collections and works offline.
Cards Scryfall hasn't resolved yet (e.g. just imported, before the background
bulk-data refresh has caught up) simply show up with no image/price.
"""
from . import db, scryfall


def enrich(profile_id: int) -> list[dict]:
    """Owned cards for ``profile_id``, each with image/price/type/colors."""
    out = []
    for raw_name, name_key, qty in db.collection_names(profile_id):
        card = db.get_card(name_key)
        price = scryfall.price_eur(card) if card else None
        out.append({
            "name": raw_name,
            "name_key": name_key,
            "qty": qty,
            "image": scryfall.image(card) if card else None,
            "price_eur": price,
            "line_total": round(price * qty, 2) if price is not None else None,
            "type_line": (card.get("type_line") if card else "") or "",
            "mana_cost": scryfall.mana_cost(card) if card else "",
            "oracle_text": scryfall.oracle_text(card) if card else "",
            "colors": scryfall.color_identity(card) if card else [],
        })
    return out


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
