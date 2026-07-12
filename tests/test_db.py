import importlib
import sqlite3

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


def _row(name, set_code="ABC", scryfall_id="", qty=1, binder_type=""):
    key = name.split("//")[0].strip().lower()
    return {
        "scryfall_id": scryfall_id, "name_key": key, "raw_name": name,
        "set_code": set_code, "foil": 0, "condition": "near_mint", "quantity": qty,
        "binder_type": binder_type,
    }


def test_distinct_cards_without_scryfall_id_are_not_collapsed(fresh_db):
    # Regression: empty Scryfall ids must not merge different cards into one row.
    pid = fresh_db.ensure_default_profile()
    fresh_db.replace_collection(pid, [
        _row("Sol Ring"), _row("Goblin Matron"), _row("Krenko, Mob Boss"),
    ])
    distinct, total = fresh_db.collection_count(pid)
    assert distinct == 3 and total == 3
    assert fresh_db.owned_name_keys(pid) == {"sol ring", "goblin matron", "krenko, mob boss"}


def test_identical_printing_sums_quantity(fresh_db):
    pid = fresh_db.ensure_default_profile()
    fresh_db.replace_collection(pid, [_row("Sol Ring", qty=1), _row("Sol Ring", qty=2)])
    distinct, total = fresh_db.collection_count(pid)
    assert distinct == 1 and total == 3


def test_deck_binder_copies_are_tracked_separately(fresh_db):
    # Same printing split between a binder and a deck: quantities are summed
    # but the deck copies stay visible via owned_quantities.
    pid = fresh_db.ensure_default_profile()
    fresh_db.replace_collection(pid, [
        _row("Sol Ring", qty=2, binder_type="binder"),
        _row("Sol Ring", qty=1, binder_type="deck"),
        _row("Goblin Matron", qty=1, binder_type="deck"),
        _row("Llanowar Elves", qty=1),  # no binder info (older export)
    ])
    assert fresh_db.owned_quantities(pid) == {
        "sol ring": (3, 1),
        "goblin matron": (1, 1),
        "llanowar elves": (1, 0),
    }
    # Totals are unchanged: deck copies are still owned copies.
    assert fresh_db.collection_count(pid) == (3, 5)


def test_deck_qty_column_added_to_existing_profile_schema(tmp_path, monkeypatch):
    """A DB created before deck_qty existed gets the column in place."""
    db_path = tmp_path / "noqty.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE collection (
            profile_id INTEGER NOT NULL, scryfall_id TEXT, name_key TEXT NOT NULL,
            raw_name TEXT NOT NULL, set_code TEXT, foil INTEGER NOT NULL DEFAULT 0,
            condition TEXT, quantity INTEGER NOT NULL,
            PRIMARY KEY (profile_id, name_key, set_code, foil, condition)
        );
        INSERT INTO collection VALUES (1, '', 'sol ring', 'Sol Ring', 'LTC', 0, 'nm', 2);
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("MTG_DB_PATH", str(db_path))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()

    # Legacy rows count as fully available (deck_qty 0).
    assert db.owned_quantities(1) == {"sol ring": (2, 0)}


def test_collections_are_isolated_per_profile(fresh_db):
    alice = fresh_db.create_profile("Alice")
    bob = fresh_db.create_profile("Bob")
    fresh_db.replace_collection(alice, [_row("Sol Ring"), _row("Goblin Matron")])
    fresh_db.replace_collection(bob, [_row("Llanowar Elves")])

    assert fresh_db.collection_count(alice) == (2, 2)
    assert fresh_db.collection_count(bob) == (1, 1)
    assert fresh_db.owned_name_keys(bob) == {"llanowar elves"}


def test_delete_profile_removes_its_collection_and_keeps_one(fresh_db):
    alice = fresh_db.create_profile("Alice")
    bob = fresh_db.create_profile("Bob")
    fresh_db.replace_collection(alice, [_row("Sol Ring")])
    fresh_db.delete_profile(alice)

    assert fresh_db.get_profile(alice) is None
    names = {p["name"] for p in fresh_db.list_profiles()}
    assert "Bob" in names and "Alice" not in names


def test_delete_last_profile_recreates_default(fresh_db):
    only = fresh_db.ensure_default_profile()
    fresh_db.delete_profile(only)
    assert len(fresh_db.list_profiles()) == 1  # a default was recreated


def test_create_profile_is_idempotent_by_name(fresh_db):
    a = fresh_db.create_profile("Alice")
    b = fresh_db.create_profile("Alice")
    assert a == b


def test_migration_from_pre_profiles_schema(tmp_path, monkeypatch):
    """An old single-collection DB is migrated under a default profile."""
    db_path = tmp_path / "old.db"
    # Build a pre-profiles database by hand.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE cards (name_key TEXT PRIMARY KEY, data TEXT, fetched_at REAL);
        CREATE TABLE edhrec (slug TEXT PRIMARY KEY, data TEXT, fetched_at REAL);
        CREATE TABLE collection (
            scryfall_id TEXT, name_key TEXT NOT NULL, raw_name TEXT NOT NULL,
            set_code TEXT, foil INTEGER NOT NULL DEFAULT 0, condition TEXT,
            quantity INTEGER NOT NULL,
            PRIMARY KEY (name_key, set_code, foil, condition)
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO collection VALUES ('', 'sol ring', 'Sol Ring', 'LTC', 0, 'nm', 2);
        INSERT INTO meta VALUES ('collection_source', 'ManaBox: old.csv');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("MTG_DB_PATH", str(db_path))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()

    profiles = db.list_profiles()
    assert len(profiles) == 1
    pid = profiles[0]["id"]
    assert db.collection_count(pid) == (1, 2)
    assert profiles[0]["collection_source"] == "ManaBox: old.csv"
