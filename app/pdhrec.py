"""PDHREC client: Pauper Commander (PDH) card recommendations.

EDHREC aggregates regular Commander decks only. Filtering its page down to
commons (what the app used to do for PDH) keeps the commons that 4-player
high-power EDH decks happen to run — not what a PDH deck plays — and EDHREC has
no page at all for the non-legendary uncommon creatures that make up most of
the PDH commander pool (Mayhem Devil, Soulherder…). PDHREC (pdhrec.com) is the
EDHREC equivalent built from PDH decklists only.

There is no JSON API: pages are server-rendered HTML where each card is an
``<a class="gallery-item" popularity="…" typeline="…">`` with the card name in
the ``<img alt>``. Parsing is deliberately defensive (attribute regexes, no
layout assumptions) and this module is the single place to adjust if the
markup changes. The output reuses the EDHREC payload shape (``container →
json_dict → cardlists``) so ``deckgen``/``analysis``/``edhrec.extract_*`` work
on it unchanged — and, like an EDHREC page, every name it yields is re-checked
downstream (Scryfall existence + ``paupercommander`` legality).

Sentinels and caching follow ``edhrec._fetch``: ``{"_not_found": True}`` is
cached, ``{"_error": True}`` never is.
"""
import html
import re
import unicodedata

import httpx

from . import db, webfetch
from .config import settings

SOURCE = "pdhrec"
LABEL = "PDHREC"

_ITEM_RE = re.compile(r"<a\s([^>]*class=\"gallery-item\"[^>]*)>(.*?)</a>", re.S)
_ATTR_RE = re.compile(r'([a-z-]+)="([^"]*)"')
_ALT_RE = re.compile(r'alt="([^"]*)"')
_NUM_DECKS_RE = re.compile(r"In\s+([\d,]+)\s+decks", re.I)
_HREF_RE = re.compile(r"/commander/([^/]+)/?$")

# PDHREC typeline -> EDHREC section tag (first match wins: an artifact creature
# is a creature, a land creature is a land).
_TYPE_TAGS = (
    ("land", "lands"), ("creature", "creatures"), ("planeswalker", "planeswalkers"),
    ("battle", "battles"), ("instant", "instants"), ("sorcery", "sorceries"),
    ("artifact", "utilityartifacts"), ("enchantment", "enchantments"),
)
_TAG_HEADERS = {
    "lands": "Lands", "creatures": "Creatures", "planeswalkers": "Planeswalkers",
    "battles": "Battles", "instants": "Instants", "sorceries": "Sorceries",
    "utilityartifacts": "Artifacts", "enchantments": "Enchantments", "cards": "Cards",
}

# Colour identity -> PDHREC colour page slug (the site has no 4-colour pages).
_COLOR_PAGES = {
    "": "colorless", "W": "white", "U": "blue", "B": "black", "R": "red", "G": "green",
    "WU": "azorius", "UB": "dimir", "BR": "rakdos", "RG": "gruul", "WG": "selesnya",
    "WB": "orzhov", "UR": "izzet", "BG": "golgari", "WR": "boros", "UG": "simic",
    "WUB": "esper", "UBR": "grixis", "BRG": "jund", "WRG": "naya", "WUG": "bant",
    "WBG": "abzan", "WUR": "jeskai", "UBG": "sultai", "WBR": "mardu", "URG": "temur",
    "WUBRG": "wubrg",
}
_WUBRG = "WUBRG"


def slugify(name: str) -> str:
    """PDHREC slug: like EDHREC's (apostrophes/commas dropped, spaces hyphenated)
    but the separators are not collapsed — a double-faced card keeps both faces
    (``exdeath-void-warlock--neo-exdeath-dimensions-end``)."""
    name = unicodedata.normalize("NFKD", name.strip().lower())
    name = name.encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z0-9 -]", "", name)
    return re.sub(r" ", "-", name).strip("-")


