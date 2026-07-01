"""Pool-constrained deckbuilding — pick the best deck from a *given* card list.

Unlike the rest of the app (which builds from your collection / EDHREC / web
research), this module takes a fixed **pool** of cards the user provides — a
draft or sealed pool, pasted or imported — and returns the strongest deck
buildable from it. Today only **Limited** (40 cards, ~17 lands, basics added
freely) is wired up, but the engine is intentionally generic: a ``FormatSpec``
describes the deck-construction rules (size, lands, singleton, legality) so the
SAME pipeline can later answer "best Commander / Modern / Pauper deck from this
list" — just register another spec and expose it.

Pipeline:
1. Parse the pool (paste / .txt decklist / ManaBox-style .csv) into (name, qty).
2. Resolve every name against Scryfall; drop anything that doesn't exist (and,
   for formats with a legality, anything not legal there).
3. Ask the LLM to pick the best deck — grounded: it may only name cards from the
   pool, and we re-check each chosen name against the pool. No API key ⇒ a
   heuristic picker keeps the feature working.
4. Assemble the deck: group spells by type, add basic lands to hit the deck
   size, leftover pool cards become the sideboard.
"""
import re
from collections import Counter
from dataclasses import dataclass

import httpx

from . import buylist, db, llm, manabox, parsing, scryfall
from .config import settings

_BASICS = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}
_BASIC_NAMES = set(_BASICS.values()) | {"Wastes"}

# Display order + French labels for the deck's spell categories (lands handled
# separately). Mirrors deckgen's vocabulary so the UI feels consistent.
CATEGORY_ORDER = [
    "creatures", "instants", "sorceries", "artifacts",
    "enchantments", "planeswalkers", "autres",
]
CATEGORY_LABELS = {
    "creatures": "Créatures",
    "instants": "Éphémères",
    "sorceries": "Rituels",
    "artifacts": "Artefacts",
    "enchantments": "Enchantements",
    "planeswalkers": "Planeswalkers",
    "autres": "Autres",
    "lands": "Terrains",
}


@dataclass(frozen=True)
class FormatSpec:
    """Deck-construction rules for building from a pool."""
    name: str
    label: str
    deck_size: int
    target_lands: int        # desired land count when add_basics is True
    add_basics: bool         # top up with basic lands to reach deck_size
    singleton: bool          # at most one copy of each card
    legality_format: str | None  # Scryfall legality to enforce, or None (Limited)
    needs_commander: bool = False   # pick a legendary commander from the pool
    # Recommend extra owned/buy synergy cards beyond the deck (off for Limited,
    # where the deck must come solely from the sealed/draft pool).
    allow_collection_bonus: bool = True


def _constructed(name: str, label: str) -> FormatSpec:
    """A standard 60-card constructed format (legality enforced, basics free)."""
    return FormatSpec(name=name, label=label, deck_size=60, target_lands=17,
                      add_basics=True, singleton=False, legality_format=name)


# All pool-buildable formats share the one engine below; the spec is the only
# thing that differs. Limited stays pool-only (no collection bonus); the rest
# enforce Scryfall legality and can suggest synergy cards from your collection.
SPECS: dict[str, FormatSpec] = {
    "limited": FormatSpec(
        name="limited", label="Limité (draft/sealed)",
        deck_size=settings.limited_deck_size, target_lands=settings.limited_lands,
        add_basics=True, singleton=False, legality_format=None,
        allow_collection_bonus=False,
    ),
    "commander": FormatSpec(
        name="commander", label="Commander", deck_size=100, target_lands=37,
        add_basics=True, singleton=True, legality_format="commander",
        needs_commander=True,
    ),
    "standard": _constructed("standard", "Standard"),
    "pioneer": _constructed("pioneer", "Pioneer"),
    "modern": _constructed("modern", "Modern"),
    "pauper": _constructed("pauper", "Pauper"),
    "legacy": _constructed("legacy", "Legacy"),
    "vintage": _constructed("vintage", "Vintage"),
    "premodern": _constructed("premodern", "Premodern"),
}

# Stable display order for the format selector (engine ignores order).
FORMAT_ORDER = ["limited", "commander", "standard", "pioneer", "modern",
                "pauper", "legacy", "vintage", "premodern"]

DEFAULT_FORMAT = "limited"


def _norm(name: str) -> str:
    return name.split("//")[0].strip().lower()


def _first_face_type(card: dict) -> str:
    return (card.get("type_line") or "").split("//")[0]


