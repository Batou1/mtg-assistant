"""Paper-printing enforcement: nothing the app prices may be MTGO/Arena-only.

Scryfall's canonical pick for a card name is sometimes a digital printing
(Vintage Masters, Masters Edition, Alchemy). Those objects carry no Cardmarket
price, which used to make expensive staples look free and blow the budget maths
apart — Mishra's Workshop priced at 0 €.
"""
import importlib

import pytest


@pytest.fixture()
def sf(tmp_path, monkeypatch):
    """app.scryfall + app.db pointed at a throwaway SQLite file."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MTG_REQUEST_DELAY", "0")
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.scryfall as scryfall
    importlib.reload(scryfall)
    db.init_db()
    return scryfall, db


DIGITAL = {
    "id": "mtgo-1", "name": "Mishra's Workshop", "games": ["mtgo"], "digital": True,
    "legalities": {"vintage": "restricted"}, "prices": {"eur": None, "tix": "40"},
}
PAPER = {
    "id": "paper-1", "name": "Mishra's Workshop", "games": ["paper"],
    "legalities": {"vintage": "restricted"}, "prices": {"eur": "949.00"},
}


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    """Stands in for httpx.Client: records the searches it was asked to run."""

    def __init__(self, printings):
        self.printings = printings
        self.queries = []

    def get(self, url, params=None):
        self.queries.append((params or {}).get("q"))
        return _Resp({"data": self.printings})


def test_is_paper_uses_games_then_digital_flag(sf):
    scryfall, _db = sf
    assert scryfall.is_paper(PAPER) is True
    assert scryfall.is_paper(DIGITAL) is False
    # Older/slimmer objects without a games list fall back to the digital flag.
    assert scryfall.is_paper({"name": "X"}) is True
    assert scryfall.is_paper({"name": "X", "digital": True}) is False


def test_digital_only_result_is_swapped_for_a_paper_printing(sf):
    scryfall, db = sf
    db.set_card("mishra's workshop", DIGITAL)
    client = _FakeClient([PAPER])

    resolved, _nf = scryfall.resolve_cards(["Mishra's Workshop"], client=client)

    card = resolved["mishra's workshop"]
    assert card["id"] == "paper-1"
    assert scryfall.price_eur(card) == 949.0
    # The poisoned cache entry is repaired in place, so the next lookup (and
    # every other profile) gets the paper printing without a new search.
    assert db.get_card("mishra's workshop")["id"] == "paper-1"
    assert client.queries == ['!"Mishra\'s Workshop" game:paper']


def test_paper_printing_with_a_price_wins_over_a_priceless_one(sf):
    scryfall, db = sf
    db.set_card("mishra's workshop", DIGITAL)
    promo = {"id": "paper-2", "name": "Mishra's Workshop", "games": ["paper"],
             "prices": {"eur": None}}
    client = _FakeClient([promo, PAPER])

    resolved, _nf = scryfall.resolve_cards(["Mishra's Workshop"], client=client)
    assert resolved["mishra's workshop"]["id"] == "paper-1"


def test_card_with_no_paper_printing_is_kept_and_not_searched_twice(sf):
    """Alchemy/Arena-only cards genuinely have no paper printing: keep them,
    but remember the miss so every later lookup doesn't re-run the search."""
    scryfall, db = sf
    alchemy = {"id": "arena-1", "name": "A-Fake Card", "games": ["arena"],
               "digital": True}
    db.set_card("a-fake card", alchemy)
    client = _FakeClient([])

    resolved, _nf = scryfall.resolve_cards(["A-Fake Card"], client=client)
    assert resolved["a-fake card"]["id"] == "arena-1"
    assert len(client.queries) == 1

    scryfall.resolve_cards(["A-Fake Card"], client=client)
    assert len(client.queries) == 1


def test_paper_results_are_left_alone(sf):
    scryfall, db = sf
    db.set_card("mishra's workshop", PAPER)
    client = _FakeClient([PAPER])
    resolved, _nf = scryfall.resolve_cards(["Mishra's Workshop"], client=client)
    assert resolved["mishra's workshop"]["id"] == "paper-1"
    assert client.queries == []
