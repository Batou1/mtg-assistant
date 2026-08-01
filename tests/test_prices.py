"""Cheapest-printing index and the buy-vs-own price split (app/prices.py)."""
import importlib

import pytest


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """(db, prices) modules pointed at a throwaway SQLite file."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.prices as prices
    importlib.reload(prices)
    db.init_db()
    prices.clear_cache()
    yield db, prices
    prices.clear_cache()


def printing(name, eur, set_code="xyz", **extra):
    card = {
        "id": f"{set_code}-{name}",
        "name": name,
        "set": set_code,
        "set_name": set_code.upper() + " set",
        "set_type": "expansion",
        "border_color": "black",
        "games": ["paper"],
        "prices": {"eur": str(eur)},
    }
    card.update(extra)
    return card


def test_index_keeps_the_cheapest_printing_of_each_name(fresh):
    _db, prices = fresh
    prices.build_index([
        printing("Sol Ring", 1.18, "msc"),
        printing("Sol Ring", 0.61, "voc"),
        printing("Sol Ring", 0.94, "clb"),
    ])
    assert prices.cheapest("sol ring") == (0.61, "voc", "VOC set")


def test_buy_price_ignores_the_printing_it_is_handed(fresh):
    """A decklist names a card, not an edition: quote the cheapest one."""
    _db, prices = fresh
    canonical = printing("Sol Ring", 1.18, "msc")
    prices.build_index([canonical, printing("Sol Ring", 0.61, "voc")])
    assert prices.buy_price_eur(canonical) == 0.61
    assert prices.buy_printing(canonical) == ("VOC", "VOC set")


def test_buy_price_falls_back_to_the_card_when_the_index_is_empty(fresh):
    """A fresh install (no bulk import yet) must still price cards."""
    _db, prices = fresh
    assert prices.buy_price_eur(printing("Sol Ring", 1.18)) == 1.18
    assert prices.buy_printing(printing("Sol Ring", 1.18)) == ("", "")


def test_buy_price_never_exceeds_the_printing_in_hand(fresh):
    """A stale index must not make a card look dearer than a known printing."""
    _db, prices = fresh
    prices.build_index([printing("Sol Ring", 5.00, "old")])
    assert prices.buy_price_eur(printing("Sol Ring", 0.80, "new")) == 0.80
    assert prices.buy_printing(printing("Sol Ring", 0.80, "new")) == ("NEW", "NEW set")


@pytest.mark.parametrize("unplayable", [
    # World Championship decks / Collector's Edition: real, cheap, gold-bordered
    # and not legal in any constructed format.
    {"border_color": "gold", "set_type": "memorabilia"},
    {"border_color": "silver", "set_type": "funny"},
    {"set_type": "token"},
    {"oversized": True},
    {"layout": "art_series"},
    # MTGO/Arena printings carry no Cardmarket price to begin with.
    {"games": ["mtgo"]},
])
def test_unplayable_printings_never_set_the_price(fresh, unplayable):
    _db, prices = fresh
    prices.build_index([
        printing("Counterspell", 0.10, "bad", **unplayable),
        printing("Counterspell", 1.16, "6ed"),
    ])
    assert prices.cheapest("counterspell") == (1.16, "6ed", "6ED set")


def test_double_faced_cards_are_indexed_under_both_names(fresh):
    _db, prices = fresh
    prices.build_index([printing("Fire // Ice", 0.40, "apc")])
    assert prices.cheapest("fire // ice")[0] == 0.40
    assert prices.cheapest("fire")[0] == 0.40


def test_rebuild_from_cache_reads_the_printings_already_stored(fresh):
    """An install whose card cache predates the index gets it without a
    multi-GB re-download."""
    db, prices = fresh
    assert prices.needs_rebuild() is False   # nothing imported at all
    for card in (printing("Sol Ring", 1.18, "msc"), printing("Sol Ring", 0.61, "voc")):
        db.set_card(f"id:{card['id']}", card)
    assert prices.needs_rebuild() is True
    assert prices.rebuild_from_cache() == 1
    assert prices.cheapest("sol ring")[0] == 0.61
    assert prices.needs_rebuild() is False


def test_index_is_replaced_not_merged(fresh):
    """A card whose cheap printing vanished must not keep its old price."""
    db, prices = fresh
    prices.build_index([printing("Sol Ring", 0.61, "voc")])
    prices.build_index([printing("Sol Ring", 1.18, "msc")])
    assert prices.cheapest("sol ring")[0] == 1.18
    assert db.card_prices_count() == 1


def test_buylist_quotes_the_cheapest_printing(fresh):
    """The end-to-end reason this exists: budgets were computed on the dearest
    printing of every card."""
    _db, prices = fresh
    import app.buylist as buylist
    import app.scryfall as scryfall
    importlib.reload(buylist)
    canonical = printing("Sol Ring", 1.18, "msc")
    prices.build_index([canonical, printing("Sol Ring", 0.61, "voc")])

    def _resolve(names, client=None):
        return {n.strip().lower(): canonical for n in names}, []

    buylist.scryfall.resolve_cards = _resolve
    try:
        result = buylist.build(["Sol Ring"], budget=None)
    finally:
        importlib.reload(scryfall)
        importlib.reload(buylist)
    assert result["to_buy"][0]["price_eur"] == 0.61
    assert result["to_buy"][0]["set_code"] == "VOC"
    assert result["total_eur"] == 0.61
