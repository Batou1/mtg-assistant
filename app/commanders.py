"""Determine whether a Scryfall card can legally be a Commander."""
from . import duelbans, prices, scryfall

# Commander variants analysis.py / deckgen.py build for. "commander" is the
# default 100-card multiplayer format (EDHREC data); the other two have their
# own recommendation sources (app/variants.py: PDHREC, MTGTop8) and every card
# is still filtered by the variant's Scryfall legality (SCRYFALL_LEGALITY).
FORMATS = {"commander", "duelcommander", "paupercommander"}

# Scryfall legality key enforced on the 99 (deckgen/analysis) and on a Duel
# commander — Duel Commander bans a handful of cards legal in normal
# Commander; Pauper Commander's 99 must be commons. Plain "commander" has no
# entry: its banned list is short enough that the app doesn't enforce it today
# (see analyze() in analysis.py).
SCRYFALL_LEGALITY = {"duelcommander": "duel", "paupercommander": "paupercommander"}


def _faces(card: dict):
    return card.get("card_faces") or []


def is_commander(card: dict) -> bool:
    """True if the card is a legal commander.

    Covers legendary creatures and any card whose rules text says it
    "can be your commander" (planeswalker-commanders, Backgrounds, etc.).
    """
    type_line = card.get("type_line", "") or ""
    oracle_text = card.get("oracle_text", "") or ""

    texts = [type_line, oracle_text]
    for face in _faces(card):
        texts.append(face.get("type_line", "") or "")
        texts.append(face.get("oracle_text", "") or "")

    blob = " ".join(texts).lower()
    if "can be your commander" in blob:
        return True

    if "legendary" in type_line.lower() and "creature" in type_line.lower():
        return True

    for face in _faces(card):
        face_type = (face.get("type_line", "") or "").lower()
        if "legendary" in face_type and "creature" in face_type:
            return True

    return False


def _front_type(card: dict) -> str:
    faces = _faces(card)
    if faces and faces[0].get("type_line"):
        return faces[0]["type_line"].lower()
    return (card.get("type_line") or "").split("//")[0].lower()


def is_pdh_commander(card: dict) -> bool:
    """True if the card can be a Pauper Commander commander.

    PDH's rule is NOT "legendary": the commander is any creature (or a
    Background, for "Choose a Background" pairs) with an UNCOMMON printing in
    paper or on MTGO — Mayhem Devil, Soulherder… Scryfall's ``paupercommander``
    legality does not encode this (it marks uncommons ``not_legal``, since they
    are illegal in the 99), so the rarity comes from the printing index built
    during the bulk import (``prices.printed_rarities``), falling back to the
    canonical printing's own rarity without it. Only an explicit ``banned``
    status rejects the card.
    """
    if (card.get("legalities") or {}).get("paupercommander") == "banned":
        return False
    front = _front_type(card)
    if "creature" not in front and "background" not in front:
        return False
    if card.get("rarity") == "uncommon":
        return True
    return "uncommon" in prices.printed_rarities(card.get("name") or "")


def is_eligible_commander(card: dict, fmt: str = "commander") -> bool:
    """True if the card can be a commander in ``fmt`` (one of FORMATS).

    Combines the structural check (legendary creature / "can be your
    commander") with the format's Scryfall legality when one is enforced
    (Duel Commander's banned list) and Duel Commander's official "banned as
    commander" list. Pauper Commander has its own rule (:func:`is_pdh_commander`).
    """
    if fmt == "paupercommander":
        return is_pdh_commander(card)
    if not is_commander(card):
        return False
    # legal_in() counts Scryfall's "restricted" as legal (true for the 99):
    # the committee bans these only from the command zone (app/duelbans.py).
    if fmt == "duelcommander" and duelbans.is_banned_as_commander(card.get("name", ""), card):
        return False
    legality_key = SCRYFALL_LEGALITY.get(fmt)
    return legality_key is None or scryfall.legal_in(card, legality_key)


def front_name(card: dict) -> str:
    """The face name used for display and EDHREC slug lookup."""
    return card["name"].split("//")[0].strip()
