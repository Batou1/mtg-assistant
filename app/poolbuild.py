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

from . import llm, manabox, parsing, scryfall
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


# Only Limited is exposed for now. The commented specs show how the SAME engine
# extends to pool-constrained constructed/Commander decks later — add the spec
# here and surface it in the UI/chat; no pipeline changes needed.
SPECS: dict[str, FormatSpec] = {
    "limited": FormatSpec(
        name="limited", label="Limité (draft/sealed)",
        deck_size=settings.limited_deck_size, target_lands=settings.limited_lands,
        add_basics=True, singleton=False, legality_format=None,
    ),
    # "commander": FormatSpec("commander", "Commander", 100, 37, True, True, "commander"),
    # "modern":    FormatSpec("modern", "Modern", 60, 17, True, False, "modern"),
    # "pauper":    FormatSpec("pauper", "Pauper", 60, 17, True, False, "pauper"),
}

DEFAULT_FORMAT = "limited"


def _norm(name: str) -> str:
    return name.split("//")[0].strip().lower()


def _first_face_type(card: dict) -> str:
    return (card.get("type_line") or "").split("//")[0]


def _is_land(card: dict) -> bool:
    return "Land" in _first_face_type(card)


def _is_creature(card: dict) -> bool:
    return "Creature" in _first_face_type(card)


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


def build_from_pool(pool_items, fmt: str, intent: dict, client: httpx.Client | None = None) -> dict:
    """Build the best deck for ``fmt`` from ``pool_items`` (list of (name, qty))."""
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

    spells = [c for c in chosen if not _is_land(c["entry"]["card"])]
    nonbasic_lands = [c for c in chosen if _is_land(c["entry"]["card"])]
    nonbasic_total = sum(c["count"] for c in spells) + sum(c["count"] for c in nonbasic_lands)

    basics: list[tuple[str, int]] = []
    if spec.add_basics:
        need = spec.deck_size - nonbasic_total
        if need < 0:
            spells = _trim(spells, -need)
            need = 0
        colors = [c for c in (selection.get("colors") or intent.get("colors")
                              or _derive_colors([s["entry"]["card"] for s in spells]))
                  if c in _BASICS]
        basics = _distribute_basics(colors, need, selection.get("basic_lands"))
    elif nonbasic_total > spec.deck_size:
        spells = _trim(spells, nonbasic_total - spec.deck_size)

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
        leftover = e["qty"] - chosen_counts.get(e["key"], 0)
        if leftover > 0:
            sideboard.append({"name": e["name"], "image": scryfall.image(e["card"]),
                              "qty": leftover})

    spell_count = sum(c["count"] for c in spells)
    nonbasic_land_count = sum(c["count"] for c in nonbasic_lands)
    creature_count = sum(c["count"] for c in spells if _is_creature(c["entry"]["card"]))
    lands_total = nonbasic_land_count + basics_total

    colors_out = [
        c for c in (selection.get("colors")
                    or _derive_colors([s["entry"]["card"] for s in spells]))
        if c in _BASICS
    ]

    deck = {
        "format": spec.name,
        "format_label": spec.label,
        "archetype": {
            "name": (selection.get("archetype") or "Deck Limité").strip(),
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
            "total": spell_count + lands_total,
            "spells": spell_count,
            "creatures": creature_count,
            "lands": lands_total,
            "sideboard": sum(s["qty"] for s in sideboard),
            "pool_size": sum(e["qty"] for e in pool),
        },
        "invalid": invalid,
        "source": source,
        "deck_size": spec.deck_size,
    }
    deck["decklist_text"] = _decklist_text(deck)
    return deck


def _decklist_text(deck: dict) -> str:
    lines: list[str] = []
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


def build(pool_items, fmt: str, intent: dict) -> dict:
    """Resolve + build with a managed HTTP client. Blocking (network I/O)."""
    with httpx.Client(timeout=30, headers={"User-Agent": settings.user_agent}) as client:
        return build_from_pool(pool_items, fmt, intent, client=client)