def _is_land(card: dict) -> bool:
    return "Land" in _first_face_type(card)


def _is_creature(card: dict) -> bool:
    return "Creature" in _first_face_type(card)


def _is_commander_candidate(card: dict) -> bool:
    """A card that can be a Commander: a legendary creature (good enough here)."""
    t = _first_face_type(card)
    return "Legendary" in t and "Creature" in t


def _category(card: dict) -> str:
    t = _first_face_type(card)
    if "Creature" in t:
        return "creatures"
    if "Instant" in t:
        return "instants"
    if "Sorcery" in t:
        return "sorceries"
    if "Planeswalker" in t:
        return "planeswalkers"
    if "Enchantment" in t:
        return "enchantments"
    if "Artifact" in t:
        return "artifacts"
    return "autres"


# --- Pool parsing --------------------------------------------------------

def parse_pool(text: str, filename: str = "") -> list[tuple[str, int]]:
    """Parse pasted text / a .txt decklist / a ManaBox-style .csv into (name, qty)."""
    text = text or ""
    is_csv = filename.lower().endswith(".csv")
    if not is_csv:
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        # A header row with commas and a recognisable column name ⇒ treat as CSV.
        if "," in first and re.search(r"\b(name|quantity|scryfall|set code)\b", first, re.I):
            is_csv = True

    if is_csv:
        rows, _errors = manabox.parse_manabox_csv(text)
        items: dict[str, list] = {}
        order: list[str] = []
        for r in rows:
            name = r["raw_name"]
            key = name.lower()
            if key in items:
                items[key][1] += r["quantity"]
            else:
                items[key] = [name, r["quantity"]]
                order.append(key)
        return [(items[k][0], items[k][1]) for k in order]

    items2, _unrecognized = parsing.parse_decklist(text)
    return items2


# --- Selection (LLM, with heuristic fallback) ----------------------------

def _pool_line(entry: dict) -> str:
    card = entry["card"]
    cost = card.get("mana_cost") or ""
    type_line = card.get("type_line") or ""
    oracle = card.get("oracle_text")
    if not oracle:
        faces = card.get("card_faces") or []
        oracle = " // ".join(f.get("oracle_text", "") for f in faces if f.get("oracle_text"))
    oracle = (oracle or "").replace("\n", " ").strip()
    if len(oracle) > 140:
        oracle = oracle[:140] + "…"
    qty = f" (x{entry['qty']})" if entry["qty"] > 1 else ""
    return f"- {entry['name']}{qty} | {cost} | {type_line} | {oracle}"


def _derive_colors(cards: list[dict]) -> list[str]:
    """Union of the colour identities of the given cards (WUBRG)."""
    colors: list[str] = []
    for card in cards:
        for c in scryfall.color_identity(card):
            if c in _BASICS and c not in colors:
                colors.append(c)
    return colors


def _heuristic_select(pool: list[dict], spec: FormatSpec, intent: dict) -> dict:
    """Key-free fallback: pick the strongest colour pair by depth, fill the curve."""
    nonland = [e for e in pool if not _is_land(e["card"])]

    if intent.get("colors"):
        colors = [c for c in intent["colors"] if c in _BASICS]
    else:
        support: Counter = Counter()
        for e in nonland:
            for c in scryfall.color_identity(e["card"]):
                if c in _BASICS:
                    support[c] += e["qty"]
        max_colors = intent.get("max_colors") or 2
        colors = [c for c, _ in support.most_common(max_colors)]
    colorset = set(colors)

    def fits(card: dict) -> bool:
        return set(scryfall.color_identity(card)) <= colorset  # colourless fits any

    playable = [e for e in nonland if fits(e["card"])]
    # Curve-friendly: cheap cards first, then by name for stability.
    playable.sort(key=lambda e: ((e["card"].get("cmc") or 0), e["name"]))

    target_spells = spec.deck_size - (spec.target_lands if spec.add_basics else 0)
    main, count = [], 0
    for e in playable:
        if count >= target_spells:
            break
        take = 1 if spec.singleton else min(e["qty"], target_spells - count)
        main.append({"name": e["name"], "count": take})
        count += take

    # Include on-colour non-basic lands from the pool.
    for e in pool:
        if _is_land(e["card"]) and fits(e["card"]):
            main.append({"name": e["name"], "count": min(e["qty"], 2)})

    archetype = ("/".join(colors) + " Limité") if colors else "Limité"
    return {"archetype": archetype, "colors": colors, "strategy": "", "main_deck": main,
            "basic_lands": {}}


