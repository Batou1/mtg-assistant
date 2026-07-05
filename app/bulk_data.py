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

from . import collection, db, scryfall
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
    """
    batch: list[tuple[str, dict]] = []
    total = 0
    for card in cards:
        if card.get("layout") in scryfall.NON_GAME_LAYOUTS:
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
    """Register ``id:<uuid>`` only — exact-printing lookups (resolve_ids)."""
    batch: list[tuple[str, dict]] = []
    total = 0
    for card in cards:
        cid = card.get("id")
        if not cid:
            continue
        batch.append((f"id:{cid}", card))
        if len(batch) >= _BATCH_SIZE:
            total += db.bulk_set_cards(batch)
            batch = []
    total += db.bulk_set_cards(batch)
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
            logger.info("bulk_data: importing %s (%.0f MB)", bulk_type, meta["size"] / 1e6)
            count = _IMPORTERS[bulk_type](
                _iter_remote_jsonl(client, meta["jsonl_download_uri"])
            )
            db.set_meta(updated_key, meta.get("updated_at", ""))
            db.set_meta(f"bulk_{bulk_type}_synced_at", str(time.time()))
            imported[bulk_type] = count
            logger.info("bulk_data: imported %d %s rows", count, bulk_type)
    if imported:
        # Card data changed: drop the in-memory text-view cache and the
        # per-profile collection stats (prices feed the cached total value).
        collection.clear_text_cache()
        db.delete_meta_prefix(collection.STATS_META_PREFIX)
    return imported


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
