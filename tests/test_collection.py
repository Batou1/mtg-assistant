"""Collection enrichment, search and cached home-page stats (app/collection.py)."""
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


def _card(name, eur="2.50", colors=("R",), **extra):
    card = {
        "name": name,
        "type_line": "Creature — Goblin",
        "mana_cost": "{R}",
        "cmc": 1.0,
        "oracle_text": "Haste",
        "power": "1",
        "toughness": "1",
        "rarity": "common",
        "set": "abc",
        "released_at": "2020-01-01",
        "color_identity": list(colors),
        "colors": list(colors),
        "prices": {"eur": eur},
        "image_uris": {
            "small": f"https://img/small/{name}.jpg",
            "normal": f"https://img/normal/{name}.jpg",
        },
    }
    card.update(extra)
    return card


def _row(name, qty=1, binder_type="binder", set_code="ABC"):
    return {
        "scryfall_id": "", "name_key": name.lower(), "raw_name": name,
        "set_code": set_code, "foil": 0, "condition": "near_mint",
        "quantity": qty, "binder_type": binder_type,
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


def _stocked(fresh):
    """A profile holding four cards with contrasting type/colour/price/rarity."""
    db, collection = fresh
    pid = db.ensure_default_profile()
    db.set_card("goblin matron", _card("Goblin Matron", eur="1.20"))
    db.set_card("sol ring", _card(
        "Sol Ring", eur="1.90", colors=(), type_line="Artifact", mana_cost="{1}",
        oracle_text="{T}: Add {C}{C}.", rarity="uncommon", set="c21",
        power=None, toughness=None,
    ))
    db.set_card("counterspell", _card(
        "Counterspell", eur="0.60", colors=("U",), type_line="Instant",
        mana_cost="{U}{U}", cmc=2.0, oracle_text="Counter target spell.",
        rarity="uncommon", power=None, toughness=None,
    ))
    db.replace_collection(pid, [
        _row("Goblin Matron", qty=2),
        _row("Goblin Matron", qty=1, binder_type="deck", set_code="XYZ"),
        _row("Sol Ring", qty=4),
        _row("Counterspell", qty=1),
        _row("Mystery Card", qty=3),
    ])
    return db, collection, pid


def _names(collection, pid, query, sort="name", direction="asc"):
    import app.scryquery as scryquery
    rows = collection.search(pid, scryquery.parse(query), sort, direction)
    return [r["name"] for r in rows]


def test_search_filters_on_card_data(fresh):
    _db, collection, pid = _stocked(fresh)
    assert _names(collection, pid, "") == [
        "Counterspell", "Goblin Matron", "Mystery Card", "Sol Ring",
    ]
    assert _names(collection, pid, "t:creature") == ["Goblin Matron"]
    assert _names(collection, pid, "t:artifact or t:instant") == ["Counterspell", "Sol Ring"]
    assert _names(collection, pid, "id<=u") == ["Counterspell", "Sol Ring"]
    assert _names(collection, pid, "eur<1") == ["Counterspell"]
    assert _names(collection, pid, "r:uncommon mv<=1") == ["Sol Ring"]
    assert _names(collection, pid, 'o:"counter target"') == ["Counterspell"]
    assert _names(collection, pid, "s:c21") == ["Sol Ring"]


def test_search_filters_on_owned_quantities(fresh):
    """Copies already sleeved in a deck are not copies you can build with."""
    _db, collection, pid = _stocked(fresh)
    assert _names(collection, pid, "qty>=3") == ["Goblin Matron", "Mystery Card", "Sol Ring"]
    assert _names(collection, pid, "is:indeck") == ["Goblin Matron"]
    assert _names(collection, pid, "deck:1") == ["Goblin Matron"]
    # Goblin Matron: 3 owned, 1 in a deck -> still 2 spare copies.
    assert "Goblin Matron" in _names(collection, pid, "is:spare")


def test_search_keeps_cards_the_cache_has_not_resolved(fresh):
    _db, collection, pid = _stocked(fresh)
    assert _names(collection, pid, "is:unresolved") == ["Mystery Card"]
    assert _names(collection, pid, "mystery") == ["Mystery Card"]
    # ...but a card-data filter can't match one: there is no data to match on.
    assert "Mystery Card" not in _names(collection, pid, "t:creature")


def test_sort_rows_uses_name_as_a_stable_tiebreaker(fresh):
    _db, collection, pid = _stocked(fresh)
    assert _names(collection, pid, "", sort="qty", direction="desc") == [
        "Sol Ring", "Goblin Matron", "Mystery Card", "Counterspell",
    ]
    assert _names(collection, pid, "", sort="price", direction="desc")[0] == "Sol Ring"
    # Unpriced/unresolved cards sort last on value, not first.
    assert _names(collection, pid, "", sort="value", direction="desc")[-1] == "Mystery Card"


def test_build_query_compiles_the_panel_into_scryfall_syntax(fresh):
    collection = fresh[1]
    assert collection.build_query({"type": "creature", "mv_max": "3"}) == "t:creature mv<=3"
    assert collection.build_query({"oracle": "draw a card"}) == 'o:"draw a card"'
    assert collection.build_query({"colors": ["U", "R"], "color_mode": "exact"}) == "c=ur"
    assert collection.build_query({"colors": ["U"]}) == "id<=u"
    assert collection.build_query({"copies": "spare"}) == "is:spare"
    assert collection.build_query({"price_min": "1,5"}) == "eur>=1.5"
    assert collection.build_query({}) == ""


def test_build_query_parenthesises_the_raw_box(fresh):
    """A top-level `or` in the free-text box must not swallow the panel's terms."""
    collection = fresh[1]
    query = collection.build_query({"q": "t:goblin or t:elf", "rarity": "rare"})
    assert query == "(t:goblin or t:elf) r:rare"
    assert collection.build_query({"q": "t:goblin"}) == "t:goblin"


def test_resolve_sort_falls_back_and_accepts_scryfall_order(fresh):
    collection = fresh[1]
    assert collection.resolve_sort("price", None) == "price"
    assert collection.resolve_sort(None, "cmc") == "mv"
    assert collection.resolve_sort("nonsense", None) == "qty"
    assert collection.resolve_sort(None, None) == "qty"


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