# --- Land maths ----------------------------------------------------------

def _distribute_basics(colors: list[str], count: int, model_basics: dict | None) -> list[tuple[str, int]]:
    """Split ``count`` basic lands across colours, honouring the model's ratio."""
    if count <= 0:
        return []
    weights: dict[str, int] = {}
    for name, c in (model_basics or {}).items():
        if name in _BASIC_NAMES:
            try:
                w = int(c)
            except (TypeError, ValueError):
                continue
            if w > 0:
                weights[name] = w
    if not weights:
        names = [_BASICS[c] for c in colors if c in _BASICS] or ["Wastes"]
        weights = {n: 1 for n in names}

    total_w = sum(weights.values())
    raw = {n: count * w / total_w for n, w in weights.items()}
    base = {n: int(x) for n, x in raw.items()}
    remainder = count - sum(base.values())
    for n in sorted(weights, key=lambda n: raw[n] - base[n], reverse=True)[:remainder]:
        base[n] += 1
    return [(n, q) for n, q in base.items() if q > 0]


def _trim(cards: list[dict], excess: int) -> list[dict]:
    """Reduce total copies by ``excess``, from the end (lowest-priority first)."""
    i = len(cards) - 1
    while excess > 0 and i >= 0:
        take = min(cards[i]["count"], excess)
        cards[i]["count"] -= take
        excess -= take
        i -= 1
    return [c for c in cards if c["count"] > 0]


# --- Assembly ------------------------------------------------------------

def _item(entry: dict, count: int) -> dict:
    card = entry["card"]
    return {
        "name": entry["name"],
        "image": scryfall.image(card),
        "qty": count,
        "category": _category(card),
        "cmc": card.get("cmc"),
    }


