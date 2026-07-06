"""Determine whether a Scryfall card can legally be a Commander."""
from . import scryfall

# EDHREC-backed Commander variants analysis.py can suggest commanders for.
# "commander" is the default 100-card multiplayer format; EDHREC has no
# dedicated section for the other two, so they reuse plain Commander's own
# EDHREC page, filtered down to what's actually legal in the variant via
# SCRYFALL_LEGALITY below (see analysis.py / deckgen.py).
FORMATS = {"commander", "duelcommander", "paupercommander"}

# Scryfall legality key to enforce for a commander candidate, beyond the
# structural is_commander() check — e.g. Duel Commander bans a handful of
# cards legal in normal Commander, and Pauper Commander only allows a small
# set of (mostly uncommon) legendary creatures as commander. Plain "commander"
# has no entry: its banned list is short enough that the app doesn't enforce
# it today (see analyze() in analysis.py).
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


def is_eligible_commander(card: dict, fmt: str = "commander") -> bool:
    """True if the card can be a commander in ``fmt`` (one of FORMATS).

    Combines the structural check (legendary creature / "can be your
    commander") with the format's Scryfall legality when one is enforced
    (Duel Commander's banned list, Pauper Commander's rarity restriction).
    """
    if not is_commander(card):
        return False
    legality_key = SCRYFALL_LEGALITY.get(fmt)
    return legality_key is None or scryfall.legal_in(card, legality_key)


def front_name(card: dict) -> str:
    """The face name used for display and EDHREC slug lookup."""
    return card["name"].split("//")[0].strip()
