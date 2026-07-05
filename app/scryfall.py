"""Scryfall client: resolve card names / ids to canonical card data, cached.

Uses the official /cards/collection batch endpoint (max 75 identifiers per
request) and caches every result in SQLite. Double-faced cards are registered
under both their full "Front // Back" name and their front-face name so that
either form in a decklist resolves. Cards are also cached under "id:<uuid>" so
ManaBox rows (which carry a Scryfall id) resolve to an exact printing.
"""
import time

import httpx

from . import db
from .config import settings

# Layouts that aren't real spells/permanents — Art Series cards, tokens,
# emblems, etc. They routinely reuse a real card's display name (e.g. an Art
# Series "Sol Ring // Sol Ring") without being that card, so both the bulk
# import (app/bulk_data.py) and the local card search (app/cardsearch.py)
# exclude them.
NON_GAME_LAYOUTS = {
    "art_series", "token", "double_faced_token", "emblem",
    "scheme", "vanguard", "planar", "augment", "host",
}


def _key(name: str) -> str:
    return name.strip().lower()


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _register(card: dict, results: dict) -> None:
    full = _key(card["name"])
    db.set_card(full, card)
    results[full] = card
    if "//" in card["name"]:
        front = _key(card["name"].split("//")[0])
        db.set_card(front, card)
        results.setdefault(front, card)
    if card.get("id"):
        db.set_card(f"id:{card['id']}", card)


def _client(client: httpx.Client | None):
    if client is not None:
        return client, False
    return httpx.Client(timeout=30, headers={"User-Agent": settings.user_agent}), True


def _post_collection(conn: httpx.Client, identifiers: list[dict]) -> dict | None:
    """POST a batch to /cards/collection, returning the JSON payload.

    Returns None on a 4xx (e.g. a malformed identifier from messy import data)
    so a single bad batch is skipped rather than aborting the whole analysis.
    Server (5xx) / network errors still propagate.
    """
    resp = conn.post(
        f"{settings.scryfall_api}/cards/collection",
        json={"identifiers": identifiers},
    )
    if resp.status_code >= 400 and resp.status_code < 500:
        return None
    resp.raise_for_status()
    return resp.json()


def resolve_cards(names, client: httpx.Client | None = None):
    """Resolve ``names`` to card dicts.

    Returns (results, not_found) where results maps a lowercased name to the
    Scryfall card dict and not_found is the list of names Scryfall did not match.
    """
    results: dict[str, dict] = {}
    missing: list[str] = []

    for name in names:
        cached = db.get_card(_key(name))
        if cached is not None:
            results[_key(name)] = cached
        else:
            missing.append(name)

    not_found: list[str] = []
    if not missing:
        return results, not_found

    conn, owns = _client(client)
    try:
        for batch in _chunks(missing, 75):
            identifiers = [{"name": n} for n in batch]
            payload = _post_collection(conn, identifiers)
            if payload is None:
                # Malformed/empty batch (e.g. messy import data): don't let one
                # bad batch sink the whole analysis — treat it as not found.
                not_found.extend(batch)
                continue
            for card in payload.get("data", []):
                _register(card, results)
            for nf in payload.get("not_found", []):
                not_found.append(nf.get("name", str(nf)) if isinstance(nf, dict) else str(nf))
            time.sleep(settings.request_delay)
    finally:
        if owns:
            conn.close()

    return results, not_found


def resolve_ids(ids, client: httpx.Client | None = None):
    """Resolve Scryfall ids to card dicts. Returns {id: card}."""
    results: dict[str, dict] = {}
    missing: list[str] = []
    for cid in ids:
        cached = db.get_card(f"id:{cid}")
        if cached is not None:
            results[cid] = cached
        else:
            missing.append(cid)

    if not missing:
        return results

    conn, owns = _client(client)
    try:
        for batch in _chunks(missing, 75):
            payload = _post_collection(conn, [{"id": c} for c in batch])
            if payload is None:
                continue
            for card in payload.get("data", []):
                tmp: dict = {}
                _register(card, tmp)
                if card.get("id"):
                    results[card["id"]] = card
            time.sleep(settings.request_delay)
    finally:
        if owns:
            conn.close()

    return results


# --- Convenience accessors on a Scryfall card dict -----------------------

def _image_uris(card: dict) -> dict | None:
    uris = card.get("image_uris")
    if uris:
        return uris
    faces = card.get("card_faces") or []
    if faces and faces[0].get("image_uris"):
        return faces[0]["image_uris"]
    return None


def image(card: dict) -> str | None:
    uris = _image_uris(card)
    if uris:
        return uris.get("normal") or uris.get("large") or uris.get("small")
    return None


def image_small(card: dict) -> str | None:
    """Thumbnail-sized (~146px) image, for dense grids; falls back to normal."""
    uris = _image_uris(card)
    if uris:
        return uris.get("small") or uris.get("normal")
    return None


def price_eur(card: dict) -> float | None:
    """Cardmarket EUR price (non-foil preferred, else foil)."""
    prices = card.get("prices") or {}
    for field in ("eur", "eur_foil"):
        val = prices.get(field)
        if val:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def mana_cost(card: dict) -> str:
    """Mana cost string, e.g. '{2}{R}'. For double-faced cards, both faces'
    costs joined with ' // ' (a face with no cost, e.g. a back land face, is
    skipped).
    """
    cost = card.get("mana_cost")
    if cost:
        return cost
    faces = card.get("card_faces") or []
    costs = [f["mana_cost"] for f in faces if f.get("mana_cost")]
    return " // ".join(costs)


def power_toughness(card: dict) -> str:
    """'P/T' for creatures, e.g. '8/8'. For double-faced cards where more than
    one face is a creature, both joined with ' // '. Empty for non-creatures.
    """
    if "power" in card and "toughness" in card:
        return f"{card['power']}/{card['toughness']}"
    faces = card.get("card_faces") or []
    pts = [
        f"{f['power']}/{f['toughness']}" for f in faces
        if "power" in f and "toughness" in f
    ]
    return " // ".join(pts)


def oracle_text(card: dict) -> str:
    """Rules text. For double-faced/split cards, each face's name, type and
    text are stacked so both halves are readable.
    """
    text = card.get("oracle_text")
    if text:
        return text
    faces = card.get("card_faces") or []
    if not faces:
        return ""
    parts = []
    for face in faces:
        header = " ".join(
            p for p in (face.get("name"), face.get("mana_cost")) if p
        )
        body = "\n".join(
            p for p in (header, face.get("type_line"), face.get("oracle_text")) if p
        )
        parts.append(body)
    return "\n\n".join(parts)


def legal_in(card: dict, fmt: str) -> bool:
    """True if the card is legal (or restricted) in ``fmt``."""
    status = (card.get("legalities") or {}).get(fmt.lower())
    return status in ("legal", "restricted")


def color_identity(card: dict) -> list[str]:
    return card.get("color_identity", []) or []
