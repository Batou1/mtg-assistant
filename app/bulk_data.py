"""Load Scryfall's bulk-data exports into the local card cache.

Scryfall publishes full-database dumps (docs: https://scryfall.com/docs/api/bulk-data)
that are refreshed roughly once a day. Importing them lets ``app.scryfall``
resolve almost every card from SQLite instead of the live API — the API is
only hit for genuine cache misses (a card added since the last import, or an
exact printing/language the import didn't cover).

Two exports are used, for different keys of the same ``cards`` table that
``app.scryfall`` already reads via ``db.get_card``:

- ``oracle_cards`` (~180 MB): one row per Oracle ID, Scryfall's own pick of
  "the most up-to-date recognizable version" of each card. This is what
  registers the ``name_key`` entries — it reproduces what a live
  ``/cards/collection`` lookup by name used to return, without us having to
  re-implement "which printing is canonical" ourselves.
- ``all_cards`` (~2.5 GB, every printing in every language): registers
  ``id:<uuid>`` entries only, so an exact printing (including a foreign-
  language card from a ManaBox import) resolves without a live call.

``unique_artwork`` is deliberately not used: every card object in the two
exports above already carries ``image_uris`` for its own printing, which is
all ``scryfall.image()`` needs. A separate artwork fetch would only matter for
a gallery of alternate arts per card, which this app doesn't have.
"""
import gzip
import io
import json
import logging
import os
import time
from datetime import datetime

import httpx

from . import collection, db, prices, scryfall
from .config import settings

logger = logging.getLogger(__name__)

BULK_TYPES = ("oracle_cards", "all_cards")
_BATCH_SIZE = 5000
# Re-check hourly so a missed 24h window (e.g. the laptop was asleep) is
# caught soon after wake, without hammering Scryfall on every check.
_POLL_INTERVAL_SECONDS = 3600


def _key(name: str) -> str:
    return name.strip().lower()


def _bulk_object(client: httpx.Client, bulk_type: str) -> dict:
    resp = client.get(f"{settings.scryfall_api}/bulk-data/{bulk_type}")
    resp.raise_for_status()
    return resp.json()


class _IterReader(io.RawIOBase):
    """Adapt an iterator of bytes chunks (an httpx streaming body) to a
    file-like object, so it can be wrapped by ``gzip.GzipFile`` without ever
    holding the whole (multi-hundred-MB) response in memory."""

    def __init__(self, chunks):
        self._chunks = chunks
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b):
        if not self._buf:
            self._buf = next(self._chunks, b"")
        n = len(b)
        chunk, self._buf = self._buf[:n], self._buf[n:]
        b[: len(chunk)] = chunk
        return len(chunk)


def _iter_remote_jsonl(client: httpx.Client, download_uri: str):
    """Yield parsed card dicts from a ``.jsonl.gz`` bulk-data export, streamed
    straight off the network — no intermediate file, no full-file buffering."""
    with client.stream("GET", download_uri, timeout=120) as resp:
        resp.raise_for_status()
        raw = _IterReader(resp.iter_bytes())
        with gzip.GzipFile(fileobj=raw) as gz:
            for line in io.TextIOWrapper(gz, encoding="utf-8"):
                line = line.strip()
                if line:
                    yield json.loads(line)


def iter_local_jsonl(path: str):
    """Yield parsed card dicts from an already-downloaded (uncompressed)
    ``.jsonl`` bulk-data file, e.g. one fetched by hand from
    https://scryfall.com/docs/api/bulk-data."""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def _import_oracle_cards(cards) -> int:
    """Register ``name_key`` (+ front-face name for DFCs) for each card.

    Skips non-game layouts (see ``scryfall.NON_GAME_LAYOUTS``): Scryfall's
    oracle_cards export isn't limited to real spells/permanents — it also
    includes one entry per Oracle ID for Art Series cards, tokens, emblems,
    etc. Those routinely reuse a real card's display name (e.g. an Art Series
    "Sol Ring // Sol Ring") without being that card — registering them under
    name_key would silently clobber the real card's cache entry.

    Also skips digital-only printings (see ``scryfall.is_paper``): Scryfall's
    canonical pick for a card is sometimes its MTGO printing (Vintage Masters,
    Masters Edition…), which carries no Cardmarket price. Caching that under
    the card's name would make it look free forever. Leaving the name
    unregistered sends the first lookup to the live API, where
    ``scryfall._ensure_paper`` resolves and caches a real paper printing.
    """
    batch: list[tuple[str, dict]] = []
    total = 0
    for card in cards:
        if card.get("layout") in scryfall.NON_GAME_LAYOUTS:
            continue
        if not scryfall.is_paper(card):
            continue
        name = card.get("name") or ""
        if not name:
            continue
        batch.append((_key(name), card))
        if "//" in name:
            batch.append((_key(name.split("//")[0]), card))
        if len(batch) >= _BATCH_SIZE:
            total += db.bulk_set_cards(batch)
            batch = []
    total += db.bulk_set_cards(batch)
    return total


