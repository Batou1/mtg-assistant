"""Collection enrichment: attach locally-cached Scryfall data (image, price,
type, color identity) to a profile's owned cards, and search it.

Reads only from the local card cache (``db.get_cards``) — never hits the
network — so this stays fast even for large collections and works offline.
Cards Scryfall hasn't resolved yet (e.g. just imported, before the background
bulk-data refresh has caught up) simply show up with no image/price.

Searching goes through ``app.scryquery`` (Scryfall's own syntax). The
structured filter controls of the collection page are *compiled* into that
same syntax by ``build_query`` rather than filtering through a second code
path, so the panel and the raw query box can never disagree.
"""
import json

from . import db, scryfall, scryquery

STATS_META_PREFIX = "collection_stats:"
PRICES_META_PREFIX = "collection_prices:"

# name_key -> small text-view dict, shared across requests. Card text almost
# never changes, and one chat page can render hundreds of card views — without
# this every one of them would be a SQLite query + a ~5 KB JSON parse. Cleared
# after a bulk-data import (app/bulk_data.py) so refreshed oracle text lands.
_TEXT_CACHE_MAX = 20000
_text_cache: dict[str, dict] = {}


def _row(raw_name: str, name_key: str, qty: int, deck_qty: int, card: dict | None,
         price: float | None = None, line_total: float | None = None,
         set_code: str = "") -> dict:
    return {
        "name": raw_name,
        "name_key": name_key,
        "qty": qty,
        "deck_qty": deck_qty,
        "image": scryfall.image(card) if card else None,
        "image_small": scryfall.image_small(card) if card else None,
        "price_eur": price,
        "line_total": line_total,
        "type_line": (card.get("type_line") if card else "") or "",
        "mana_cost": scryfall.mana_cost(card) if card else "",
        "oracle_text": scryfall.oracle_text(card) if card else "",
        "power_toughness": scryfall.power_toughness(card) if card else "",
        "colors": scryfall.color_identity(card) if card else [],
        "rarity": (card.get("rarity") if card else "") or "",
        # The edition owned, not Scryfall's canonical one: it is the edition
        # the displayed price is the price of.
        "set_code": set_code or ((card.get("set") if card else "") or "").upper(),
        "set_name": (card.get("set_name") if card else "") or "",
        "cmc": (card.get("cmc") if card else None),
        "released_at": (card.get("released_at") if card else "") or "",
    }


def _compute_owned_prices(profile_id: int) -> dict[str, list]:
    """``{name_key: [unit_eur, line_eur, set_code]}`` from the OWNED printings.

    A card in a collection has an edition, so it is worth what that edition is
    worth — and the spread is not a rounding detail: Chainer, Dementia Master
    is 44 € in Torment and 0.24 € in the printing Scryfall calls canonical.
    Foil copies are priced as foil, being a different (dearer) product.

    ``line_eur`` sums each printing's own price times its copies, so a name
    owned in two editions is valued honestly; ``unit_eur`` is the resulting
    average, which in the overwhelmingly common single-printing case is just
    that printing's price. ``set_code`` is the most-owned printing's, so the
    edition shown next to a price is the edition that price refers to.
    """
    rows = db.collection_printings(profile_id)
    by_id = db.printing_prices(
        f"id:{r['scryfall_id']}" for r in rows if r["scryfall_id"]
    )
    # Three ways an owned printing can fail to answer: no Scryfall id in the
    # import, an id the bulk cache doesn't have yet, and — the common one, a
    # foreign-language or promo-only printing — an id whose Cardmarket price is
    # simply null. All three fall back to the canonical by-name entry: an
    # approximate price beats reporting a card the user owns as worthless.
    by_name = db.printing_prices({r["name_key"] for r in rows})

    totals: dict[str, list] = {}
    for r in rows:
        foil = bool(r["foil"])
        source = by_id.get(f"id:{r['scryfall_id']}") if r["scryfall_id"] else None
        unit = scryfall.price_eur(source, foil=foil) if source else None
        if unit is None:
            source = by_name.get(r["name_key"])
            unit = scryfall.price_eur(source, foil=foil) if source else None
        entry = totals.setdefault(r["name_key"], [0.0, 0, 0, ""])
        if unit is not None:
            entry[0] += unit * r["qty"]
            entry[1] += r["qty"]
        if r["qty"] > entry[2]:
            entry[2] = r["qty"]
            entry[3] = (r["set_code"] or "").upper()

    return {
        key: [round(total / priced, 2), round(total, 2), set_code] if priced
        else [None, None, set_code]
        for key, (total, priced, _qty, set_code) in totals.items()
    }


