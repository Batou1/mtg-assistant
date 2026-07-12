"""EDHREC client: fetch a commander's page JSON and extract popularity + cards.

EDHREC has no official API; this uses the same json.edhrec.com endpoints the
site itself consumes. Responses are cached in SQLite and requests are throttled.
Extraction is intentionally defensive: if EDHREC changes their payload shape,
this module is the single place to adjust.
"""
import random
import re
import time
import unicodedata

import httpx

from . import db
from .config import settings

_NUM_DECKS_RE = re.compile(r"([\d,]+)\s+decks", re.I)

# EDHREC sits behind Cloudflare and rejects non-browser-looking clients with a
# 403. Send realistic browser headers so our requests are accepted.
_EDHREC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://edhrec.com/",
    "Origin": "https://edhrec.com",
}

_RETRY_STATUS = {403, 429, 500, 502, 503, 504}


def slugify(name: str) -> str:
    """Convert a commander name to its EDHREC URL slug.

    EDHREC drops apostrophes entirely (K'rrik -> krrik, Life's -> lifes) rather
    than turning them into separators, so strip them before hyphenating the
    remaining non-alphanumeric runs.
    """
    name = name.split("//")[0].strip().lower()
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.replace("'", "")
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


def _s3_not_found(resp: httpx.Response) -> bool:
    """True if this 403 is S3/CloudFront's way of saying "no such page".

    EDHREC's JSON lives on S3 behind CloudFront: a slug with no page surfaces
    as a 403 "AccessDenied" XML from AmazonS3, not a 404. Detecting it lets a
    nonexistent slug be treated (and cached) as not-found instead of being
    retried as if Cloudflare were blocking us.
    """
    if resp.status_code != 403:
        return False
    server = resp.headers.get("server", "").lower()
    via = resp.headers.get("via", "").lower()
    if "amazons3" not in server and "cloudfront" not in via:
        return False
    return b"<Code>AccessDenied</Code>" in resp.content[:512]


def _get_with_retry(client: httpx.Client, url: str, attempts: int = 5):
    """GET ``url`` with backoff on transient/blocking responses.

    Returns the parsed JSON dict, the sentinel "_not_found" for a missing page
    (404, or S3's AccessDenied 403), or None if every attempt failed (caller
    treats this as a skippable error).
    """
    delay = max(settings.request_delay, 0.25)
    for attempt in range(attempts):
        try:
            resp = client.get(url, headers=_EDHREC_HEADERS)
        except httpx.RequestError:
            time.sleep(delay + random.uniform(0, 0.3))
            delay = min(delay * 2, 8)
            continue

        if resp.status_code == 404 or _s3_not_found(resp):
            time.sleep(settings.request_delay)
            return "_not_found"

        if resp.status_code in _RETRY_STATUS:
            if attempt == attempts - 1:
                break
            retry_after = resp.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.isdigit() else delay
            time.sleep(wait + random.uniform(0, 0.4))
            delay = min(delay * 2, 8)
            continue

        resp.raise_for_status()
        time.sleep(settings.request_delay + random.uniform(0, 0.25))
        return resp.json()

    return None


def _fetch(cache_key: str, url: str, client: httpx.Client | None) -> dict:
    """Cached GET of an EDHREC JSON page.

    Sentinels: {"_not_found": True} (no such page — cached, it won't appear
    later) or {"_error": True} (request blocked/failed after retries — NOT
    cached, so a later run retries).
    """
    cached = db.get_edhrec(cache_key)
    if cached is not None:
        return cached

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=30)
    try:
        result = _get_with_retry(client, url)
    finally:
        if owns_client:
            client.close()

    if result is None:
        return {"_error": True}
    if result == "_not_found":
        result = {"_not_found": True}
    db.set_edhrec(cache_key, result)
    return result


