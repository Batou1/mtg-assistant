import importlib

import pytest


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """A db module pointed at a throwaway SQLite file."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    return db


def _row(name, set_code="ABC", scryfall_id="", qty=1):
    key = name.split("//")[0].strip().lower()
    return {
        "scryfall_id": scryfall_id, "name_key": key, "raw_name": name,
        "set_code": set_code, "foil": 0, "condition": "near_mint", "quantity": qty,
    }


def test_distinct_cards_without_scryfall_id_are_not_collapsed(fresh_db):
    # Regression: empty Scryfall ids must not merge different cards into one row.
    fresh_db.replace_collection([
        _row("Sol Ring"), _row("Goblin Matron"), _row("Krenko, Mob Boss"),
    ])
    distinct, total = fresh_db.collection_count()
    assert distinct == 3 and total == 3
    assert fresh_db.owned_name_keys() == {"sol ring", "goblin matron", "krenko, mob boss"}


def test_identical_printing_sums_quantity(fresh_db):
    fresh_db.replace_collection([
        _row("Sol Ring", qty=1), _row("Sol Ring", qty=2),
    ])
    distinct, total = fresh_db.collection_count()
    assert distinct == 1 and total == 3


def test_first_scryfall_id_is_kept(fresh_db):
    fresh_db.replace_collection([
        _row("Sol Ring", scryfall_id="", qty=1),
        _row("Sol Ring", scryfall_id="abc-1", qty=1),
    ])
    assert fresh_db.collection_scryfall_ids() == ["abc-1"]
