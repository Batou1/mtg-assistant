"""MTGTop8 client: Duel Commander card pools from real tournament decklists.

EDHREC aggregates 4-player casual Commander: its pages are full of group-hug,
politics and slow value cards that are bad in a 1-vs-1 game at 20 life, and
filtering them by Scryfall's ``duel`` legality only removes the banned ones.
MTGTop8 (format code ``EDH`` there) records Duel Commander tournament top-8s —
paper events (the French/Italian/Brazilian scenes) and MTGO leagues — with an
exact 100-card list per deck. That is the closest thing to "what wins in Duel".

There is no API. Three endpoints the site's own pages call are used:

- ``cEDH_card_search?n=<name>`` → the commander's internal id
  (``AddCom('ecl285', …)``);
- ``cEDH_decks?show=decks&gamerid1=<id>&meta=<window>`` → its decks, newest
  first, with the total ("150 decks / 5365");
- ``mtgo?d=<deck id>`` → one deck as plain text (``1 Card Name`` lines, the
  commander under ``Sideboard``).

``fetch_commander`` downloads a bounded sample of the newest decks and turns
the card counts into the EDHREC payload shape (``inclusion`` = number of
sampled decks running the card), plus ``deck_stats`` — the average land and
basic-land counts, which differ a lot from casual EDH (fetch/dual manabases,
lower curves) and are what deckgen sizes the manabase with.

Every name is re-checked downstream like an EDHREC page (Scryfall existence +
``duel`` legality, which also drops cards banned since the tournament).
Sentinels/caching as ``edhrec._fetch``: ``_not_found`` cached, ``_error`` never.
"""
import html
import re
from collections import Counter

import httpx

from . import db, scryfall, webfetch
from .config import settings

SOURCE = "mtgtop8"
LABEL = "MTGTop8 (tournois Duel Commander)"
_ENCODING = "iso-8859-1"

_ADDCOM_RE = re.compile(r"AddCom\('([^']+)','\d+','([^']*)'\)")
_TOTAL_RE = re.compile(r"([\d,]+)\s+decks?\s*/")
_DECK_ID_RE = re.compile(r"[?&]d=(\d+)")
_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")

_BASIC_KEYS = {
    "plains", "island", "swamp", "mountain", "forest", "wastes",
    "snow-covered plains", "snow-covered island", "snow-covered swamp",
    "snow-covered mountain", "snow-covered forest", "snow-covered wastes",
}

# Scryfall type line -> EDHREC section tag (first match wins).
_TYPE_TAGS = (
    ("land", "lands"), ("creature", "creatures"), ("planeswalker", "planeswalkers"),
    ("battle", "battles"), ("instant", "instants"), ("sorcery", "sorceries"),
    ("artifact", "utilityartifacts"), ("enchantment", "enchantments"),
)

_norm = scryfall.norm_name


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _norm(name)).strip("-")


def parse_search(page: str, name: str) -> str | None:
    """Commander id whose label matches ``name`` (front face, case-insensitive)."""
    wanted = _norm(name)
    for cid, label in _ADDCOM_RE.findall(page):
        if _norm(html.unescape(label)) == wanted:
            return cid
    return None


def parse_deck_list(page: str) -> dict:
    """``{"total": N, "ids": [...]}`` from a ``cEDH_decks`` fragment (newest first)."""
    match = _TOTAL_RE.search(page)
    ids: list[str] = []
    for did in _DECK_ID_RE.findall(page):
        if did not in ids:
            ids.append(did)
    total = int(match.group(1).replace(",", "")) if match else len(ids)
    return {"total": total, "ids": ids}


def parse_deck(text: str) -> dict:
    """MTGTop8 text export -> ``{"main": [[qty, name]], "commanders": [names]}``.

    Duel Commander exports put the commander(s) in the ``Sideboard`` section —
    a Duel deck has no sideboard otherwise.
    """
    main: list[list] = []
    side: list[str] = []
    in_side = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.lower().startswith("sideboard"):
            in_side = True
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        qty, name = int(match.group(1)), match.group(2)
        if in_side:
            side.append(name)
        else:
            main.append([qty, name])
    return {"main": main, "commanders": side}