def build_from_pool(pool_items, fmt: str, intent: dict, client: httpx.Client | None = None,
                    profile_id: int | None = None, with_bonus: bool = True) -> dict:
    """Build the best deck for ``fmt`` from ``pool_items`` (list of (name, qty)).

    For non-Limited formats, also recommends synergy cards beyond the deck count
    (owned ones from ``profile_id``'s collection + a few to buy) when ``with_bonus``.
    """
    spec = SPECS.get(fmt) or SPECS[DEFAULT_FORMAT]
    pool_items = [(n, int(q)) for n, q in pool_items if n]

    names = [n for n, _ in pool_items]
    resolved, _nf = scryfall.resolve_cards(names, client=client) if names else ({}, [])

    # Validate + merge duplicate names, preserving first-seen order.
    merged: dict[str, dict] = {}
    order: list[str] = []
    invalid: list[str] = []
    for name, qty in pool_items:
        card = resolved.get(_norm(name))
        if not card or (spec.legality_format and not scryfall.legal_in(card, spec.legality_format)):
            invalid.append(name)
            continue
        key = _norm(card["name"])
        if key in merged:
            merged[key]["qty"] += qty
        else:
            merged[key] = {"name": card["name"], "key": key, "qty": qty, "card": card}
            order.append(key)
    pool = [merged[k] for k in order]
    index = {e["key"]: e for e in pool}

    selection = None
    if llm.is_available() and pool:
        selection = llm.pool_deck(spec, intent, [_pool_line(e) for e in pool])
    source = "llm" if selection else "heuristic"
    if not selection:
        selection = _heuristic_select(pool, spec, intent)

    # Ground the selection: keep only pool cards, clamp copies to availability.
    chosen: list[dict] = []
    seen: set[str] = set()
    for item in selection.get("main_deck") or []:
        name = item.get("name") if isinstance(item, dict) else item
        raw_count = item.get("count", 1) if isinstance(item, dict) else 1
        if not isinstance(name, str):
            continue
        entry = index.get(_norm(name))
        if entry is None or entry["key"] in seen:
            continue
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = 1
        count = 1 if spec.singleton else max(1, min(count, entry["qty"]))
        chosen.append({"entry": entry, "count": count})
        seen.add(entry["key"])

    # Commander: pick a legendary commander from the pool and enforce its colour
    # identity on the rest of the deck (a real EDH rule + keeps the deck legal).
    commander_item = None
    commander_identity: set[str] | None = None
    commander_key: str | None = None
    if spec.needs_commander and pool:
        cmd = index.get(_norm(selection.get("commander") or ""))
        if cmd is None:
            cmd = next((e for e in pool if _is_commander_candidate(e["card"])), None)
        if cmd is not None:
            commander_key = cmd["key"]
            commander_identity = {c for c in scryfall.color_identity(cmd["card"]) if c in _BASICS}
            chosen = [
                c for c in chosen
                if c["entry"]["key"] != cmd["key"]
                and set(scryfall.color_identity(c["entry"]["card"])) <= commander_identity
            ]
            commander_item = {
                "name": cmd["name"], "image": scryfall.image(cmd["card"]),
                "colors": sorted(commander_identity),
            }

    spells = [c for c in chosen if not _is_land(c["entry"]["card"])]
    nonbasic_lands = [c for c in chosen if _is_land(c["entry"]["card"])]
    nonbasic_total = sum(c["count"] for c in spells) + sum(c["count"] for c in nonbasic_lands)

    # The commander occupies one of the deck's slots.
    fill_size = spec.deck_size - (1 if commander_item else 0)

    basics: list[tuple[str, int]] = []
    if spec.add_basics:
        need = fill_size - nonbasic_total
        if need < 0:
            spells = _trim(spells, -need)
            need = 0
        if commander_identity is not None:
            colors = sorted(commander_identity)
        else:
            colors = [c for c in (selection.get("colors") or intent.get("colors")
                                  or _derive_colors([s["entry"]["card"] for s in spells]))
                      if c in _BASICS]
        basics = _distribute_basics(colors, need, selection.get("basic_lands"))
    elif nonbasic_total > fill_size:
        spells = _trim(spells, nonbasic_total - fill_size)

    basics_total = sum(q for _, q in basics)

    # Group spells by category for display.
    by_cat: dict[str, list] = {}
    for c in spells:
        by_cat.setdefault(_category(c["entry"]["card"]), []).append(
            _item(c["entry"], c["count"])
        )
    groups = [
        {"label": CATEGORY_LABELS[cat], "cards": by_cat[cat]}
        for cat in CATEGORY_ORDER if by_cat.get(cat)
    ]

    nonbasic_land_items = [_item(c["entry"], c["count"]) for c in nonbasic_lands]
    basic_items = [{"name": n, "qty": q, "is_basic": True} for n, q in basics]

    # Sideboard = whatever of the pool isn't in the maindeck.
    chosen_counts = {c["entry"]["key"]: c["count"] for c in chosen}
    sideboard = []
    for e in pool:
        if e["key"] == commander_key:
            continue  # the commander is shown on its own, not as a leftover
        leftover = e["qty"] - chosen_counts.get(e["key"], 0)
        if leftover > 0:
            sideboard.append({"name": e["name"], "image": scryfall.image(e["card"]),
                              "qty": leftover})

    spell_count = sum(c["count"] for c in spells)
    nonbasic_land_count = sum(c["count"] for c in nonbasic_lands)
    creature_count = sum(c["count"] for c in spells if _is_creature(c["entry"]["card"]))
    lands_total = nonbasic_land_count + basics_total

    if commander_identity is not None:
        colors_out = sorted(commander_identity)
    else:
        colors_out = [
            c for c in (selection.get("colors")
                        or _derive_colors([s["entry"]["card"] for s in spells]))
            if c in _BASICS
        ]

    deck = {
        "format": spec.name,
        "format_label": spec.label,
        "commander": commander_item,
        "archetype": {
            "name": (selection.get("archetype") or f"Deck {spec.label}").strip(),
            "colors": colors_out,
            "strategy": (selection.get("strategy") or "").strip(),
        },
        "groups": groups,
        "lands": {
            "nonbasic": nonbasic_land_items,
            "basics": basic_items,
            "total": lands_total,
        },
        "sideboard": sideboard,
        "counts": {
            "total": (1 if commander_item else 0) + spell_count + lands_total,
            "spells": spell_count,
            "creatures": creature_count,
            "lands": lands_total,
            "sideboard": sum(s["qty"] for s in sideboard),
            "pool_size": sum(e["qty"] for e in pool),
        },
        "invalid": invalid,
        "source": source,
        "deck_size": spec.deck_size,
        "bonus": None,
    }
    deck["decklist_text"] = _decklist_text(deck)

    if with_bonus and spec.allow_collection_bonus and profile_id is not None and llm.is_available():
        deck["bonus"] = _compute_bonus(
            deck, spec, intent, profile_id,
            pool_keys={e["key"] for e in pool}, client=client,
        )
    return deck


