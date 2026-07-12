"""Build a full 100-card Commander decklist from an EDHREC commander page.

Deterministic: the deck is assembled from EDHREC's most-included cards for the
commander. Freely available owned cards are added first. Missing cards are
bought within budget — ranked by popularity-per-euro so the deck fills out with
cheap staples first and stays *complete* (without a budget, missing cards are
ranked by raw popularity instead). Owned cards whose every copy already sits in
another of the user's decks (ManaBox "Binder Type" = deck) are a last resort:
they are only used to fill slots the budget couldn't, and flagged so the user
knows the card must be pulled from an existing deck. A floor of the land slots
(``min_basics``) is reserved for basic lands — EDHREC land sections would
otherwise fill the whole manabase with nonbasics — then lands are topped up
with basics matching the colour identity.

The function takes ``cards_by_name`` (a name->Scryfall-card map the caller has
already resolved) instead of doing network I/O itself, which keeps it unit
testable and lets the caller batch/caches Scryfall calls.
"""
from . import commanders, scryfall

# EDHREC section tags grouped by role.
_LAND_TAGS = {"lands", "utilitylands"}
_TYPE_TAGS = {
    "creatures", "instants", "sorceries", "enchantments",
    "utilityartifacts", "manaartifacts", "planeswalkers", "battles",
}

# Display order + French labels for the deck's categories.
CATEGORY_ORDER = [
    "creatures", "manaartifacts", "utilityartifacts",
    "instants", "sorceries", "enchantments", "planeswalkers", "battles", "autres",
]
CATEGORY_LABELS = {
    "creatures": "Créatures",
    "manaartifacts": "Artefacts de mana",
    "utilityartifacts": "Artefacts utilitaires",
    "instants": "Éphémères",
    "sorceries": "Rituels",
    "enchantments": "Enchantements",
    "planeswalkers": "Planeswalkers",
    "battles": "Batailles",
    "autres": "Autres",
    "lands": "Terrains",
}

_BASICS = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}
BASIC_NAMES = list(_BASICS.values()) + ["Wastes"]


_norm = scryfall.norm_name


def _by_name(item: dict) -> str:
    """Sort key: cards are displayed alphabetically within each type group."""
    return item["name"].lower()


def _pool(data: dict, commander_name: str) -> dict:
    """name_key -> {name, inclusion, is_land, category} across all EDHREC sections."""
    jd = (data.get("container", {}) or {}).get("json_dict", {}) or {}
    cmd = _norm(commander_name)
    pool: dict[str, dict] = {}
    for section in jd.get("cardlists") or []:
        tag = section.get("tag")
        is_land = tag in _LAND_TAGS
        cat = tag if tag in _TYPE_TAGS else None
        for cv in section.get("cardviews") or []:
            name = cv.get("name")
            if not name:
                continue
            key = _norm(name)
            if key == cmd:
                continue
            inclusion = cv.get("inclusion") or cv.get("num_decks") or 0
            entry = pool.get(key)
            if entry is None:
                pool[key] = {
                    "name": name,
                    "inclusion": inclusion,
                    "is_land": is_land,
                    "category": cat,
                }
            else:
                entry["inclusion"] = max(entry["inclusion"], inclusion)
                if is_land:
                    entry["is_land"] = True
                if cat and not entry["category"]:
                    entry["category"] = cat
    return pool


def _sorted(pool: dict, *, lands: bool) -> list[dict]:
    items = [e for e in pool.values() if e["is_land"] is lands]
    return sorted(items, key=lambda e: e["inclusion"], reverse=True)


def candidate_names(data: dict, commander_name: str,
                    nonland_cap: int = 130, land_cap: int = 50):
    """Names the caller should resolve via Scryfall before calling build_deck."""
    pool = _pool(data, commander_name)
    nonlands = [e["name"] for e in _sorted(pool, lands=False)][:nonland_cap]
    lands = [e["name"] for e in _sorted(pool, lands=True)][:land_cap]
    return nonlands, lands