def _cached(cache_key: str, url: str, client: httpx.Client, parse) -> dict:
    cached = db.get_edhrec(cache_key)
    if cached is not None:
        return cached
    body = webfetch.get_text(client, url, encoding=_ENCODING)
    if body is None:
        return {"_error": True}
    result = {"_not_found": True} if body == webfetch.NOT_FOUND else parse(body)
    db.set_edhrec(cache_key, result)
    return result


def find_commander_id(name: str, client: httpx.Client) -> dict:
    """``{"id": …}`` for the commander, or a sentinel."""
    front = name.split("//")[0].strip()
    url = f"{settings.mtgtop8_url}/cEDH_card_search?" + str(httpx.QueryParams(
        {"n": front, "b": "1"}))

    def parse(body: str) -> dict:
        cid = parse_search(body, front)
        return {"id": cid} if cid else {"_not_found": True}

    return _cached(f"mtgtop8-id:{_slug(front)}", url, client, parse)


def list_decks(commander_id: str, meta: str, client: httpx.Client) -> dict:
    url = f"{settings.mtgtop8_url}/cEDH_decks?" + str(httpx.QueryParams({
        "f": "EDH", "show": "decks", "cid": "", "meta": meta,
        "gamerid1": commander_id, "gamerid2": "", "cEDH_cp": "1",
    }))
    return _cached(f"mtgtop8-decks:{commander_id}:{meta}", url, client, parse_deck_list)


def fetch_deck(deck_id: str, client: httpx.Client) -> dict:
    url = f"{settings.mtgtop8_url}/mtgo?d={deck_id}"

    def parse(body: str) -> dict:
        deck = parse_deck(body)
        return deck if deck["main"] else {"_not_found": True}

    return _cached(f"mtgtop8-deck:{deck_id}", url, client, parse)


_META_ROW_RE = re.compile(
    r"<a href=archetype\?a=(\d+)[^>]*>([^<]*)</a></div>\s*<div[^>]*>(.*?)</div>"
    r"\s*<div[^>]*>\s*([\d.]+)", re.S)


def parse_metagame(page: str) -> list[dict]:
    """Commanders of a metagame breakdown, most played first.

    ``share`` is MTGTop8's per-mille of the window's decks; ``colors`` is read
    from the mana-symbol images of the row.
    """
    out = []
    for _aid, name, colors_html, share in _META_ROW_RE.findall(page):
        out.append({
            "name": html.unescape(name).strip(),
            "colors": sorted(set(re.findall(r"manas/([WUBRG])\.png", colors_html))),
            "share": float(share),
        })
    return out


def fetch_metagame(client: httpx.Client | None = None) -> list[dict]:
    """Most-played Duel Commander commanders over the first ``mtgtop8_metas``
    window (the tournament metagame) — the Duel analogue of EDHREC's theme
    pages for the commander finder. Errors yield an empty list."""
    meta = settings.mtgtop8_metas[0].strip()
    url = f"{settings.mtgtop8_url}/cEDH_decks?" + str(httpx.QueryParams({
        "f": "EDH", "show": "pop", "cid": "", "meta": meta,
        "gamerid1": "", "gamerid2": "", "cEDH_cp": "1",
    }))
    owns = client is None
    client = client or httpx.Client(timeout=30)
    try:
        data = _cached(f"mtgtop8-meta:{meta}", url, client,
                       lambda body: {"commanders": parse_metagame(body)})
    finally:
        if owns:
            client.close()
    if data.get("_error") or data.get("_not_found"):
        return []
    return data.get("commanders") or []


def _tag_for(card: dict | None) -> str:
    if card is None:
        return "cards"
    type_line = card.get("type_line") or ""
    if not type_line:
        faces = card.get("card_faces") or []
        type_line = faces[0].get("type_line", "") if faces else ""
    t = type_line.lower()
    for needle, tag in _TYPE_TAGS:
        if needle in t:
            return tag
    return "cards"


