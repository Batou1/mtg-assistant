import importlib

import pytest


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """app.cardsearch + app.db pointed at a throwaway SQLite file."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.scryfall as scryfall
    importlib.reload(scryfall)
    import app.cardsearch as cardsearch
    importlib.reload(cardsearch)
    db.init_db()
    return cardsearch, db


CARDS = [
    {
        "id": "c-sol-ring", "name": "Sol Ring", "layout": "normal",
        "mana_cost": "{1}", "cmc": 1.0, "type_line": "Artifact",
        "oracle_text": "{T}: Add {C}{C}.", "color_identity": [],
        "keywords": [], "legalities": {"commander": "legal"},
    },
    {
        "id": "c-viscera", "name": "Viscera Seer", "layout": "normal",
        "mana_cost": "{B}", "cmc": 1.0, "type_line": "Creature — Cleric",
        "oracle_text": "Sacrifice a creature: Scry 1.", "color_identity": ["B"],
        "keywords": [], "legalities": {"commander": "legal"},
    },
    {
        "id": "c-serra", "name": "Serra Angel", "layout": "normal",
        "mana_cost": "{3}{W}{W}", "cmc": 5.0, "type_line": "Creature — Angel",
        "oracle_text": "Flying, vigilance", "color_identity": ["W"],
        "keywords": ["Flying", "Vigilance"], "legalities": {"commander": "legal"},
    },
    {
        "id": "art-sol-ring", "name": "Sol Ring // Sol Ring", "layout": "art_series",
        "type_line": "Card // Card", "color_identity": [], "keywords": [],
        "legalities": {},
    },
]


def _seed(db):
    db.bulk_set_cards([(c["name"].strip().lower(), c) for c in CARDS])
    db.set_meta("bulk_oracle_cards_synced_at", "1")


def test_search_by_text_contains_finds_sacrifice_synergy(fresh):
    cardsearch, db = fresh
    _seed(db)
    results = cardsearch.search(text_contains=["sacrifice a creature"])
    assert [c["name"] for c in results] == ["Viscera Seer"]


def test_search_by_keyword(fresh):
    cardsearch, db = fresh
    _seed(db)
    results = cardsearch.search(keywords=["flying"])
    assert [c["name"] for c in results] == ["Serra Angel"]


def test_search_by_type_contains(fresh):
    cardsearch, db = fresh
    _seed(db)
    results = cardsearch.search(type_contains=["Artifact"])
    assert [c["name"] for c in results] == ["Sol Ring"]


def test_search_respects_color_identity_subset(fresh):
    cardsearch, db = fresh
    _seed(db)
    results = cardsearch.search(colors=["B"])
    names = {c["name"] for c in results}
    assert "Viscera Seer" in names and "Sol Ring" in names  # colourless fits any
    assert "Serra Angel" not in names


def test_search_respects_legality(fresh):
    cardsearch, db = fresh
    _seed(db)
    results = cardsearch.search(legal_in="commander")
    names = {c["name"] for c in results}
    # The Art Series junk object has no legalities and must be excluded anyway.
    assert "Sol Ring // Sol Ring" not in names


def test_search_respects_cmc_range(fresh):
    cardsearch, db = fresh
    _seed(db)
    results = cardsearch.search(min_cmc=2, max_cmc=6)
    assert [c["name"] for c in results] == ["Serra Angel"]


def test_search_excludes_names(fresh):
    cardsearch, db = fresh
    _seed(db)
    results = cardsearch.search(exclude_names={"Sol Ring"})
    names = {c["name"] for c in results}
    assert "Sol Ring" not in names


def test_search_skips_non_game_layouts(fresh):
    """Regression: an Art Series object sharing a real card's name must never
    surface as a search result — it isn't a playable card."""
    cardsearch, db = fresh
    _seed(db)
    results = cardsearch.search()
    names = {c["name"] for c in results}
    assert "Sol Ring // Sol Ring" not in names
    assert len(results) == 3


def test_cache_invalidates_on_new_sync_token(fresh):
    cardsearch, db = fresh
    _seed(db)
    first = cardsearch.search()
    assert len(first) == 3

    db.bulk_set_cards([("goblin guide", {
        "id": "c-goblin", "name": "Goblin Guide", "layout": "normal",
        "cmc": 1.0, "type_line": "Creature — Goblin", "color_identity": ["R"],
        "keywords": ["Haste"], "legalities": {},
    })])
    db.set_meta("bulk_oracle_cards_synced_at", "2")

    second = cardsearch.search()
    assert len(second) == 4