def _category_for_card(card: dict) -> str | None:
    """EDHREC-style category for a card forced into the deck from outside the
    EDHREC pool (player-requested include), derived from its Scryfall type line."""
    type_line = card.get("type_line") or ""
    if not type_line:
        faces = card.get("card_faces") or []
        type_line = " // ".join(f.get("type_line", "") for f in faces)
    t = type_line.lower()
    if "creature" in t:
        return "creatures"
    if "planeswalker" in t:
        return "planeswalkers"
    if "battle" in t:
        return "battles"
    if "instant" in t:
        return "instants"
    if "sorcery" in t:
        return "sorceries"
    if "enchantment" in t:
        return "enchantments"
    if "artifact" in t:
        return "utilityartifacts"
    return None


def _is_land_card(card: dict) -> bool:
    type_line = card.get("type_line") or ""
    if not type_line:
        faces = card.get("card_faces") or []
        type_line = (faces[0].get("type_line", "") if faces else "")
    return "land" in type_line.lower()


def _basics_for(colors: list[str], count: int) -> list[tuple[str, int]]:
    """Distribute ``count`` basic lands across the commander's colours."""
    if count <= 0:
        return []
    names = [_BASICS[c] for c in colors if c in _BASICS] or ["Wastes"]
    per = {n: 0 for n in names}
    for i in range(count):
        per[names[i % len(names)]] += 1
    return [(n, q) for n, q in per.items() if q > 0]