def aggregate(commander_name: str, decks: list[dict], num_decks: int,
              cards_by_name: dict) -> dict:
    """Card counts over ``decks`` -> EDHREC-shaped payload with ``deck_stats``.

    Pure (no I/O) so it is unit-testable: ``cards_by_name`` is the caller's
    Scryfall resolution of every name, used only to type the cards (sections,
    land counts). Basic lands are left out of the pool — deckgen adds basics
    itself, sized by ``deck_stats.basics``.
    """
    commander_key = _norm(commander_name)
    presence: Counter = Counter()
    display: dict[str, str] = {}
    partners: Counter = Counter()
    lands_per_deck: list[int] = []
    basics_per_deck: list[int] = []
    for deck in decks:
        lands = basics = 0
        seen: set[str] = set()
        for qty, name in deck.get("main") or []:
            key = _norm(name)
            card = cards_by_name.get(key)
            is_basic = key in _BASIC_KEYS
            if is_basic or _tag_for(card) == "lands":
                lands += qty
            if is_basic:
                basics += qty
                continue
            if key == commander_key or key in seen:
                continue
            seen.add(key)
            presence[key] += 1
            display.setdefault(key, card["name"] if card else name)
        for other in deck.get("commanders") or []:
            if _norm(other) != commander_key:
                partners[other] += 1
                presence.pop(_norm(other), None)
        lands_per_deck.append(lands)
        basics_per_deck.append(basics)

    sections: dict[str, list[dict]] = {}
    for key, count in presence.items():
        tag = _tag_for(cards_by_name.get(key))
        sections.setdefault(tag, []).append(
            {"name": display[key], "inclusion": count, "num_decks": count}
        )
    if not sections:
        return {"_not_found": True}
    cardlists = []
    for tag, views in sections.items():
        views.sort(key=lambda v: (-v["inclusion"], v["name"]))
        cardlists.append({"tag": tag, "header": tag, "cardviews": views})
    cardlists.sort(key=lambda s: s["cardviews"][0]["inclusion"], reverse=True)

    sample = len(decks)
    return {
        "_source": SOURCE,
        "num_decks": num_decks,
        "sample_size": sample,
        "deck_stats": {
            "lands": round(sum(lands_per_deck) / sample, 1) if sample else None,
            "basics": round(sum(basics_per_deck) / sample, 1) if sample else None,
        },
        "partners": [name for name, _n in partners.most_common(3)],
        "container": {"json_dict": {"cardlists": cardlists}},
    }


def fetch_commander(name: str, client: httpx.Client | None = None,
                    sample: int | None = None) -> dict:
    """Aggregated Duel Commander pool for ``name`` (sentinels as edhrec).

    Tries each ``mtgtop8_metas`` window until one has at least
    ``duel_min_decks`` results (a thin aggregate is reported as not found so
    the caller falls back to EDHREC), then downloads the newest
    ``duel_decks_sample`` lists. The aggregate itself is cached, so a commander
    costs its ~20 requests once per cache TTL. ``sample`` overrides the number
    of lists (ranking many candidates uses fewer); each deck is cached on its
    own, so a later full-size aggregate only downloads the missing ones.
    """
    sample = sample or settings.duel_decks_sample
    cache_key = f"mtgtop8:{_slug(name)}:{sample}"
    cached = db.get_edhrec(cache_key)
    if cached is not None:
        return cached

    owns = client is None
    client = client or httpx.Client(timeout=30)
    try:
        found = find_commander_id(name, client)
        if found.get("_error") or found.get("_not_found"):
            return found
        listing: dict = {"total": 0, "ids": []}
        meta_used = None
        for meta in settings.mtgtop8_metas:
            listing = list_decks(found["id"], meta.strip(), client)
            if listing.get("_error"):
                return {"_error": True}
            if listing.get("total", 0) >= settings.duel_min_decks:
                meta_used = meta.strip()
                break
        if meta_used is None:
            result = {"_not_found": True}
            db.set_edhrec(cache_key, result)
            return result

        decks = []
        for deck_id in listing["ids"][:sample]:
            deck = fetch_deck(deck_id, client)
            if deck.get("_error"):
                continue  # one blocked deck must not sink the whole aggregate
            if not deck.get("_not_found"):
                decks.append(deck)
        if len(decks) < settings.duel_min_decks:
            # Errors (not "no data") — don't cache, retry later.
            return {"_error": True}

        names = sorted({n for d in decks for _q, n in d["main"]})
        resolved, _nf = scryfall.resolve_cards(names)
        result = aggregate(name, decks, listing["total"], resolved)
        if not result.get("_not_found"):
            result["_url"] = f"{settings.mtgtop8_url}/format?f=EDH&meta={meta_used}"
        db.set_edhrec(cache_key, result)
        return result
    finally:
        if owns:
            client.close()
