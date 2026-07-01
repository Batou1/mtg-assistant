"""Search the full local Scryfall card pool for cards matching gameplay
criteria — colours, type, ability keywords, oracle text, mana value,
legality.

This is what lets deck-building surface real synergy cards ("des cartes qui
sacrifient des créatures en mono-noir") beyond what EDHREC's popularity data
happens to list or what an LLM recalls from training — every result is an
actual card from the local database (see app/bulk_data.py), not a guess to be
validated after the fact.

Built on the ``oracle_cards`` import: one canonical printing per card, cached
in memory per process (rebuilt only when a newer oracle_cards bulk import
lands, tracked via the same meta timestamp app/bulk_data.py writes).
"""
from . import db, scryfall

_cache: list[dict] | None = None
_cache_token: str | None = None


def _all_cards() -> list[dict]:
    global _cache, _cache_token
    token = db.get_meta("bulk_oracle_cards_synced_at")
    if _cache is None or token != _cache_token:
        by_id: dict[str, dict] = {}
        for card in db.all_oracle_cards():
            if card.get("layout") in scryfall.NON_GAME_LAYOUTS:
                continue
            cid = card.get("id")
            if cid:
                by_id[cid] = card
        _cache = list(by_id.values())
        _cache_token = token
    return _cache


def _text(card: dict, field: str) -> str:
    """A card's ``field`` (type_line/oracle_text), joined across faces for
    double-faced/split/adventure cards when there's no combined top-level value."""
    direct = card.get(field)
    if direct:
        return direct
    faces = card.get("card_faces") or []
    return " // ".join(f.get(field, "") for f in faces if f.get(field))


def _norm_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [v.lower() for v in value if isinstance(v, str) and v]


def _any_contains(haystack: str, needles: list[str]) -> bool:
    return any(n in haystack for n in needles)


def _color_identity_ok(card: dict, colors: list[str] | None, exact: bool) -> bool:
    identity = set(card.get("color_identity") or [])
    if not colors:
        return True
    allowed = set(colors)
    return identity == allowed if exact else identity <= allowed


def search(
    colors: list[str] | None = None,
    exact_colors: bool = False,
    type_contains=None,
    keywords=None,
    text_contains=None,
    legal_in: str | None = None,
    min_cmc: float | None = None,
    max_cmc: float | None = None,
    exclude_names=None,
    limit: int = 40,
) -> list[dict]:
    """Return up to ``limit`` cards matching every given criterion.

    ``type_contains``/``text_contains``/``keywords`` each accept a string or a
    list of strings; a card matching ANY one of them satisfies that
    criterion. ``type_contains`` matches the type line (e.g. "Creature",
    "Zombie"); ``keywords`` matches Scryfall's own ability-keyword list
    (Flying, Trample, Proliferate…); ``text_contains`` substring-matches the
    oracle text — the way to search a *theme* that isn't a formal keyword
    (e.g. "sacrifice a creature", "return...from your graveyard").

    Results are real cards straight from the local database: safe to name
    directly once returned here.
    """
    exclude = {n.lower() for n in (exclude_names or ())}
    wanted_types = _norm_list(type_contains)
    wanted_kw = set(_norm_list(keywords))
    wanted_text = _norm_list(text_contains)

    out = []
    for card in _all_cards():
        name = card.get("name") or ""
        if not name or name.lower() in exclude:
            continue
        if not _color_identity_ok(card, colors, exact_colors):
            continue
        if legal_in and not scryfall.legal_in(card, legal_in):
            continue
        cmc = card.get("cmc")
        if min_cmc is not None and (cmc is None or cmc < min_cmc):
            continue
        if max_cmc is not None and (cmc is None or cmc > max_cmc):
            continue
        if wanted_types and not _any_contains(_text(card, "type_line").lower(), wanted_types):
            continue
        if wanted_kw and not (wanted_kw & {k.lower() for k in (card.get("keywords") or [])}):
            continue
        if wanted_text and not _any_contains(_text(card, "oracle_text").lower(), wanted_text):
            continue
        out.append(card)

    out.sort(key=lambda c: ((c.get("cmc") or 0), c.get("name") or ""))
    return out[:limit]
