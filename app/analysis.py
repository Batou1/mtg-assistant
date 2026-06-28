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


def _color_ok(card: dict, wanted: list[str]) -> bool:
    """A commander fits if its color identity is within the requested colors."""
    if not wanted:
        return True
    identity = set(scryfall.color_identity(card))
    return identity.issubset(set(wanted))


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


def analyze(intent: dict, limit: int = 12):
    """Return ranked commander suggestions for the stored collection + intent."""
    notices = []
    fmt = intent.get("format")
    if fmt and fmt != "commander":
        notices.append(
            f"Le format « {fmt} » (60 cartes) arrive en Phase 2 — "
            "affichage des suggestions Commander en attendant."
        )

    owned_ids = db.collection_scryfall_ids()
    owned_keys = db.owned_name_keys()
    budget = intent.get("budget_eur")

    with httpx.Client(timeout=30, headers={"User-Agent": settings.user_agent}) as client:
        # Resolve owned cards precisely via their Scryfall ids (ManaBox provides
        # them); fall back to name resolution for any row without an id.
        by_id = scryfall.resolve_ids(owned_ids, client=client) if owned_ids else {}
        covered = {_norm(c["name"]) for c in by_id.values()}
        named = [n for n, k, _q in db.collection_names() if k not in covered]
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
            if commanders.is_commander(card) and _color_ok(card, intent.get("colors") or []):
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
                return True

            ordered = edhrec.extract_recommended_ordered(data)
            ordered = [r for r in ordered if _norm(r) != _norm(card["name"])]
            owned_cards_list = [r for r in ordered if _norm(r) in owned_keys]
            missing_cards = [r for r in ordered if _norm(r) not in owned_keys]
            total = len(ordered)
            tags = edhrec.extract_tags(data)

            results.append(
                {
                    "name": commanders.front_name(card),
                    "full_name": card["name"],
                    "image": scryfall.image(card),
                    "color_identity": scryfall.color_identity(card),
                    "num_decks": num_decks,
                    "owned_count": len(owned_cards_list),
                    "total_recommended": total,
                    "pct": round(100 * len(owned_cards_list) / total, 1) if total else 0.0,
                    "owned_cards": owned_cards_list,
                    "missing_cards": missing_cards,
                    "theme_score": _theme_score(intent, ordered, tags),
                    "buylist": None,  # filled in below for displayed commanders
                }
            )
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

        # Build a budget-constrained buylist for each displayed commander.
        for r in results:
            r["buylist"] = buylist.build(r["missing_cards"], budget, client=client)

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
    }