def _front_slug(name: str) -> str:
    return slugify(name.split("//")[0])


def _tag_for(typeline: str) -> str:
    t = (typeline or "").lower()
    for needle, tag in _TYPE_TAGS:
        if needle in t:
            return tag
    return "cards"


def _items(page: str):
    """(attrs dict, card name, info text) for each gallery item of a page."""
    for attrs_txt, body in _ITEM_RE.findall(page):
        attrs = dict(_ATTR_RE.findall(attrs_txt))
        alt = _ALT_RE.search(body)
        if not alt:
            continue
        name = html.unescape(alt.group(1)).strip()
        if not name:
            continue
        info = re.search(r'<div class="info">(.*?)</div>', body, re.S)
        info_txt = re.sub(r"<[^>]+>", " ", info.group(1)) if info else ""
        yield attrs, name, html.unescape(re.sub(r"\s+", " ", info_txt)).strip()


def parse_commander_page(page: str, commander_name: str = "") -> dict:
    """PDHREC commander page -> EDHREC-shaped payload.

    ``popularity`` is the number of PDH decks of this commander running the
    card: it plays the role of EDHREC's ``inclusion``. The commander's own
    deck count is the first "In N decks" of the page (its own tile). A page
    without a single card item is reported as not found — that is what PDHREC
    renders for an unknown slug.
    """
    num_decks = 0
    match = _NUM_DECKS_RE.search(page)
    if match:
        num_decks = int(match.group(1).replace(",", ""))

    sections: dict[str, list[dict]] = {}
    seen: set[str] = set()
    cmd_key = commander_name.split("//")[0].strip().lower()
    for attrs, name, _info in _items(page):
        if "popularity" not in attrs:
            continue  # the commander tile itself / navigation tiles
        key = name.split("//")[0].strip().lower()
        if key in seen or key == cmd_key:
            continue
        seen.add(key)
        try:
            pop = int(float(attrs.get("popularity") or 0))
        except ValueError:
            pop = 0
        try:
            synergy = float(attrs.get("synergy") or 0)
        except ValueError:
            synergy = 0.0
        tag = _tag_for(attrs.get("typeline", ""))
        sections.setdefault(tag, []).append(
            {"name": name, "inclusion": pop, "num_decks": pop,
             "synergy": round(synergy, 3)}
        )
    if not sections:
        return {"_not_found": True}

    cardlists = []
    for tag, views in sections.items():
        views.sort(key=lambda v: v["inclusion"], reverse=True)
        cardlists.append({"tag": tag, "header": _TAG_HEADERS.get(tag, tag),
                          "cardviews": views})
    # Most-played section first: edhrec.extract_recommended_ordered keeps
    # section order, and the buylist prefers earlier (more characteristic) cards.
    cardlists.sort(key=lambda s: s["cardviews"][0]["inclusion"], reverse=True)
    return {
        "_source": SOURCE,
        "num_decks": num_decks,
        "container": {"json_dict": {"cardlists": cardlists}},
    }


def parse_commander_list(page: str) -> list[dict]:
    """Commander tiles of an index/colour page: ``[{name, slug, num_decks}]``."""
    out = []
    for attrs_txt, body in _ITEM_RE.findall(page):
        href = _HREF_RE.match(dict(_ATTR_RE.findall(attrs_txt)).get("href", ""))
        alt = _ALT_RE.search(body)
        if not href or not alt:
            continue
        num = _NUM_DECKS_RE.search(body)
        out.append({
            "name": html.unescape(alt.group(1)).strip(),
            "slug": href.group(1),
            "num_decks": int(num.group(1).replace(",", "")) if num else 0,
        })
    return out


_STAPLE_RE = re.compile(
    r'<a\s+class="gallery-item"\s+color-identity=([A-Z]*)\s*>(.*?)</a>', re.S)
_PCT_RE = re.compile(r":\s*([\d.]+)%")


