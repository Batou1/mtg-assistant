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
4. The deck is priced with real Cardmarket data. A model pricing cards from
   memory routinely lands an order of magnitude above the requested budget
   (a "200 €" Vintage deck full of Mishra's Workshops), so when the purchase
   cost busts the budget the model is asked to rebuild WITH the real prices in
   hand — up to ``_MAX_BUDGET_PASSES`` times, keeping the cheapest deck. The
   cost is always reported, so an archetype that simply cannot fit the budget
   is stated as such instead of being silently mispriced.
5. Gap analysis vs the active profile's collection (per COPY, not per name:
   owning 2 of a 4-of leaves 2 to buy) + a budget buylist in EUR.
6. A budget of 0 € (or an explicit "only my cards" wish) is a different
   problem: there is no such thing as a free Cardmarket order, so asking the
   model for a cheaper metagame list can never converge — it kept proposing
   28 owned / 32 to buy. ``owned_only`` mode instead hands the model the
   player's own legal, on-colour cards (the same pool-deck idea as
   ``poolbuild``) and clamps every copy count to what is actually owned, so
   the deck costs 0 € by construction and the model only decides *which* of
   the owned cards make the best 60.
"""
import re
from dataclasses import dataclass, field

import httpx

from . import buylist, db, llm, poolbuild, prices, research, scryfall
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

# How many times the model may be asked to rebuild a deck that busts the
# budget. Each pass is a full deck-proposal call, so this trades latency for
# budget accuracy; two passes is enough to go from "cEDH-priced" to "budget
# variant" in practice.
_MAX_BUDGET_PASSES = 2
# A full 60-card list holds at least this many non-basic cards. A revision that
# falls below it (and below the deck it would replace) is a degenerate answer,
# not a cheaper deck — refusing it stops a broken retry from replacing a working
# deck with a pile of basic lands. See _usable.
_MIN_NONBASIC = 24
# Longest price report handed back to the model when asking for a cheaper deck.
_PRICE_REPORT_LINES = 25

# Canonical basic-land names by lowercase key, to reroute any basic the model
# put in main_deck into the basic-land pile (where counts are unlimited).
_BASIC_BY_KEY = {n.lower(): n for n in poolbuild._BASIC_NAMES}

_norm = scryfall.norm_name


def owned_only(intent: dict) -> bool:
    """True when the deck must come entirely from the collection.

    A zero budget means it: "0 €" is not a small budget to optimise towards
    but a refusal to buy anything, and no metagame rewrite can reach it. The
    explicit ``owned_only`` flag covers wishes that never mention money ("que
    des cartes que j'ai").
    """
    budget = intent.get("budget_eur")
    return bool(intent.get("owned_only")) or (budget is not None and budget <= 0)


def _relevance_terms(intent: dict) -> list[str]:
    terms = [str(k).lower() for k in (intent.get("keywords") or [])]
    terms += [w for w in re.findall(r"[a-zà-ÿ]{4,}", (intent.get("theme") or "").lower())]
    return [t for t in dict.fromkeys(terms) if t]


def _owned_pool(profile_id: int, fmt: str, intent: dict, free_qty: dict[str, int],
                excluded: frozenset[str]) -> tuple[list[dict], int]:
    """The player's cards a ``fmt`` deck may be built from: ``(pool, skipped)``.

    Reads the local card cache only (like the collection page): a card the
    cache has not resolved yet has no legality or colours, so it is skipped
    rather than guessed — ``skipped`` counts those. Copies sleeved in other
    decks are already removed from ``free_qty`` (invariant 13). Basic lands
    are left out: they are free and added by the manabase step anyway.

    The pool is capped at ``settings.owned_pool_max`` lines to keep the prompt
    bounded on large collections; when it overflows, cards whose type or text
    mentions the wish's keywords are kept first so the theme survives the cut.
    """
    names = db.collection_names(profile_id)
    cards = db.get_cards(key for _, key, _ in names)
    wanted = {c for c in (intent.get("colors") or []) if c in _COLOR_WORDS}
    pool: list[dict] = []
    skipped = 0
    for _raw, key, _qty in names:
        qty = free_qty.get(key, 0)
        if qty <= 0 or key in excluded or key in _BASIC_BY_KEY:
            continue
        card = cards.get(key)
        if card is None:
            skipped += 1
            continue
        if not scryfall.legal_in(card, fmt):
            continue
        if wanted and not set(scryfall.color_identity(card)) <= wanted:
            continue
        pool.append({"name": card["name"], "key": key,
                     "qty": min(qty, _MAX_COPIES), "card": card})

    cap = settings.owned_pool_max
    if len(pool) > cap:
        terms = _relevance_terms(intent)

        def score(entry: dict) -> int:
            card = entry["card"]
            text = " ".join(str(card.get(f) or "") for f in ("type_line", "oracle_text"))
            faces = card.get("card_faces") or []
            text += " " + " ".join(str(f.get("oracle_text") or "") for f in faces)
            text = text.lower()
            return sum(1 for t in terms if t in text)

        pool.sort(key=lambda e: (-score(e), e["name"]))
        pool = pool[:cap]
    return pool, skipped


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


@dataclass(frozen=True)
class _BuildContext:
    """Everything a deck build needs beyond the model's proposal itself.

    Held apart from the proposal so the exact same validation/assembly runs for
    the first answer and for each budget revision.
    """
    fmt: str
    client: httpx.Client
    owned_qty: dict[str, int]
    in_deck_qty: dict[str, int]
    include_cards: list[str] = field(default_factory=list)
    excluded: frozenset[str] = frozenset()
    max_card_price: float | None = None
    # Every copy must come from the collection: counts are clamped to the
    # owned (free) copies and cards the player has none of are dropped.
    owned_only: bool = False


def _price_report(items: list[dict], max_card_price: float | None) -> str:
    """Real Cardmarket prices of the copies to buy, dearest first.

    This is the grounding handed back to the model when its deck busts the
    budget: without it the rewrite is just another guess from memory.
    """
    priced = [c for c in items if c["missing_qty"] > 0 and c["price_eur"] is not None]
    priced.sort(key=lambda c: c["price_eur"] * c["missing_qty"], reverse=True)
    lines = []
    for c in priced[:_PRICE_REPORT_LINES]:
        line = (f"- {c['missing_qty']}x {c['name']} : {c['price_eur']:.2f} €/u "
                f"= {c['price_eur'] * c['missing_qty']:.2f} €")
        if max_card_price is not None and c["price_eur"] > max_card_price:
            line += f" (DÉPASSE le plafond de {max_card_price:.0f} €/carte)"
        lines.append(line)
    unpriced = [c["name"] for c in items
                if c["missing_qty"] > 0 and c["price_eur"] is None]
    if unpriced:
        lines.append("- prix indisponible : " + ", ".join(unpriced[:10]))
    return "\n".join(lines)


def _build_deck(archetype: dict, ctx: _BuildContext) -> dict:
    """Validate a model proposal and assemble the priced 60-card deck.

    Pure of any budget decision: it reports what the deck costs (``cost_eur``,
    the missing copies only — owned ones are free) and lets ``analyze`` decide
    whether that is acceptable.
    """
    entries, basic_weights = _deck_entries(archetype)
    colors = [c for c in (archetype.get("colors") or []) if c in _COLOR_WORDS]
    fmt = ctx.fmt

    names = [n for n, _ in entries] + ctx.include_cards
    resolved, _nf = (scryfall.resolve_cards(names, client=ctx.client)
                     if names else ({}, []))

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
        if key in ctx.excluded:
            continue
        if key in merged:
            merged[key]["count"] = min(_MAX_COPIES, merged[key]["count"] + count)
        else:
            merged[key] = {"name": card["name"], "card": card,
                           "count": min(_MAX_COPIES, count)}
            order.append(key)

    # Player-requested cards must end up in the deck even if the LLM
    # ignored the prompt instruction: validate them (existence + format
    # legality) and force-add any missing one with a single copy. Rejects
    # carry a French reason relayed verbatim to the player.
    rejected_includes: list[dict] = []
    for name in ctx.include_cards:
        card = resolved.get(_norm(name))
        if not card:
            rejected_includes.append(
                {"name": name, "reason": "introuvable sur Scryfall"})
            continue
        if not scryfall.legal_in(card, fmt):
            rejected_includes.append(
                {"name": card["name"], "reason": f"non légale en {fmt}"})
            continue
        key = _norm(card["name"])
        if key in ctx.excluded:
            continue
        if key not in merged:
            merged[key] = {"name": card["name"], "card": card, "count": 1}
            order.append(key)
        merged[key]["forced"] = True

    # Collection-only build: the model was shown the owned pool, but a name
    # outside it (or more copies than owned) must not turn into a purchase.
    # Player-forced cards are the one exception — an explicit ask wins and
    # its cost is reported like any other missing copy.
    not_owned: list[str] = []
    if ctx.owned_only:
        for key in list(order):
            entry = merged[key]
            if entry.get("forced"):
                continue
            have = ctx.owned_qty.get(key, 0)
            if have <= 0:
                not_owned.append(entry["name"])
                del merged[key]
                order.remove(key)
            else:
                entry["count"] = min(entry["count"], have)

    chosen = [merged[k] for k in order]
    spells = [c for c in chosen if not poolbuild._is_land(c["card"])]
    nonbasic_lands = [c for c in chosen if poolbuild._is_land(c["card"])]

    # Top up to exactly DECK_SIZE with basic lands (dropping invalid cards
    # must not leave a short deck); trim low-priority spells if the model
    # overshot despite the prompt. _trim eats from the end, so forced
    # (player-requested) cards are moved to the front to survive it.
    nonbasic_total = sum(c["count"] for c in spells) + sum(c["count"] for c in nonbasic_lands)
    need = DECK_SIZE - nonbasic_total
    if need < 0:
        spells.sort(key=lambda c: not c.get("forced"))
        spells = poolbuild._trim(spells, -need)
        need = 0
    basics = poolbuild._distribute_basics(colors, need, basic_weights or None)

    def deck_item(entry: dict) -> dict:
        card, count = entry["card"], entry["count"]
        key = _norm(entry["name"])
        have = ctx.owned_qty.get(key, 0)
        owned = min(count, have)
        return {
            "name": entry["name"],
            "image": scryfall.image(card),
            "qty": count,
            "owned_qty": owned,
            "missing_qty": count - owned,
            # Missing copies the user does own — but locked in other decks.
            "in_deck_qty": min(count - owned, ctx.in_deck_qty.get(key, 0)),
            "owned": owned >= count,
            "price_eur": prices.buy_price_eur(card),
            "cmc": card.get("cmc"),
            "forced": bool(entry.get("forced")),
        }

    by_cat: dict[str, list] = {}
    for c in spells:
        by_cat.setdefault(poolbuild._category(c["card"]), []).append(deck_item(c))
    groups = [
        {"label": CATEGORY_LABELS[cat],
         "cards": sorted(by_cat[cat], key=poolbuild._by_name)}
        for cat in CATEGORY_ORDER if by_cat.get(cat)
    ]
    nonbasic_items = sorted((deck_item(c) for c in nonbasic_lands),
                            key=poolbuild._by_name)
    # Basic lands are treated as freely available (same convention as the
    # Commander generator): they cost pennies and everyone has piles.
    basic_items = [
        {"name": n, "qty": q, "owned_qty": q, "missing_qty": 0, "owned": True,
         "is_basic": True}
        for n, q in basics
    ]

    all_items = [c for g in groups for c in g["cards"]] + nonbasic_items
    # What the player actually has to spend to play this deck: every missing
    # copy at its real price. Cards Scryfall has no EUR price for can't be
    # counted, so they're reported separately rather than treated as free.
    cost_eur = sum(c["price_eur"] * c["missing_qty"] for c in all_items
                   if c["price_eur"] is not None and c["missing_qty"] > 0)
    unpriced_missing = sum(c["missing_qty"] for c in all_items
                           if c["price_eur"] is None and c["missing_qty"] > 0)
    over_cap = ([c["name"] for c in all_items
                 if c["price_eur"] is not None and not c["forced"]
                 and c["price_eur"] > ctx.max_card_price]
                if ctx.max_card_price is not None else [])

    return {
        "archetype": archetype,
        "colors": colors,
        "groups": groups,
        "nonbasic_items": nonbasic_items,
        "basic_items": basic_items,
        "basics": basics,
        "all_items": all_items,
        "dropped": dropped,
        "not_owned": not_owned,
        "rejected_includes": rejected_includes,
        "nonbasic_total": sum(c["qty"] for c in all_items),
        "cost_eur": round(cost_eur, 2),
        "unpriced_missing": unpriced_missing,
        "over_cap": over_cap,
        "price_report": _price_report(all_items, ctx.max_card_price),
    }


def _fits_budget(build: dict, budget: float | None) -> bool:
    """True when the deck respects BOTH budget constraints as built."""
    if budget is not None and build["cost_eur"] > budget:
        return False
    return not build["over_cap"]


def _usable(candidate: dict, current: dict) -> bool:
    """Reject a revision that came back as a shell of a deck.

    A rewrite whose cards mostly failed validation is "cheaper" only because it
    is no longer a deck. It must hold a real 60-card list's worth of non-basic
    cards — or, when the deck it replaces is itself smaller than that, at least
    as many as that one.
    """
    floor = min(_MIN_NONBASIC, current["nonbasic_total"])
    return candidate["nonbasic_total"] >= floor


def _empty_pool_result(fmt: str, intent: dict, results: list, skipped: int) -> dict:
    """Collection-only wish with nothing to build from: say so, build nothing.

    Padding 60 basic lands would be a "deck" costing 0 € — and useless. The
    result keeps the archetype header so the artifact template renders.
    """
    colors = [c for c in (intent.get("colors") or []) if c in _COLOR_WORDS]
    return {
        "format": fmt,
        "archetype": {"name": "Deck 100 % collection", "colors": colors,
                      "strategy": ""},
        "owned_only": True,
        "owned_pool_empty": True,
        "owned_pool_size": 0,
        "owned_pool_unresolved": skipped,
        "sources": results[:6],
        "budget_eur": intent.get("budget_eur"),
        "max_card_price_eur": intent.get("max_card_price_eur"),
        "llm_unavailable": False,
    }


def analyze(intent: dict, profile_id: int) -> dict:
    fmt = intent.get("format")
    only_owned = owned_only(intent)
    results = research.brave_search(_query(fmt, intent))
    context = research.context_block(results)

    # Only copies not already sleeved in another of the user's decks count as
    # available; the rest is surfaced separately (in_deck_qty) so the user
    # knows buying replaces borrowing.
    quantities = db.owned_quantities(profile_id)
    free_qty = {k: max(0, qty - deck_qty) for k, (qty, deck_qty) in quantities.items()}
    budget = intent.get("budget_eur")
    max_card_price = intent.get("max_card_price_eur")

    include_cards = [n.strip() for n in (intent.get("include_cards") or [])
                     if isinstance(n, str) and n.strip()]
    exclude_cards = [n.strip() for n in (intent.get("exclude_cards") or [])
                     if isinstance(n, str) and n.strip()]
    excluded = frozenset(_norm(n) for n in exclude_cards)

    pool: list[dict] = []
    pool_skipped = 0
    if only_owned:
        # The deck is chosen FROM the collection, not researched then priced:
        # the model sees the owned pool and nothing else, and a key-free
        # install falls back to the same curve-filling heuristic as the
        # "from a list" page.
        pool, pool_skipped = _owned_pool(profile_id, fmt, intent, free_qty, excluded)
        if not pool:
            return _empty_pool_result(fmt, intent, results, pool_skipped)
        archetype = llm.archetype_from_collection(
            fmt, intent, [poolbuild._pool_line(e) for e in pool], context)
        if not archetype:
            archetype = poolbuild._heuristic_select(
                pool, poolbuild.SPECS[fmt], intent)
    else:
        archetype = llm.archetype_research(fmt, intent, context)
        if not archetype:
            return {"format": fmt, "llm_unavailable": True, "sources": results}

    with httpx.Client(timeout=30, headers={"User-Agent": settings.user_agent}) as client:
        ctx = _BuildContext(
            fmt=fmt, client=client,
            owned_qty=free_qty,
            in_deck_qty={k: deck_qty for k, (_qty, deck_qty) in quantities.items()},
            include_cards=include_cards,
            excluded=excluded,
            max_card_price=max_card_price,
            owned_only=only_owned,
        )

        build = _build_deck(archetype, ctx)
        # The model priced the deck from memory; now that it has real prices,
        # give it a chance to rebuild under budget. Keep the cheapest usable
        # answer and stop as soon as a pass fails to improve on it. A
        # collection-only build costs 0 € by construction, so it never loops.
        budget_passes = 0
        while (budget is not None and not only_owned and not _fits_budget(build, budget)
               and budget_passes < _MAX_BUDGET_PASSES and llm.is_available()):
            budget_passes += 1
            revised = llm.archetype_revise(
                fmt, intent, build["archetype"], build["price_report"],
                build["cost_eur"])
            if not revised:
                break
            candidate = _build_deck(revised, ctx)
            if (not _usable(candidate, build)
                    or candidate["cost_eur"] >= build["cost_eur"]):
                break
            build = candidate

        groups = build["groups"]
        nonbasic_items = build["nonbasic_items"]
        all_items = build["all_items"]
        missing = [(c["name"], c["missing_qty"]) for c in all_items if c["missing_qty"] > 0]
        buy = buylist.build(missing, budget,
                            max_card_price=max_card_price, client=client)

    archetype = build["archetype"]
    basics_total = sum(q for _, q in build["basics"])
    lands_total = sum(c["qty"] for c in nonbasic_items) + basics_total
    spells_total = sum(c["qty"] for g in groups for c in g["cards"])
    owned_copies = sum(c["owned_qty"] for c in all_items) + basics_total
    total = spells_total + lands_total
    lands = {"nonbasic": nonbasic_items, "basics": build["basic_items"],
             "total": lands_total}

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
            "colors": build["colors"],
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
        "forced_cards": [c["name"] for c in all_items if c.get("forced")],
        "rejected_includes": build["rejected_includes"],
        "excluded_cards": exclude_cards,
        "dropped": build["dropped"],
        # Collection-only mode: names the model picked outside the owned pool
        # (dropped rather than bought) and the size of the pool it chose from.
        "owned_only": only_owned,
        "owned_pool_empty": False,
        "owned_pool_size": len(pool),
        "owned_pool_unresolved": pool_skipped,
        "not_owned": build["not_owned"],
        "sources": results[:6],
        "budget_eur": budget,
        "max_card_price_eur": max_card_price,
        # Full price of the copies to buy — NOT the buylist total, which stops
        # at the budget. The gap between the two is what the user still has to
        # spend (or cut) to actually play the deck.
        "deck_cost_eur": build["cost_eur"],
        "budget_exceeded": budget is not None and build["cost_eur"] > budget,
        "over_budget_eur": (round(build["cost_eur"] - budget, 2)
                            if budget is not None and build["cost_eur"] > budget else 0.0),
        "budget_passes": budget_passes,
        "unpriced_missing": build["unpriced_missing"],
        "over_cap_cards": build["over_cap"],
        "llm_unavailable": False,
    }