def owned_prices(profile_id: int) -> dict[str, list]:
    """``_compute_owned_prices`` memoized in ``meta``.

    Pricing the owned printings means touching one ``id:<uuid>`` cache row per
    printing, scattered across a multi-GB table — about a second on a large
    collection, paid again on every filtered view. So it is cached like the
    home-page stats, and dropped by the same two events: a collection import
    (``db.replace_collection``) and a bulk-data refresh, which is precisely
    when prices move.
    """
    key = f"{PRICES_META_PREFIX}{int(profile_id)}"
    raw = db.get_meta(key)
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    result = _compute_owned_prices(profile_id)
    db.set_meta(key, json.dumps(result))
    return result


def _owned(profile_id: int):
    """Yield ``(raw_name, name_key, qty, deck_qty, card, unit, line, set_code)``
    per owned card.

    ``card`` is Scryfall's canonical entry for the name (text, image, colours —
    all printing-independent), while the price and the edition come from the
    printings actually owned (``owned_prices``).
    """
    names = db.collection_names(profile_id)
    cards = db.get_cards(name_key for _, name_key, _ in names)
    quantities = db.owned_quantities(profile_id)
    priced = owned_prices(profile_id)
    for raw_name, name_key, qty in names:
        deck_qty = quantities.get(name_key, (qty, 0))[1] or 0
        unit, line, set_code = priced.get(name_key) or (None, None, "")
        yield raw_name, name_key, qty, deck_qty, cards.get(name_key), unit, line, set_code


def enrich(profile_id: int) -> list[dict]:
    """Owned cards for ``profile_id``, each with image/price/type/colors."""
    return [_row(*owned) for owned in _owned(profile_id)]


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


# --- Search --------------------------------------------------------------

_RARITY_RANK = {"common": 0, "uncommon": 1, "rare": 2, "special": 3,
                "mythic": 4, "bonus": 5}
_COLOR_RANK = {"W": 1, "U": 2, "B": 3, "R": 4, "G": 5}


def _color_sort_key(row: dict):
    """Colourless first, then mono-colour in WUBRG order, then multicolour."""
    colors = row["colors"]
    if not colors:
        return (0, 0)
    if len(colors) == 1:
        return (1, _COLOR_RANK.get(colors[0], 9))
    return (2, len(colors))


#: Sort keys offered by the collection page. ``desc`` is the direction that
#: reads as "most interesting first" for that key, used when none is asked for.
SORTS: dict[str, dict] = {
    "qty": {"label": "Quantité", "desc": True, "key": lambda r: r["qty"]},
    "value": {"label": "Valeur", "desc": True, "key": lambda r: r["line_total"] or 0.0},
    "price": {"label": "Prix", "desc": True, "key": lambda r: r["price_eur"] or 0.0},
    "name": {"label": "Nom", "desc": False, "key": lambda r: r["name"].lower()},
    "mv": {"label": "Coût de mana", "desc": False,
           "key": lambda r: r["cmc"] if r["cmc"] is not None else 99.0},
    "rarity": {"label": "Rareté", "desc": True,
               "key": lambda r: _RARITY_RANK.get(r["rarity"], -1)},
    "color": {"label": "Couleur", "desc": False, "key": _color_sort_key},
    "set": {"label": "Édition", "desc": False, "key": lambda r: r["set_code"]},
    "released": {"label": "Sortie", "desc": True, "key": lambda r: r["released_at"]},
}

# Scryfall's own `order:` vocabulary, so a pasted query's sort still applies.
_ORDER_ALIASES = {"cmc": "mv", "manavalue": "mv", "eur": "price", "usd": "price",
                  "edhrec": "qty", "artist": "name", "released": "released",
                  "spoiled": "released"}


