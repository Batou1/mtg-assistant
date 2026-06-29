"""Intent-aware Commander analysis against the stored collection.

For the cards you own, find legal commanders, score each against its EDHREC
page (how many recommended cards you already have), and rank them by how well
they fit the requested intent (colors + theme) and your collection.
"""
import time

import httpx

from . import buylist, commanders, db, edhrec, scryfall
from .config import settings


def _norm(name: str) -> str:
    return name.split("//")[0].strip().lower()


def _color_ok(card: dict, wanted: list[str], max_colors: int | None) -> bool:
    """Does the commander respect the requested colours and colour count?

    With colours requested, the commander's colour identity must be non-empty
    and a subset of them (so 'noir/blanc' never surfaces a blue or green card,
    nor a colourless one). ``max_colors`` enforces e.g. mono-colour: 'monocouleur
    noir ou blanc' is colours {B,W} with max_colors=1, keeping only mono-black or
    mono-white commanders — not two-colour Orzhov.
    """
    identity = set(scryfall.color_identity(card))
    if wanted:
        allowed = set(wanted)
        if not identity or not identity.issubset(allowed):
            return False
    if max_colors is not None and len(identity) > max_colors:
        return False
    return True


def _theme_score(intent: dict, recommended: list[str], tags: list[str]) -> int:
    """Heuristic fit between the intent and a commander's deck.

    Rewards keyword matches in EDHREC theme tags (strong signal) and in the
    names of recommended cards (weak signal).
    """
    keywords = intent.get("keywords") or []
    if not keywords:
        return 0
    score = 0
    tag_blob = " ".join(tags)
    reco_blob = " ".join(recommended).lower()
    for kw in keywords:
        if kw in tag_blob:
            score += 5
        if kw in reco_blob:
            score += 1
    return score


# Basic lands carry no commander signal, so they're never probed for discovery.
_BASIC_LANDS = {"plains", "island", "swamp", "mountain", "forest", "wastes"}


def _build_result(card: dict, data: dict, owned_keys: set, intent: dict,
                  *, owned: bool) -> dict:
    """Score a commander against the collection + intent (shared owned/unowned).

    ``owned`` flags whether the commander is in the collection; for a proposed
    (unowned) commander we also price it so its cost can count against the budget.
    """
    ordered = edhrec.extract_recommended_ordered(data)
    ordered = [r for r in ordered if _norm(r) != _norm(card["name"])]
    owned_cards_list = [r for r in ordered if _norm(r) in owned_keys]
    missing_cards = [r for r in ordered if _norm(r) not in owned_keys]
    total = len(ordered)
    num_decks = edhrec.extract_num_decks(data)
    return {
        "name": commanders.front_name(card),
        "full_name": card["name"],
        "image": scryfall.image(card),
        "color_identity": scryfall.color_identity(card),
        "num_decks": num_decks,
        "below_threshold": num_decks < settings.min_decks,
        "owned_count": len(owned_cards_list),
        "total_recommended": total,
        "pct": round(100 * len(owned_cards_list) / total, 1) if total else 0.0,
        "owned_cards": owned_cards_list,
        "missing_cards": missing_cards,
        "theme_score": _theme_score(intent, ordered, edhrec.extract_tags(data)),
        "owned": owned,
        "price_eur": None if owned else scryfall.price_eur(card),
        "link_count": 0,            # owned cards pointing here (discovery only)
        "linked_owned_cards": [],   # a sample of those cards (discovery only)
        "buylist": None,            # filled in by analyze()
    }


def _discover_unowned(owned_cards: list, owned_keys: set, intent: dict,
                      *, existing: set, client):
    """Propose commanders you don't own but that your cards point to (EDHREC).

    For a bounded sample of owned cards, look up the commanders that most play
    them, rank candidates by how many of your cards point to each, then keep the
    legal commanders that fit the requested colours and budget. Returns
    (results, stats).
    """
    budget = intent.get("budget_eur")

    # 1. Probe a bounded, deterministic sample of owned cards (skip basics).
    probe = sorted({
        c["name"] for c in owned_cards
        if c.get("name") and _norm(c["name"]) not in _BASIC_LANDS
    })[: settings.discovery_card_limit]

    # 2. Aggregate the commanders those cards are most played in.
    link_count: dict[str, int] = {}
    linked_by: dict[str, list[str]] = {}
    queried = 0
    for name in probe:
        data = edhrec.fetch_card(name, client=client)
        if data.get("_error") or data.get("_not_found"):
            continue
        queried += 1
        for cmd in edhrec.extract_top_commanders(data):
            if _norm(cmd) in existing:  # you already own this commander
                continue
            link_count[cmd] = link_count.get(cmd, 0) + 1
            linked_by.setdefault(cmd, [])
            if name not in linked_by[cmd]:
                linked_by[cmd].append(name)

    stats = {"queried_cards": queried, "candidate_count": len(link_count)}
    ranked = sorted(link_count, key=lambda n: link_count[n], reverse=True)
    ranked = ranked[: settings.discovery_pool]
    if not ranked:
        return [], stats

    # 3. Resolve candidates; keep legal commanders fitting colours + budget.
    resolved, _nf = scryfall.resolve_cards(ranked, client=client)
    discovered: list[dict] = []
    for name in ranked:
        if len(discovered) >= settings.discovery_limit:
            break
        card = resolved.get(_norm(name))
        if not card or not commanders.is_commander(card):
            continue
        if not _color_ok(card, intent.get("colors") or [], intent.get("max_colors")):
            continue
        price = scryfall.price_eur(card)
        # "Match the budget": a commander you must buy can't exceed it alone.
        if budget is not None and price is not None and price > budget:
            continue
        data = edhrec.fetch_commander(commanders.front_name(card), client=client)
        if data.get("_error") or data.get("_not_found"):
            continue
        if (edhrec.extract_num_decks(data) < settings.min_decks
                and not intent.get("include_low_decks")):
            continue
        result = _build_result(card, data, owned_keys, intent, owned=False)
        result["link_count"] = link_count[name]
        result["linked_owned_cards"] = linked_by[name][:8]
        discovered.append(result)

    # Strongest collection link first, then theme fit and popularity.
    discovered.sort(
        key=lambda r: (r["link_count"], r["theme_score"], r["owned_count"], r["num_decks"]),
        reverse=True,
    )
    return discovered, stats