def build_deck(commander_card: dict, data: dict, owned_keys: set[str],
               budget: float | None, cards_by_name: dict,
               target_lands: int = 36, deck_size: int = 100,
               sideboard_size: int = 18, max_card_price: float | None = None,
               fmt: str = "commander",
               in_deck_keys: set[str] = frozenset(),
               include_cards: list[str] | None = None,
               exclude_cards: list[str] | None = None,
               min_basics: int = 0) -> dict:
    """Assemble a decklist. See module docstring for the selection strategy.

    ``max_card_price`` additionally caps the price of any single bought card —
    e.g. a 30€ total budget with a 5€ per-card cap never adds a 10€ card even
    though it would otherwise fit the remaining total.

    ``in_deck_keys`` marks owned cards with no free copy (every copy already
    sits in another of the user's decks): they are used last, and flagged
    ``from_deck`` so the UI can show they'd be borrowed from an existing deck.

    ``fmt``'s EDHREC page is always the regular (paper) Commander one — EDHREC
    has no separate Duel/Pauper Commander section — so for those variants the
    pool is additionally filtered here to cards Scryfall marks legal in ``fmt``
    (Duel Commander's banned list, Pauper Commander's commons-only restriction).

    ``include_cards`` are player-requested cards the deck MUST contain: they are
    seated before any popularity/budget ordering and deliberately bypass both
    budget caps (the player asked for them explicitly; their price still counts
    in ``buy_total_eur`` so the total stays honest). The caller is responsible
    for validating them (existence, colour identity, legality) — anything not
    resolvable in ``cards_by_name`` is silently dropped here. ``exclude_cards``
    are removed from the pool entirely (main deck AND sideboard).

    ``min_basics`` reserves that many of the ``target_lands`` slots for basic
    lands: EDHREC land sections list plenty of nonbasics, and without a floor
    the manabase ends up almost all nonbasic. A land the player forces via
    ``include_cards`` still claims a nonbasic slot first.
    """
    pool = _pool(data, commander_card["name"])
    legality_key = commanders.SCRYFALL_LEGALITY.get(fmt)
    if legality_key:
        pool = {
            key: entry for key, entry in pool.items()
            if (card := cards_by_name.get(key)) is not None
            and scryfall.legal_in(card, legality_key)
        }

    excluded = {_norm(n) for n in (exclude_cards or [])}
    if excluded:
        pool = {key: entry for key, entry in pool.items() if key not in excluded}

    forced: list[str] = []
    for name in include_cards or []:
        key = _norm(name)
        if key in excluded or key == _norm(commander_card["name"]) or key in forced:
            continue
        entry = pool.get(key)
        if entry is None:
            card = cards_by_name.get(key)
            if card is None:
                continue
            pool[key] = entry = {
                "name": card["name"],
                "inclusion": 0,
                "is_land": _is_land_card(card),
                "category": _category_for_card(card),
            }
        entry["forced"] = True
        forced.append(key)

    def owned(name: str) -> bool:
        return _norm(name) in owned_keys

    def from_deck(name: str) -> bool:
        return _norm(name) in in_deck_keys and _norm(name) in owned_keys

    def card_of(name: str):
        return cards_by_name.get(_norm(name))

    def make_item(entry: dict, is_basic: bool = False, qty: int = 1) -> dict:
        name = entry["name"]
        card = card_of(name)
        is_owned = is_basic or owned(name)
        price = None if is_owned else scryfall.price_eur(card) if card else None
        # EDHREC only types cards listed in a typed section; cards seen only in
        # "High Synergy"/"Top Cards" (and forced includes) fall back to their
        # Scryfall type line instead of landing in "autres".
        category = entry.get("category")
        if not category and card is not None:
            category = _category_for_card(card)
        return {
            "name": name,
            "image": scryfall.image(card) if card else None,
            "owned": is_owned,
            "from_deck": not is_basic and from_deck(name),
            "price_eur": round(price, 2) if price is not None else None,
            "category": category or "autres",
            "is_basic": is_basic,
            "qty": qty,
            "forced": bool(entry.get("forced")),
        }

    buy_total = 0.0

    def price_of(name: str):
        card = card_of(name)
        return scryfall.price_eur(card) if card else None

    def take_unowned(entry: dict, items: list) -> bool:
        nonlocal buy_total
        price = price_of(entry["name"])
        if price is None:
            return False
        if max_card_price is not None and price > max_card_price:
            return False
        if budget is not None and buy_total + price > budget:
            return False
        buy_total += price
        items.append(make_item(entry))
        return True

    def _unowned_order(entries: list) -> list:
        """With a budget, rank by popularity-per-euro so the deck fills out with
        cheap staples first and stays complete; without a budget, rank by raw
        popularity (best cards regardless of price)."""
        if budget is None:
            return sorted(entries, key=lambda e: e["inclusion"], reverse=True)

        def value(e):
            p = price_of(e["name"])
            return -1.0 if p is None else e["inclusion"] / (p + 0.1)

        return sorted(entries, key=value, reverse=True)

    def fill(entries: list, target: int) -> list:
        nonlocal buy_total
        items: list[dict] = []
        # Player-requested cards first: they claim their slot regardless of
        # popularity and bypass budget caps (explicit ask), but an unowned one
        # still adds its price to the buy total.
        for entry in [e for e in entries if e.get("forced")]:
            if len(items) >= target:
                break
            if not owned(entry["name"]):
                price = price_of(entry["name"])
                if price is not None:
                    buy_total += price
            items.append(make_item(entry))
        entries = [e for e in entries if not e.get("forced")]
        # Freely available owned cards next (popularity order) — copies locked
        # in the user's other decks are deliberately skipped here.
        for entry in sorted(entries, key=lambda e: e["inclusion"], reverse=True):
            if len(items) >= target:
                break
            if owned(entry["name"]) and not from_deck(entry["name"]):
                items.append(make_item(entry))
        # Then buy the best value missing cards within budget.
        for entry in _unowned_order([e for e in entries if not owned(e["name"])]):
            if len(items) >= target:
                break
            take_unowned(entry, items)
        # Last resort: borrow cards already sleeved in another deck rather than
        # leaving slots unfilled (flagged from_deck for the UI).
        for entry in sorted(
            [e for e in entries if from_deck(e["name"])],
            key=lambda e: e["inclusion"], reverse=True,
        ):
            if len(items) >= target:
                break
            items.append(make_item(entry))
        return items

    # --- Spells (non-land) + non-basic lands ------------------------------
    spells_target = deck_size - 1 - target_lands
    spells = fill([e for e in pool.values() if not e["is_land"]], spells_target)
    nonbasic_target = max(0, target_lands - max(0, min_basics))
    nonbasic_lands = fill([e for e in pool.values() if e["is_land"]], nonbasic_target)

    # --- Basic lands fill -------------------------------------------------
    basics_needed = target_lands - len(nonbasic_lands)
    colors = scryfall.color_identity(commander_card)
    basic_items = [
        make_item({"name": name, "category": "lands"}, is_basic=True, qty=qty)
        for name, qty in _basics_for(colors, basics_needed)
    ]
    basics_total = sum(b["qty"] for b in basic_items)

    # --- Sideboard: relevant alternatives not in the main deck ------------
    # The next most-played EDHREC cards that didn't make the 100 — swap-ins the
    # player can consider. Prices/owned status are tracked like the main deck.
    used = {_norm(c["name"]) for c in spells + nonbasic_lands}
    used.add(_norm(commander_card["name"]))
    sideboard = [
        make_item(e)
        for e in _sorted(pool, lands=False)
        if _norm(e["name"]) not in used and card_of(e["name"]) is not None
    ][:sideboard_size]
    sideboard_to_buy = [c for c in sideboard if not c["owned"]]
    sideboard_buy_total = round(sum(c["price_eur"] or 0 for c in sideboard_to_buy), 2)

    # --- Group spells by category for display (alphabetical within each) ---
    groups = []
    for cat in CATEGORY_ORDER:
        cards = sorted([s for s in spells if s["category"] == cat], key=_by_name)
        if cards:
            groups.append({"label": CATEGORY_LABELS[cat], "cards": cards})
    land_cards = sorted(nonbasic_lands, key=_by_name) + sorted(basic_items, key=_by_name)
    if land_cards:
        groups.append({"label": CATEGORY_LABELS["lands"], "cards": land_cards})

    commander_item = {
        "name": commander_card["name"],
        "image": scryfall.image(commander_card),
        "owned": owned(commander_card["name"]),
        "from_deck": from_deck(commander_card["name"]),
        "color_identity": colors,
    }

    lands_total = len(nonbasic_lands) + basics_total
    total = 1 + len(spells) + lands_total
    to_buy = [c for c in spells + nonbasic_lands if not c["owned"]]
    from_decks = (1 * commander_item["from_deck"]
                  + sum(1 for c in spells + nonbasic_lands if c["from_deck"]))

    return {
        "commander": commander_item,
        "format": fmt,
        "forced_cards": [c["name"] for c in spells + nonbasic_lands if c["forced"]],
        "groups": groups,
        "sideboard": sideboard,
        "sideboard_buy_total_eur": sideboard_buy_total,
        "buy_list": sorted(to_buy, key=lambda c: c["price_eur"] or 0, reverse=True),
        "buy_total_eur": round(buy_total, 2),
        "budget_eur": budget,
        "max_card_price_eur": max_card_price,
        "counts": {
            "total": total,
            "owned": 1 * commander_item["owned"]
            + sum(1 for c in spells if c["owned"])
            + sum(1 for c in nonbasic_lands if c["owned"]) + basics_total,
            "from_decks": from_decks,
            "to_buy": len(to_buy),
            "lands": lands_total,
            "spells": len(spells),
            "spells_target": spells_target,
            "sideboard": len(sideboard),
            "sideboard_to_buy": len(sideboard_to_buy),
        },
        "unfilled_spells": max(0, spells_target - len(spells)),
        "decklist_text": _decklist_text(commander_item, groups, sideboard),
    }