def resolve_sort(requested: str | None, query_order: str | None = None) -> str:
    """Pick a sort key: the toolbar's choice wins, else an inline ``order:``."""
    for candidate in (requested, query_order):
        if not candidate:
            continue
        key = _ORDER_ALIASES.get(candidate.lower(), candidate.lower())
        if key in SORTS:
            return key
    return "qty"


def sort_rows(rows: list[dict], sort: str, direction: str | None = None) -> list[dict]:
    """Sort ``rows`` in place by one of ``SORTS``, name as tiebreaker.

    The name pre-pass relies on Python's stable sort: it keeps ties in
    alphabetical order even when the main key is reversed, which a compound
    ``(key, name)`` tuple would not.
    """
    spec = SORTS.get(sort) or SORTS["qty"]
    reverse = spec["desc"] if direction not in ("asc", "desc") else direction == "desc"
    rows.sort(key=lambda r: r["name"].lower())
    if spec is not SORTS["name"]:
        rows.sort(key=spec["key"], reverse=reverse)
    elif reverse:
        rows.reverse()
    return rows


def search(profile_id: int, query: scryquery.Query,
           sort: str = "qty", direction: str | None = None) -> list[dict]:
    """Owned cards matching ``query``, sorted.

    Cards the local cache hasn't resolved yet carry no Scryfall data at all;
    they are matched on their name only (and found by ``is:unresolved``) so
    they never silently vanish from the collection view.

    Matching happens before the display row is built: on a 10k-card collection
    that skips thousands of image/oracle-text lookups per filtered view.
    """
    out = []
    for owned in _owned(profile_id):
        raw_name, name_key, qty, deck_qty, card, price, line_total, set_code = owned
        extra = {
            "qty": qty,
            "deck_qty": deck_qty,
            # The owned printing's price and edition: the query box must filter
            # on exactly what the page displays (see app/scryquery.py).
            "unit_price": price,
            "set_code": set_code,
            "line_total": line_total or 0.0,
            "resolved": card is not None,
        }
        if query.match(card if card is not None else {"name": raw_name}, extra):
            out.append(_row(*owned))
    return sort_rows(out, sort, direction)


# --- Structured filter panel ---------------------------------------------

#: Query-string parameters the filter panel round-trips (``colors`` is a list).
FILTER_FIELDS = (
    "q", "name", "oracle", "type", "subtype", "rarity", "kw", "set", "format",
    "mana", "mv_min", "mv_max", "pow_min", "pow_max", "tou_min", "tou_max",
    "price_min", "price_max", "qty_min", "copies", "color_mode",
)

#: Mana symbols offered as click-to-insert chips under the "Symboles de mana"
#: field: (symbol, WUBRG class for the swatch or "" for a neutral chip, title).
MANA_SYMBOLS = (
    ("{W}", "W", "Blanc"),
    ("{U}", "U", "Bleu"),
    ("{B}", "B", "Noir"),
    ("{R}", "R", "Rouge"),
    ("{G}", "G", "Vert"),
    ("{C}", "", "Mana incolore"),
    ("{X}", "", "Coût variable X"),
    ("{1}", "", "1 mana générique"),
    ("{2}", "", "2 manas génériques"),
    ("{3}", "", "3 manas génériques"),
    ("{W/U}", "", "Hybride (blanc ou bleu)"),
    ("{2/W}", "", "Mono-hybride (2 génériques ou blanc)"),
    ("{W/P}", "", "Phyrexian (blanc ou 2 points de vie)"),
)

#: value -> (label, query fragment) for the "copies" selector, which exists
#: because copies already sleeved in a deck are not copies you can build with
#: (see db.owned_quantities).
COPIES_CHOICES = {
    "": "Toutes les copies",
    "spare": "Copies disponibles",
    "indeck": "Copies en deck",
}