def _decklist_text(deck: dict) -> str:
    lines: list[str] = []
    if deck.get("commander"):
        lines.append(f"Commander\n1 {deck['commander']['name']}\n")
    for group in deck["groups"]:
        for c in group["cards"]:
            lines.append(f"{c['qty']} {c['name']}")
    for c in deck["lands"]["nonbasic"]:
        lines.append(f"{c['qty']} {c['name']}")
    for c in deck["lands"]["basics"]:
        lines.append(f"{c['qty']} {c['name']}")
    if deck["sideboard"]:
        lines.append("")
        lines.append("Sideboard")
        for c in deck["sideboard"]:
            lines.append(f"{c['qty']} {c['name']}")
    return "\n".join(lines)


# --- Collection bonus: synergy cards beyond the deck ---------------------

def _compute_bonus(deck: dict, spec: FormatSpec, intent: dict, profile_id: int,
                   pool_keys: set[str], client: httpx.Client | None) -> dict | None:
    """Recommend extra cards beyond the deck: owned synergy + a few to buy.

    Owned candidates come from the player's collection (legal + colour/identity
    fit, not already in the submitted pool); the LLM ranks them and proposes a
    few new cards to buy. Buy suggestions are Scryfall-validated and priced.
    """
    identity = set(deck["archetype"]["colors"])  # commander identity or deck colours
    legal_fmt = spec.legality_format

    def fits(card: dict) -> bool:
        if legal_fmt and not scryfall.legal_in(card, legal_fmt):
            return False
        return set(scryfall.color_identity(card)) <= identity if identity else \
            not scryfall.color_identity(card)

    # Owned cards not already in the submitted pool.
    owned = [(raw, key) for raw, key, _qty in db.collection_names(profile_id)
             if key not in pool_keys]
    cand_names = [raw for raw, _key in owned][:settings.bonus_owned_scan]
    resolved, _nf = scryfall.resolve_cards(cand_names, client=client) if cand_names else ({}, [])
    eligible = [resolved[_norm(n)]["name"] for n in cand_names
                if _norm(n) in resolved and fits(resolved[_norm(n)])]
    eligible_set = {n.lower() for n in eligible}

    key_cards = [deck["commander"]["name"]] if deck.get("commander") else []
    key_cards += [c["name"] for g in deck["groups"] for c in g["cards"]][:40]

    sel = llm.pool_bonus(spec, deck["archetype"], key_cards,
                         deck["archetype"]["colors"], eligible[:80]) or {}

    # Owned bonus: grounded to the eligible owned list.
    owned_bonus, seen = [], set()
    for name in sel.get("owned_bonus") or []:
        if isinstance(name, str) and name.lower() in eligible_set and name.lower() not in seen:
            card = resolved.get(_norm(name))
            owned_bonus.append({"name": card["name"] if card else name,
                                "image": scryfall.image(card) if card else None})
            seen.add(name.lower())
        if len(owned_bonus) >= settings.bonus_owned_max:
            break

    # Buy bonus: new cards the model proposes — must exist, be legal, fit, and
    # not already be owned / in the pool / in the deck.
    known = pool_keys | eligible_set | {n.lower() for n in seen}
    if deck.get("commander"):
        known.add(deck["commander"]["name"].lower())
    buy_names = [n for n in (sel.get("buy_bonus") or []) if isinstance(n, str)]
    buy_resolved, _bnf = scryfall.resolve_cards(buy_names, client=client) if buy_names else ({}, [])
    buy_candidates, seen_buy = [], set()
    for name in buy_names:
        card = buy_resolved.get(_norm(name))
        key = _norm(name)
        if card is None or key in known or key in seen_buy:
            continue
        if not fits(card):
            continue
        seen_buy.add(key)
        buy_candidates.append(card["name"])
    buy = buylist.build(buy_candidates[:settings.bonus_buy_max],
                        intent.get("budget_eur"),
                        max_card_price=intent.get("max_card_price_eur"), client=client)

    if not owned_bonus and not buy.get("to_buy"):
        return None
    return {
        "owned": owned_bonus,
        "owned_count": len(owned_bonus),
        "buy": buy.get("to_buy") or [],
        "buy_count": buy.get("bought_count", 0),
        "buy_total_eur": buy.get("total_eur", 0),
        "budget_eur": intent.get("budget_eur"),
        "max_card_price_eur": intent.get("max_card_price_eur"),
    }


def build(pool_items, fmt: str, intent: dict, profile_id: int | None = None) -> dict:
    """Resolve + build with a managed HTTP client. Blocking (network I/O)."""
    with httpx.Client(timeout=30, headers={"User-Agent": settings.user_agent}) as client:
        return build_from_pool(pool_items, fmt, intent, client=client, profile_id=profile_id)