def _import_all_cards(cards) -> int:
    """Register ``id:<uuid>`` only — exact-printing lookups (resolve_ids).

    Also builds ``app.prices``' cheapest-printing index from the same pass: it
    is the one place in the app that sees every printing of every card, and
    re-reading half a million cached rows afterwards just to compare prices
    would be pure waste.
    """
    index = prices.Index()
    batch: list[tuple[str, dict]] = []
    total = 0
    for card in cards:
        cid = card.get("id")
        if not cid:
            continue
        index.add(card)
        batch.append((f"id:{cid}", card))
        if len(batch) >= _BATCH_SIZE:
            total += db.bulk_set_cards(batch)
            batch = []
    total += db.bulk_set_cards(batch)
    db.replace_card_prices(index.rows())
    prices.clear_cache()
    return total


_IMPORTERS = {
    "oracle_cards": _import_oracle_cards,
    "all_cards": _import_all_cards,
}


def import_local_all_cards_file(path: str) -> int:
    """Bootstrap the ``id:<uuid>`` cache from an already-downloaded bulk file
    (e.g. the ``all-cards-*.jsonl`` grabbed by hand), without a network call.

    Also records the export's ``updated_at`` (parsed from Scryfall's filename
    convention ``all-cards-YYYYMMDDHHMMSS.jsonl``) so the next scheduled
    refresh knows this version is already applied and won't immediately
    re-download the same ~2.5 GB file.
    """
    count = _import_all_cards(iter_local_jsonl(path))
    stamp = _updated_at_from_filename(path)
    if stamp:
        db.set_meta("bulk_all_cards_updated_at", stamp)
    db.set_meta("bulk_all_cards_synced_at", str(time.time()))
    prices.mark_indexed()
    return count


def _same_version(stored: str | None, remote: str | None) -> bool:
    """Compare two Scryfall ``updated_at`` timestamps at second precision.

    A version bootstrapped from a local file's name (``_updated_at_from_filename``)
    doesn't carry milliseconds, so a raw string comparison against the API's
    ``updated_at`` (which does) would always miss and trigger a redundant
    multi-GB re-download every time.
    """
    if not stored or not remote:
        return False
    try:
        return datetime.fromisoformat(stored).replace(microsecond=0) == datetime.fromisoformat(
            remote
        ).replace(microsecond=0)
    except ValueError:
        return stored == remote


def _updated_at_from_filename(path: str) -> str | None:
    base = os.path.basename(path)
    digits = "".join(ch for ch in base if ch.isdigit())
    if len(digits) < 14:
        return None
    y, mo, d, h, mi, s = (
        digits[0:4], digits[4:6], digits[6:8], digits[8:10], digits[10:12], digits[12:14],
    )
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}+00:00"


