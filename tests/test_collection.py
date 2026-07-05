"""Collection enrichment + cached home-page stats (app/collection.py)."""
import importlib

import pytest


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """(db, collection) modules pointed at a throwaway SQLite file."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.collection as collection
    importlib.reload(collection)
    db.init_db()
    collection.clear_text_cache()
    return db, collection


def _card(name, eur="2.50", colors=("R",)):
    return {
        "name": name,
        "type_line": "Creature — Goblin",
        "mana_cost": "{R}",
        "oracle_text": "Haste",
        "power": "1",
        "toughness": "1",
        "color_identity": list(colors),
        "prices": {"eur": eur},
        "image_uris": {
            "small": f"https://img/small/{name}.jpg",
            "normal": f"https://img/normal/{name}.jpg",
        },
    }


def _row(name, qty=1):
    return {
        "scryfall_id": "", "name_key": name.lower(), "raw_name": name,
        "set_code": "ABC", "foil": 0, "condition": "near_mint", "quantity": qty,
    }


def test_get_cards_batches_and_skips_missing(fresh):
    db, _ = fresh
    db.set_card("sol ring", _card("Sol Ring"))
    db.set_card("goblin matron", _card("Goblin Matron"))
    out = db.get_cards(["sol ring", "goblin matron", "unknown card"])
    assert set(out) == {"sol ring", "goblin matron"}
    assert out["sol ring"]["name"] == "Sol Ring"


def test_enrich_uses_cache_and_fills_fields(fresh):
    db, collection = fresh
    pid = db.ensure_default_profile()
    db.set_card("sol ring", _card("Sol Ring"))
    db.replace_collection(pid, [_row("Sol Ring", qty=2), _row("Mystery Card")])
    rows = {r["name_key"]: r for r in collection.enrich(pid)}

    sol = rows["sol ring"]
    assert sol["qty"] == 2
    assert sol["line_total"] == 5.0
    assert sol["image"].endswith("/normal/Sol Ring.jpg")
    assert sol["image_small"].endswith("/small/Sol Ring.jpg")
    assert sol["power_toughness"] == "1/1"

    unknown = rows["mystery card"]
    assert unknown["image"] is None and unknown["price_eur"] is None


def test_stats_cached_and_invalidated_on_import(fresh):
    db, collection = fresh
    pid = db.ensure_default_profile()
    db.set_card("sol ring", _card("Sol Ring"))
    db.replace_collection(pid, [_row("Sol Ring", qty=2)])

    first = collection.stats(pid)
    assert first["total"] == 2 and first["total_value"] == 5.0
    assert db.get_meta(f"{collection.STATS_META_PREFIX}{pid}") is not None

    # Re-import invalidates the cached entry; stats reflect the new content.
    db.replace_collection(pid, [_row("Sol Ring", qty=5)])
    assert db.get_meta(f"{collection.STATS_META_PREFIX}{pid}") is None
    assert collection.stats(pid)["total"] == 5


def test_delete_meta_prefix_only_touches_prefix(fresh):
    db, _ = fresh
    db.set_meta("collection_stats:1", "x")
    db.set_meta("collection_stats:2", "x")
    db.set_meta("other_key", "keep")
    db.delete_meta_prefix("collection_stats:")
    assert db.get_meta("collection_stats:1") is None
    assert db.get_meta("collection_stats:2") is None
    assert db.get_meta("other_key") == "keep"


def test_card_text_info_memoizes_but_not_misses(fresh):
    db, collection = fresh
    assert collection.card_text_info("Sol Ring") is None
    # A card resolved later (e.g. bulk refresh) must show up without a clear.
    db.set_card("sol ring", _card("Sol Ring"))
    info = collection.card_text_info("Sol Ring")
    assert info == {
        "mana_cost": "{R}",
        "type_line": "Creature — Goblin",
        "oracle_text": "Haste",
        "power_toughness": "1/1",
    }
