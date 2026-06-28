"""Parse pasted / uploaded decklist text into normalized (name, quantity) pairs.

Handles the common MTG export formats:
    1 Sol Ring
    1x Atraxa, Praetors' Voice
    2 Lightning Bolt (M10) 146
    Sol Ring
    # comments / // comments and "Creatures (30)" style headers are ignored.
"""
import re

_QTY_RE = re.compile(r"^\s*(\d+)\s*[xX]?\s+(.*)$")
# Trailing set + collector number, e.g. " (M10) 146" or " (DOM)".
_SET_RE = re.compile(r"\s+\([A-Za-z0-9]{2,6}\)(?:\s+[\dA-Za-z\-]+)?\s*$")
_FOIL_RE = re.compile(r"\s*\*[FfEe]\*\s*$")
_TAG_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
_HEADER_COUNT_RE = re.compile(r"\(\d+\)\s*$")
_SECTION_RE = re.compile(
    r"^(sideboard|commander|deck|maybeboard|companion|about|name)\b", re.I
)
_COMMENT_PREFIXES = ("#", "//")


def _clean_name(name: str) -> str:
    name = _SET_RE.sub("", name).strip()
    name = _FOIL_RE.sub("", name).strip()
    name = _TAG_RE.sub("", name).strip()
    return name


def parse_decklist(text: str):
    """Return (items, unrecognized).

    items: list of (name, quantity) preserving first-seen order, quantities summed.
    unrecognized: list of raw lines that could not be interpreted as a card.
    """
    items: dict[str, list] = {}
    order: list[str] = []
    unrecognized: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(_COMMENT_PREFIXES):
            continue

        # Drop sideboard markers like "SB: 1 Card".
        stripped = re.sub(r"^SB:\s*", "", stripped, flags=re.I)

        match = _QTY_RE.match(stripped)
        if match:
            quantity = int(match.group(1))
            name = match.group(2).strip()
        else:
            # A bare line: ignore obvious section/category headers, treat the
            # rest as a single copy of a card.
            if _HEADER_COUNT_RE.search(stripped):
                continue
            if _SECTION_RE.match(stripped) and len(stripped.split()) <= 2:
                continue
            quantity = 1
            name = stripped

        name = _clean_name(name)
        if not name:
            unrecognized.append(line.strip())
            continue

        key = name.lower()
        if key not in items:
            items[key] = [name, 0]
            order.append(key)
        items[key][1] += quantity

    result = [(items[k][0], items[k][1]) for k in order]
    return result, unrecognized