def _decklist_text(commander_item: dict, groups: list, sideboard: list | None = None) -> str:
    # "Commander" section header so Moxfield/Archidekt import the card as the
    # commander rather than counting it as a 100th maindeck card.
    lines = [f"Commander\n1 {commander_item['name']}\n"]
    for group in groups:
        for c in group["cards"]:
            lines.append(f"{c['qty']} {c['name']}")
    if sideboard:
        lines.append("")
        lines.append("Sideboard")
        for c in sideboard:
            lines.append(f"{c['qty']} {c['name']}")
    return "\n".join(lines)


def validate_includes(include_cards: list[str], resolved: dict,
                      commander_card: dict, fmt: str) -> tuple[list[str], list[dict]]:
    """Split player-requested cards into (valid, rejected-with-reason).

    Anti-hallucination gate for ``include_cards``: a requested card must resolve
    on Scryfall, fit the commander's colour identity and be legal in ``fmt``.
    Rejection reasons are in French — they are relayed verbatim to the player.
    """
    commander_colors = set(scryfall.color_identity(commander_card))
    legality_key = commanders.SCRYFALL_LEGALITY.get(fmt, "commander")
    valid: list[str] = []
    rejected: list[dict] = []
    for name in include_cards:
        card = resolved.get(_norm(name))
        if card is None:
            rejected.append({"name": name, "reason": "introuvable sur Scryfall"})
        elif _norm(card["name"]) == _norm(commander_card["name"]):
            continue  # already in the deck as the commander
        elif not set(scryfall.color_identity(card)) <= commander_colors:
            rejected.append({
                "name": card["name"],
                "reason": "hors identité couleur du commandant",
            })
        elif card.get("legalities") and not scryfall.legal_in(card, legality_key):
            rejected.append({"name": card["name"], "reason": f"non légale en {fmt}"})
        else:
            valid.append(card["name"])
    return valid, rejected


