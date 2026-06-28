"""Determine whether a Scryfall card can legally be a Commander."""


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


def front_name(card: dict) -> str:
    """The face name used for display and EDHREC slug lookup."""
    return card["name"].split("//")[0].strip()
