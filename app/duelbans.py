"""Duel Commander's "banned as commander" list (official site).

Duel Commander bans some cards only from the command zone: they stay legal in
the 99 (Edgar Markov, Derevi, Yuriko…). Scryfall shows them ``restricted`` in
``duel``, which ``scryfall.legal_in`` counts as legal (right for the 99), so
the commander check used to accept them. The authoritative list is the Duel Commander committee's banlist page
(duelcommander.org/banlist), where each entry of that section is a
``<div class="ban-item … commander-restricted" data-card-name="…">``.

Freshness: the page is cached like an EDHREC page (``cache_ttl_days``); the
last successfully parsed list is also kept in ``meta`` with no expiry, so an
outage or a markup change keeps enforcing the last known list instead of
silently lifting every ban. Without any copy (first start offline), Scryfall's
``duel: "restricted"`` status (its encoding of the same list, possibly a few
days late) is used instead. The parsed set is memoised in
process: ``commanders.is_eligible_commander`` runs inside loops over the whole
card pool, and a failed fetch is not retried for ``_RETRY_AFTER`` seconds.
"""
import html
import re
import sqlite3
import threading
import time

import httpx

from . import db, scryfall, webfetch
from .config import settings

_CACHE_KEY = "duelbans:page"
_META_KEY = "duel_banned_as_commander"
_RETRY_AFTER = 600.0

_ITEM_RE = re.compile(
    r'<div\s+class="[^"]*\bcommander-restricted\b[^"]*"[^>]*\bdata-card-name="([^"]*)"')
_SECTION_RE = re.compile(r'id="banned-commanders"(.*?)</section>', re.S)
_NAME_RE = re.compile(r'data-card-name="([^"]*)"')
_UPDATED_RE = re.compile(r"Last Updated:\s*</strong>\s*([^<]+?)\s*<", re.S)

_lock = threading.Lock()
_memo: dict | None = None      # {"names": [...], "updated": str}
_memo_at = 0.0


def _unescape(text: str) -> str:
    # The site double-escapes some names ("Minsc &amp;amp; Boo").
    prev = None
    while prev != text:
        prev, text = text, html.unescape(text)
    return text.strip()


def parse(page: str) -> dict | None:
    """``{"names": [...], "updated": "July 27, 2026"}``, or None when the page
    has no recognisable banned-as-commander section (markup changed)."""
    names = _ITEM_RE.findall(page)
    if not names:
        section = _SECTION_RE.search(page)
        names = _NAME_RE.findall(section.group(1)) if section else []
    if not names:
        return None
    updated = _UPDATED_RE.search(page)
    return {
        "names": sorted({_unescape(n) for n in names}),
        "updated": updated.group(1).strip() if updated else "",
    }


def _load(client: httpx.Client | None) -> dict:
    cached = db.get_edhrec(_CACHE_KEY)
    if cached is not None:
        return cached
    owns = client is None
    client = client or httpx.Client(timeout=30)
    try:
        body = webfetch.get_text(client, settings.duel_banlist_url)
    finally:
        if owns:
            client.close()
    parsed = parse(body) if body and body != webfetch.NOT_FOUND else None
    if parsed:
        db.set_edhrec(_CACHE_KEY, parsed)
        db.set_meta(_META_KEY, _dump(parsed))
        return parsed
    last = db.get_meta(_META_KEY)
    return _undump(last) if last else {"names": [], "updated": "", "_unavailable": True}


def _dump(data: dict) -> str:
    return data.get("updated", "") + "\n" + "\n".join(data["names"])


def _undump(raw: str) -> dict:
    updated, _, rest = raw.partition("\n")
    return {"names": [n for n in rest.split("\n") if n], "updated": updated}


def banlist(client: httpx.Client | None = None) -> dict:
    """The current list (memoised; see the module docstring)."""
    global _memo, _memo_at
    with _lock:
        now = time.time()
        ttl = settings.cache_ttl_days * 86400
        stale = _memo is None or now - _memo_at > (
            _RETRY_AFTER if _memo.get("_unavailable") else ttl)
        if stale:
            try:
                _memo = _load(client)
            except sqlite3.OperationalError:  # schema not initialised (bare tests)
                _memo = {"names": [], "updated": "", "_unavailable": True}
            _memo_at = now
            _memo["keys"] = {scryfall.norm_name(n) for n in _memo["names"]}
        return _memo


def clear_cache() -> None:
    global _memo, _memo_at
    with _lock:
        _memo, _memo_at = None, 0.0


def is_banned_as_commander(name: str, card: dict | None = None) -> bool:
    """True if ``name`` may not lead a Duel Commander deck (front-face match).

    The official list wins whenever we have one (it reflects unbans the day
    they're announced). Without any copy, Scryfall's ``duel: "restricted"``
    — its encoding of "banned as commander" — is the offline fallback, when
    the caller passes the ``card``.
    """
    data = banlist()
    if data.get("_unavailable"):
        return bool(card) and (card.get("legalities") or {}).get("duel") == "restricted"
    return scryfall.norm_name(name or "") in data["keys"]