def generate_full_deck(commander_name: str, budget, theme: str, profile_id: int,
                       max_card_price=None, fmt: str = "commander",
                       include_cards: list[str] | None = None,
                       exclude_cards: list[str] | None = None):
    """Resolve cards, build the decklist and write an LLM game plan.

    ``fmt`` (see ``commanders.FORMATS``) picks the Commander variant to build
    for: the EDHREC page is always the regular Commander one, but ``build_deck``
    filters it down to cards legal in ``fmt`` for Duel/Pauper Commander.

    ``include_cards`` / ``exclude_cards`` come from the chat when the player
    asks to add or remove specific cards: includes are validated first (see
    ``validate_includes``) then forced into the deck; the rejected ones are
    reported in ``deck["rejected_includes"]`` so the reply can say WHY a card
    was not kept instead of silently dropping it.

    Blocking (network I/O via Scryfall/EDHREC); run it in a threadpool. Shared by
    the ``/generate`` route and the chat ``generate_decklist`` tool. Returns
    ``(deck, data)`` where ``deck`` is None if the commander can't be resolved.
    """
    import httpx

    from . import db, edhrec, llm
    from .config import settings

    data = edhrec.fetch_commander(commander_name)
    if data.get("_not_found") or data.get("_error"):
        return None, data

    with httpx.Client(timeout=30, headers={"User-Agent": settings.user_agent}) as client:
        nonland, lands = candidate_names(data, commander_name)
        to_resolve = ([commander_name] + list(include_cards or [])
                      + nonland + lands + BASIC_NAMES)
        resolved, _nf = scryfall.resolve_cards(to_resolve, client=client)

    commander_card = resolved.get(commander_name.split("//")[0].strip().lower())
    if not commander_card:
        return None, {"_error": True}

    valid_includes, rejected_includes = validate_includes(
        include_cards or [], resolved, commander_card, fmt
    )

    quantities = db.owned_quantities(profile_id)
    owned = set(quantities)
    # Cards whose every owned copy already sits in another deck: use them last.
    in_deck = {k for k, (qty, deck_qty) in quantities.items() if qty - deck_qty <= 0}
    deck = build_deck(
        commander_card, data, owned, budget, resolved,
        target_lands=settings.deck_lands, deck_size=settings.deck_size,
        sideboard_size=settings.deck_sideboard, max_card_price=max_card_price,
        fmt=fmt, in_deck_keys=in_deck,
        include_cards=valid_includes, exclude_cards=exclude_cards,
        min_basics=settings.deck_min_basics,
    )
    deck["rejected_includes"] = rejected_includes
    deck["excluded_cards"] = list(exclude_cards or [])

    key_cards = [c["name"] for g in deck["groups"] for c in g["cards"] if not c["is_basic"]]
    deck["gameplan"] = llm.deck_gameplan(commander_name, key_cards, theme=theme)
    return deck, data