def analyze(intent: dict, profile_id: int, limit: int = 12):
    """Return ranked commander suggestions for a profile's collection + intent."""
    notices = []
    fmt = intent.get("format")
    if fmt and fmt != "commander":
        notices.append(
            f"Le format « {fmt} » (60 cartes) arrive en Phase 2 — "
            "affichage des suggestions Commander en attendant."
        )

    owned_ids = db.collection_scryfall_ids(profile_id)
    owned_keys = db.owned_name_keys(profile_id)
    budget = intent.get("budget_eur")
    include_low_decks = bool(intent.get("include_low_decks"))

    with httpx.Client(timeout=30, headers={"User-Agent": settings.user_agent}) as client:
        # Resolve owned cards precisely via their Scryfall ids (ManaBox provides
        # them); fall back to name resolution for any row without an id.
        by_id = scryfall.resolve_ids(owned_ids, client=client) if owned_ids else {}
        covered = {_norm(c["name"]) for c in by_id.values()}
        named = [n for n, k, _q in db.collection_names(profile_id) if k not in covered]
        by_name, not_found = scryfall.resolve_cards(named, client=client) if named else ({}, [])

        owned_cards = list(by_id.values()) + list(by_name.values())

        # Identify the legal commanders we own (dedup by oracle/card id).
        candidates = []
        seen = set()
        for card in owned_cards:
            cid = card.get("oracle_id") or card.get("id") or card["name"]
            if cid in seen:
                continue
            seen.add(cid)
            if commanders.is_commander(card) and _color_ok(
                card, intent.get("colors") or [], intent.get("max_colors")
            ):
                candidates.append(card)

        results = []
        below_threshold = []
        not_on_edhrec = 0
        errored = []

        def process(card) -> bool:
            nonlocal not_on_edhrec
            data = edhrec.fetch_commander(commanders.front_name(card), client=client)
            if data.get("_error"):
                return False
            if data.get("_not_found"):
                not_on_edhrec += 1
                return True

            num_decks = edhrec.extract_num_decks(data)
            if num_decks < settings.min_decks:
                below_threshold.append(
                    {"name": commanders.front_name(card), "num_decks": num_decks}
                )
                # Only surface niche commanders (under the EDHREC popularity floor)
                # when the player explicitly asked for them.
                if not include_low_decks:
                    return True

            results.append(_build_result(card, data, owned_keys, intent, owned=True))
            return True

        for card in candidates:
            if not process(card):
                errored.append(card)

        for _ in range(settings.error_retry_passes):
            if not errored:
                break
            time.sleep(settings.error_retry_cooldown)
            retry, errored = errored, []
            for card in retry:
                if not process(card):
                    errored.append(card)

        # Rank: theme fit first (if any keywords), then how built you already are,
        # then raw EDHREC popularity.
        results.sort(
            key=lambda r: (r["theme_score"], r["owned_count"], r["num_decks"]),
            reverse=True,
        )
        results = results[:limit]

        # Also propose commanders you don't own but that your cards point to.
        discovery = {"queried_cards": 0, "candidate_count": 0}
        if settings.discovery_enabled:
            proposed, discovery = _discover_unowned(
                owned_cards, owned_keys, intent,
                existing=owned_keys, client=client,
            )
            # Owned suggestions first (free to build), proposed ones after.
            results = results + proposed

        # Build a budget-constrained buylist for each displayed commander. For a
        # proposed commander, its own price is reserved from the budget first and
        # folded into the deck's total acquisition cost.
        for r in results:
            sub_budget = budget
            if not r["owned"] and budget is not None and r["price_eur"] is not None:
                sub_budget = max(0.0, round(budget - r["price_eur"], 2))
            r["buylist"] = buylist.build(r["missing_cards"], sub_budget, client=client)
            commander_cost = 0.0 if r["owned"] or r["price_eur"] is None else r["price_eur"]
            r["total_cost_eur"] = round(r["buylist"]["total_eur"] + commander_cost, 2)

    return {
        "results": results,
        "intent": intent,
        "notices": notices,
        "candidate_count": len(candidates),
        "below_threshold": sorted(below_threshold, key=lambda c: c["num_decks"], reverse=True),
        "not_on_edhrec": not_on_edhrec,
        "skipped": sorted(commanders.front_name(c) for c in errored),
        "min_decks": settings.min_decks,
        "budget_eur": budget,
        "discovery": discovery,
        "proposed_count": sum(1 for r in results if not r["owned"]),
    }
