"""Cheapest buyable printing per card name — the price of a card you must BUY.

The app asks two different money questions and used to answer both with the
same number, Scryfall's canonical printing for a name:

- *what is my copy worth?* — a card in the collection has an edition, so it is
  worth what THAT printing is worth (``app.collection``, priced from the
  ``id:<uuid>`` cache entry via ``scryfall.price_eur``);
- *what will this card cost me?* — a decklist names a card, never an edition,
  so the honest answer is the cheapest printing on the market. That is what
  this module provides.

Scryfall's canonical pick is "the most up-to-date recognizable version", which
is regularly a recent premium reprint: Sol Ring priced from a Marvel set at
1.18 € when a Commander-deck copy sells for 0.61 €, Counterspell at 3.04 €
against 1.16 €. Multiplied over a 100-card decklist that inflates every budget
decision the app makes.

No extra API call is needed: the ``all_cards`` bulk export (app/bulk_data.py)
already streams every printing into the local cache, so the index is built as a
by-product of the import it already runs, and rebuilt from the cached printings
for an install whose cache predates this index.
"""
import logging

from . import db, scryfall

logger = logging.getLogger(__name__)

#: meta key holding the ``all_cards`` bulk version the index was built from.
SOURCE_META_KEY = "card_prices_source"

# Set types whose printings are collectibles or jokes rather than cards you can
# sleeve up. Several of them are genuinely the cheapest listing for a card — a
# gold-bordered World Championship Counterspell undercuts every real printing,
# and Collector's Edition undercuts the reserved list — which is exactly why
# they have to be excluded: the app would recommend buying cards that are not
# legal in any format the user plays.
_EXCLUDED_SET_TYPES = frozenset({
    "memorabilia", "funny", "token", "vanguard", "minigame", "treasure_chest",
})
#: Gold = World Championship decks / Collector's Edition, silver = Un-cards and
#: Mystery Booster playtest cards. Same story as ``_EXCLUDED_SET_TYPES``.
_EXCLUDED_BORDERS = frozenset({"gold", "silver"})

# name_key -> (eur, set_code, set_name), the whole index. It is ~35k entries
# (one per card name, not per printing), so holding it in memory costs a few MB
# and saves a SQLite round-trip per card on every deck build — the same
# trade-off app/cardsearch.py makes with the card pool.
_cache: dict[str, tuple] | None = None


def clear_cache() -> None:
    """Drop the in-memory index (after a rebuild, or in tests)."""
    global _cache
    _cache = None


def is_buyable(card: dict) -> bool:
    """True if this printing is something the user could actually buy and play.

    Digital-only printings carry no Cardmarket price at all (see
    ``scryfall.is_paper``); the rest of the exclusions are paper cards that are
    real and cheap but not legal in any constructed format.
    """
    if card.get("layout") in scryfall.NON_GAME_LAYOUTS:
        return False
    if not scryfall.is_paper(card):
        return False
    if card.get("oversized"):
        return False
    if card.get("border_color") in _EXCLUDED_BORDERS:
        return False
    return card.get("set_type") not in _EXCLUDED_SET_TYPES


def _keys(name: str) -> set[str]:
    """Cache keys a printing should answer to — same convention as the ``cards``
    table: the full "Front // Back" name and the front-face name alone, because
    a decklist may spell a double-faced card either way."""
    full = name.strip().lower()
    keys = {full}
    if "//" in name:
        keys.add(scryfall.norm_name(name))
    return keys


class Index:
    """Running "cheapest buyable printing so far" per card name.

    Fed one printing at a time so ``bulk_data`` can build it while streaming
    the multi-GB export, without ever holding the printings in memory.
    """

    def __init__(self) -> None:
        self.best: dict[str, tuple[float, str, str]] = {}

    def add(self, card: dict) -> None:
        name = card.get("name") or ""
        if not name or not is_buyable(card):
            return
        price = scryfall.price_eur(card)
        if price is None:
            return
        entry = (price, card.get("set") or "", card.get("set_name") or "")
        for key in _keys(name):
            current = self.best.get(key)
            if current is None or price < current[0]:
                self.best[key] = entry

    def rows(self):
        return [(key, eur, code, name) for key, (eur, code, name) in self.best.items()]


def build_index(cards) -> int:
    """Index the cheapest buyable printing of every card in ``cards``.

    Replaces the whole index atomically: a partial index would price part of a
    decklist cheap and the rest at the canonical printing, which is worse than
    either answer alone.
    """
    index = Index()
    for card in cards:
        index.add(card)
    rows = index.rows()
    db.replace_card_prices(rows)
    clear_cache()
    return len(rows)


def mark_indexed() -> None:
    """Record which ``all_cards`` version the current index was built from."""
    db.set_meta(SOURCE_META_KEY, db.get_meta("bulk_all_cards_updated_at") or "")


def needs_rebuild() -> bool:
    """True when the index is missing or older than the cached printings.

    ``bulk_data`` builds the index as it imports, so this only fires for an
    install whose card cache predates the index — which would otherwise have to
    wait for the next multi-GB import before deck prices became correct. The
    stored stamp is the bulk version rather than a timestamp, because the index
    is written *before* the import records its own version.
    """
    if not db.has_printings():
        return False
    if db.card_prices_count() == 0:
        return True
    stamp = db.get_meta(SOURCE_META_KEY)
    return stamp is None or stamp != (db.get_meta("bulk_all_cards_updated_at") or "")


def rebuild_from_cache() -> int:
    """Rebuild the index from the printings already in the local card cache."""
    logger.info("prices: rebuilding the cheapest-printing index from the card cache")
    count = build_index(db.iter_printings())
    mark_indexed()
    logger.info("prices: indexed %d card names", count)
    return count


def _load() -> dict[str, tuple]:
    global _cache
    if _cache is None:
        _cache = db.all_card_prices()
    return _cache


def cheapest(name: str) -> tuple[float, str, str] | None:
    """``(eur, set_code, set_name)`` of the cheapest buyable printing, or None."""
    return _load().get((name or "").strip().lower())


def buy_price_eur(card: dict | None) -> float | None:
    """What one copy of ``card`` costs to acquire, in EUR.

    The cheapest buyable printing, falling back to the price of the printing we
    were handed when the index knows nothing about the name (a fresh install
    before the first bulk import, or a card too new to be indexed). ``min`` of
    the two guards the stale case: an index built yesterday must never make a
    card look dearer than the printing in hand.
    """
    if not card:
        return None
    own = scryfall.price_eur(card)
    found = cheapest(card.get("name") or "")
    if found is None:
        return own
    return found[0] if own is None else min(found[0], own)


def buy_printing(card: dict | None) -> tuple[str, str]:
    """``(set_code, set_name)`` of the printing ``buy_price_eur`` quotes.

    Empty strings when the quote came from the card in hand rather than the
    index, so callers can simply skip an empty edition label.
    """
    if not card:
        return "", ""
    found = cheapest(card.get("name") or "")
    if found is None:
        return "", ""
    own = scryfall.price_eur(card)
    if own is not None and own < found[0]:
        return (card.get("set") or "").upper(), card.get("set_name") or ""
    return found[1].upper(), found[2]


if __name__ == "__main__":  # pragma: no cover - operational helper
    # Rebuilds the index from the printings already cached locally, so an
    # install can pick up correct deck prices without waiting for (or
    # re-downloading) the multi-GB all_cards export.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    db.init_db()
    rebuild_from_cache()