def parse_staples(page: str) -> list[dict]:
    """PDHREC's format-wide staples: ``[{name, color_identity, pct}]``.

    ``pct`` is the share of PDH decks (able to play it) that run the card.
    The page carries no type line, unlike commander pages.
    """
    out = []
    for identity, body in _STAPLE_RE.findall(page):
        alt = _ALT_RE.search(body)
        if not alt:
            continue
        pct = _PCT_RE.search(body)
        out.append({
            "name": html.unescape(alt.group(1)).strip(),
            "color_identity": sorted(identity),
            "pct": float(pct.group(1)) if pct else 0.0,
        })
    return out


def fetch_staples(client: httpx.Client | None = None) -> list[dict]:
    """Format-wide PDH staples (cached; errors yield an empty list)."""
    owns = client is None
    client = client or httpx.Client(timeout=30)
    try:
        data = _cached_page("pdhrec:staples", f"{settings.pdhrec_url}/staples", client,
                            lambda body: {"staples": parse_staples(body)})
    finally:
        if owns:
            client.close()
    if data.get("_error") or data.get("_not_found"):
        return []
    return data.get("staples") or []


def _cached_page(cache_key: str, url: str, client: httpx.Client, parse) -> dict:
    cached = db.get_edhrec(cache_key)
    if cached is not None:
        return cached
    body = webfetch.get_text(client, url)
    if body is None:
        return {"_error": True}
    result = {"_not_found": True} if body == webfetch.NOT_FOUND else parse(body)
    db.set_edhrec(cache_key, result)
    return result


def fetch_commander(name: str, client: httpx.Client | None = None) -> dict:
    """The commander's PDH recommendations (EDHREC-shaped; sentinels as edhrec).

    Tries the front-face slug first (what PDHREC uses for most cards), then
    the full double-faced name, which PDHREC keeps for some DFCs.
    """
    owns = client is None
    client = client or httpx.Client(timeout=30)
    try:
        slugs = [_front_slug(name)]
        if "//" in name and slugify(name) not in slugs:
            slugs.append(slugify(name))
        result: dict = {"_not_found": True}
        for slug in slugs:
            result = _cached_page(
                f"pdhrec:{slug}", f"{settings.pdhrec_url}/commander/{slug}/", client,
                lambda body: parse_commander_page(body, name),
            )
            if not result.get("_not_found"):
                break
        if not result.get("_not_found") and not result.get("_error"):
            result = {**result, "_url": f"{settings.pdhrec_url}/commander/{slugs[0]}/"}
        return result
    finally:
        if owns:
            client.close()


def color_page(colors) -> str | None:
    """PDHREC colour page slug for an exact colour identity (None: no page)."""
    key = "".join(c for c in _WUBRG if c in set(colors or []))
    return _COLOR_PAGES.get(key)


def fetch_commanders(colors=None, *, pages: int = 2,
                     client: httpx.Client | None = None) -> list[dict]:
    """Most-played PDH commanders, optionally for one exact colour identity.

    PDHREC ranks its index and colour pages by deck count (24 per page);
    ``pages`` bounds how deep we go. Errors yield whatever was fetched.
    """
    base = settings.pdhrec_url
    root = f"{base}/colors/{color_page(colors)}" if colors else base
    if colors and color_page(colors) is None:
        return []
    owns = client is None
    client = client or httpx.Client(timeout=30)
    out: list[dict] = []
    try:
        for page in range(1, pages + 1):
            url = f"{root}/" if page == 1 else f"{root}/{page}/"
            key = "pdhrec-list:" + url.removeprefix(base)
            data = _cached_page(key, url, client,
                                lambda body: {"commanders": parse_commander_list(body)})
            if data.get("_error") or data.get("_not_found"):
                break
            rows = data.get("commanders") or []
            if not rows:
                break
            out.extend(rows)
    finally:
        if owns:
            client.close()
    return out