def fetch_commander(name: str, client: httpx.Client | None = None) -> dict:
    """Return the commander's EDHREC JSON (sentinels: see :func:`_fetch`).

    EDHREC only has pages for regular (paper) Commander — there is no separate
    Duel Commander or Pauper Commander section, so this is also the data source
    for those variants; see commanders.py / analysis.py for how the Scryfall
    legality of the commander and its recommended cards is filtered per variant
    on top of this same page.
    """
    slug = slugify(name)
    return _fetch(slug, f"{settings.edhrec_json}/{slug}.json", client)


def fetch_card(name: str, client: httpx.Client | None = None) -> dict:
    """Return EDHREC's per-card page JSON (lists commanders that play the card).

    Cached under a ``card:`` slug prefix so a card page never collides with the
    commander page of a legendary creature sharing its slug.
    """
    slug = slugify(name)
    return _fetch(f"card:{slug}", f"{settings.edhrec_cards_json}/{slug}.json", client)


def fetch_theme(slug: str, client: httpx.Client | None = None) -> dict:
    """Return an EDHREC tag page's JSON (top commanders of a theme).

    Both strategy themes ("aristocrats", "group-hug"…) and creature types
    ("zombies", "dragons"…) live under the same tags section; cached under a
    ``theme:`` slug prefix.
    """
    slug = slugify(slug)
    return _fetch(f"theme:{slug}", f"{settings.edhrec_tags_json}/{slug}.json", client)


def _json_dict(data: dict) -> dict:
    return (data.get("container", {}) or {}).get("json_dict", {}) or {}


def extract_top_commanders(data: dict) -> list[str]:
    """Commander names a card or theme page lists (most-associated first).

    EDHREC card pages carry a "Top Commanders" card list; we match it by tag or
    header so a payload-shape tweak (e.g. ``topcommanders`` vs ``commanders``)
    keeps working. Theme (tag) pages also carry a small "New Commanders" list:
    it is skipped entirely — freshly printed commanders shouldn't surface just
    for being new (the user asks for a theme, not novelty), and a genuinely
    popular new commander appears in the "top…" list anyway.
    """
    sections = []
    for section in _json_dict(data).get("cardlists") or []:
        tag = (section.get("tag") or "").lower()
        header = (section.get("header") or "").lower()
        if "new" in tag or "new" in header:
            continue
        if "commander" in tag or "commander" in header:
            sections.append((("top" not in tag and "top" not in header), section))
    sections.sort(key=lambda pair: pair[0])  # stable: "top…" sections first

    names: list[str] = []
    seen: set[str] = set()
    for _, section in sections:
        for view in section.get("cardviews", []) or []:
            name = view.get("name")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def extract_num_decks(data: dict) -> int:
    """Best-effort number of decks built around this commander."""
    if isinstance(data.get("num_decks"), int):
        return data["num_decks"]

    card = _json_dict(data).get("card") or {}
    if isinstance(card.get("num_decks"), int):
        return card["num_decks"]

    for field in ("description", "header"):
        text = data.get(field) or ""
        match = _NUM_DECKS_RE.search(text)
        if match:
            return int(match.group(1).replace(",", ""))

    return 0


def extract_recommended_ordered(data: dict) -> list[str]:
    """Recommended card names in EDHREC's order (most synergistic first).

    Order matters for the buylist: earlier cards are more characteristic of the
    deck, so we prefer buying those when the budget is tight.
    """
    names: list[str] = []
    seen: set[str] = set()
    for section in _json_dict(data).get("cardlists") or []:
        for view in section.get("cardviews", []) or []:
            name = view.get("name")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def extract_tags(data: dict) -> list[str]:
    """Theme/tag slugs associated with the commander (best-effort)."""
    tags: list[str] = []
    panels = (data.get("panels") or {})
    for tag in panels.get("taglinks", []) or []:
        slug = tag.get("slug") or tag.get("value")
        if slug:
            tags.append(str(slug).lower())
    return tags