TYPE_CHOICES = {
    "": "Type · Tous",
    "creature": "Créatures",
    "instant": "Éphémères",
    "sorcery": "Rituels",
    "artifact": "Artefacts",
    "enchantment": "Enchantements",
    "planeswalker": "Planeswalkers",
    "land": "Terrains",
    "battle": "Batailles",
    "legendary": "Légendaires",
}

RARITY_CHOICES = {
    "": "Rareté · Toutes",
    "common": "Commune",
    "uncommon": "Peu commune",
    "rare": "Rare",
    "mythic": "Mythique",
}

COLOR_MODE_CHOICES = {
    "identity": "Identité ⊆ (jouable avec)",
    "contains": "Couleurs ⊇ (contient)",
    "exact": "Couleurs = (exactement)",
}

_COLOR_MODE_OPS = {"identity": ("id", "<="), "contains": ("c", ">="), "exact": ("c", "=")}

_RANGE_FIELDS = (
    ("mv_min", "mv", ">="), ("mv_max", "mv", "<="),
    ("pow_min", "pow", ">="), ("pow_max", "pow", "<="),
    ("tou_min", "tou", ">="), ("tou_max", "tou", "<="),
    ("price_min", "eur", ">="), ("price_max", "eur", "<="),
    ("qty_min", "qty", ">="),
)


def _clean(params, key: str) -> str:
    value = params.get(key) or ""
    return value.strip() if isinstance(value, str) else ""


def panel_query(params) -> str:
    """Compile the filter panel's own fields into query terms (no ``q`` box).

    Returns "" when the panel is empty, which is how the caller tells "the user
    asked something with the panel" from "the panel had nothing to say".
    """
    parts: list[str] = []
    quote = scryquery.quote

    name = _clean(params, "name")
    if name:
        parts.append(f"name:{quote(name)}")
    oracle = _clean(params, "oracle")
    if oracle:
        parts.append(f"o:{quote(oracle)}")
    for field in ("type", "subtype"):
        value = _clean(params, field)
        if value:
            parts.append(f"t:{quote(value)}")
    rarity = _clean(params, "rarity")
    if rarity:
        parts.append(f"r:{quote(rarity)}")
    kw = _clean(params, "kw")
    if kw:
        parts.append(f"kw:{quote(kw)}")
    # Spaces between symbols are how people type a cost ("{2} {R}"); the query
    # syntax has no room for them, and scryquery reads "2R" as {2}{R} anyway.
    mana = _clean(params, "mana").replace(" ", "")
    if mana:
        parts.append(f"m:{mana}")
    set_code = _clean(params, "set")
    if set_code:
        parts.append(f"s:{quote(set_code)}")
    fmt = _clean(params, "format")
    if fmt:
        parts.append(f"f:{quote(fmt)}")

    for field, key, op in _RANGE_FIELDS:
        value = _clean(params, field).replace(",", ".")
        if value:
            parts.append(f"{key}{op}{value}")

    colors = [c for c in (params.getlist("colors") if hasattr(params, "getlist")
                          else params.get("colors") or []) if c in _COLOR_RANK]
    if colors:
        key, op = _COLOR_MODE_OPS.get(_clean(params, "color_mode") or "identity",
                                      _COLOR_MODE_OPS["identity"])
        parts.append(f"{key}{op}{''.join(colors).lower()}")

    copies = _clean(params, "copies")
    if copies in ("spare", "indeck"):
        parts.append(f"is:{copies}")

    return " ".join(parts)


def combine(raw: str, terms: str) -> str:
    """AND an existing query with extra terms, keeping ``raw``'s meaning intact.

    ``raw`` is parenthesised only when it has a top-level ``or``, which would
    otherwise bind just its last branch to the added terms; a plain conjunction
    is appended to as-is so a query refined filter by filter stays readable.
    """
    raw, terms = (raw or "").strip(), (terms or "").strip()
    if not (raw and terms):
        return raw or terms
    return f"({raw}) {terms}" if scryquery.needs_grouping(raw) else f"{raw} {terms}"


def build_query(params, q: str | None = None) -> str:
    """The panel's terms ANDed with the query box (or with ``q`` when given)."""
    raw = _clean(params, "q") if q is None else q
    return combine(raw, panel_query(params))


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