def refresh(force: bool = False, types=BULK_TYPES) -> dict:
    """Import any bulk-data export that's newer than what's already applied.

    ``types`` narrows which exports to check (default: both) — useful to
    force a re-import of the small ``oracle_cards`` export without also
    redownloading the ~2.5 GB ``all_cards`` one.

    Returns ``{bulk_type: cards_imported}`` for the types actually refreshed.
    """
    imported = {}
    with httpx.Client(headers={"User-Agent": settings.user_agent}, timeout=30) as client:
        for bulk_type in types:
            meta = _bulk_object(client, bulk_type)
            updated_key = f"bulk_{bulk_type}_updated_at"
            stored = db.get_meta(updated_key)
            if not force and _same_version(stored, meta.get("updated_at")):
                continue
            # The size is cosmetic: Scryfall renamed ``size`` to
            # ``compressed_size`` in 2026 and a KeyError here froze every
            # refresh for weeks (stale cache, wrong prices). Never let a
            # log line's decoration break the import.
            size = meta.get("compressed_size") or meta.get("size")
            logger.info(
                "bulk_data: importing %s (%s)", bulk_type,
                f"{size / 1e6:.0f} MB" if size else "size unknown",
            )
            count = _IMPORTERS[bulk_type](
                _iter_remote_jsonl(client, meta["jsonl_download_uri"])
            )
            db.set_meta(updated_key, meta.get("updated_at", ""))
            db.set_meta(f"bulk_{bulk_type}_synced_at", str(time.time()))
            if bulk_type == "all_cards":
                # _import_all_cards rebuilt the price index from this export;
                # stamp it now that its version is known, so the scheduler
                # doesn't re-scan the cache to rebuild what is already current.
                prices.mark_indexed()
            imported[bulk_type] = count
            logger.info("bulk_data: imported %d %s rows", count, bulk_type)
    if imported:
        # Card data changed: drop the in-memory text-view cache and the
        # per-profile collection caches (prices feed both the owned-printing
        # price map and the cached total value).
        collection.clear_text_cache()
        db.delete_meta_prefix(collection.STATS_META_PREFIX)
        db.delete_meta_prefix(collection.PRICES_META_PREFIX)
    return imported


#: Past this age the home page shows the Scryfall sync in red. The scheduler
#: runs daily (``bulk_refresh_hours``), so a week of silence means it is
#: broken, not merely late — exactly what went unnoticed for 40 days when
#: Scryfall renamed a field and every refresh died on a KeyError.
STALE_AFTER_DAYS = 7


def _age_label(seconds: float) -> str:
    """French, coarse: « 3 heures », « 12 jours »… (« moins d'une heure »)."""
    hours = int(seconds // 3600)
    if hours < 1:
        return "moins d'une heure"
    if hours < 48:
        return f"{hours} heure{'s' if hours > 1 else ''}"
    days = int(seconds // 86400)
    return f"{days} jour{'s' if days > 1 else ''}"


def freshness(now: float | None = None) -> dict:
    """How old the local Scryfall data is, for display.

    Card data comes from ``oracle_cards`` and prices from ``all_cards``, so the
    OLDER of the two syncs is what counts: a stuck all_cards import must show
    even when oracle_cards went through. Returns ``{synced_at, age_label,
    stale}``; ``synced_at`` is None (and ``stale`` True) when either export
    has never been imported.
    """
    now = time.time() if now is None else now
    stamps = []
    for bulk_type in BULK_TYPES:
        raw = db.get_meta(f"bulk_{bulk_type}_synced_at")
        try:
            stamps.append(float(raw))
        except (TypeError, ValueError):
            return {"synced_at": None, "age_label": "jamais", "stale": True}
    synced = min(stamps)
    age = max(0.0, now - synced)
    return {
        "synced_at": synced,
        "age_label": _age_label(age),
        "stale": age >= STALE_AFTER_DAYS * 86400,
    }


def needs_refresh() -> bool:
    cutoff = settings.bulk_refresh_hours * 3600
    for bulk_type in BULK_TYPES:
        synced = db.get_meta(f"bulk_{bulk_type}_synced_at")
        if not synced or (time.time() - float(synced)) >= cutoff:
            return True
    return False


def run_scheduler_loop() -> None:
    """Blocking loop: refresh whatever's stale, then poll hourly. Meant to run
    in a background daemon thread for the lifetime of the process."""
    while True:
        try:
            if needs_refresh():
                refresh()
            if prices.needs_rebuild():
                # Cache imported before the price index existed: build it from
                # the printings already on disk rather than making the user
                # wait a whole refresh cycle for correct deck prices.
                prices.rebuild_from_cache()
        except Exception:
            logger.exception("bulk_data: scheduled refresh failed")
        time.sleep(_POLL_INTERVAL_SECONDS)


def start_background_refresh() -> None:
    if not settings.bulk_auto_refresh:
        return
    import threading

    threading.Thread(target=run_scheduler_loop, daemon=True, name="bulk-data-refresh").start()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    db.init_db()
    if len(sys.argv) > 1:
        path = sys.argv[1]
        n = import_local_all_cards_file(path)
        logger.info("Imported %d printings from %s", n, path)
        logger.info("Fetching oracle_cards from the Scryfall API (small, ~180 MB)...")
        result = refresh(force=False)
        logger.info("Done: %s", result)
    else:
        logger.info("Downloading oracle_cards + all_cards from the Scryfall API...")
        result = refresh(force=True)
        logger.info("Done: %s", result)
