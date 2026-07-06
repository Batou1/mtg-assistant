from app import buylist, db, poolbuild, llm, scryfall


def _card(name, type_line="Creature — Test", colors=None, cmc=1.0, legal=None, eur=None):
    card = {
        "name": name,
        "type_line": type_line,
        "color_identity": colors if colors is not None else ["R"],
        "colors": colors if colors is not None else ["R"],
        "cmc": cmc,
        "image_uris": {"normal": f"http://img/{name}"},
        "oracle_text": "Test text.",
    }
    if legal is not None:
        card["legalities"] = legal
    if eur is not None:
        card["prices"] = {"eur": str(eur)}
    return card


POOL = {
    "lightning bolt": _card("Lightning Bolt", "Instant", ["R"], 1.0),
    "goblin guide": _card("Goblin Guide", "Creature — Goblin", ["R"], 1.0),
    "monastery swiftspear": _card("Monastery Swiftspear", "Creature — Monk", ["R"], 1.0),
    "llanowar elves": _card("Llanowar Elves", "Creature — Elf", ["G"], 1.0),
    "giant growth": _card("Giant Growth", "Instant", ["G"], 1.0),
    "rugged highlands": _card("Rugged Highlands", "Land", ["R", "G"], 0.0),
}


def _fake_resolve(names, client=None):
    found = {n.lower(): POOL[n.lower()] for n in names if n.lower() in POOL}
    missing = [n for n in names if n.lower() not in POOL]
    return found, missing


# --- Pool parsing -------------------------------------------------------

def test_parse_pool_decklist_text():
    items = poolbuild.parse_pool("2 Lightning Bolt\nGoblin Guide\n# comment\n")
    assert ("Lightning Bolt", 2) in items
    assert ("Goblin Guide", 1) in items


def test_parse_pool_detects_csv():
    csv = "Name,Quantity\nLightning Bolt,3\nGoblin Guide,1\n"
    items = dict(poolbuild.parse_pool(csv, filename="pool.csv"))
    assert items["Lightning Bolt"] == 3
    assert items["Goblin Guide"] == 1


# --- Build: heuristic (no LLM) -----------------------------------------

def test_build_heuristic_fills_to_deck_size(monkeypatch):
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    monkeypatch.setattr(llm, "is_available", lambda: False)

    pool_items = [(c["name"], 1) for c in POOL.values()]
    deck = poolbuild.build_from_pool(pool_items, "limited", {"colors": ["R"]})

    assert deck["counts"]["total"] == 40  # basics top it up to a full Limited deck
    assert deck["source"] == "heuristic"
    # Only red (and colourless) cards are playable in a mono-red build.
    played = {c["name"] for g in deck["groups"] for c in g["cards"]}
    assert "Llanowar Elves" not in played
    assert "Giant Growth" not in played
    # Off-colour cards land in the sideboard instead.
    sb = {c["name"] for c in deck["sideboard"]}
    assert "Llanowar Elves" in sb


# --- Build: LLM selection is grounded to the pool ----------------------

def test_build_llm_selection_grounded(monkeypatch):
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    monkeypatch.setattr(llm, "is_available", lambda: True)

    def fake_pool_deck(spec, intent, pool_lines):
        return {
            "archetype": "Mono Red Aggro",
            "colors": ["R"],
            "strategy": "Tape vite.",
            "main_deck": [
                {"name": "Lightning Bolt", "count": 1},
                {"name": "Goblin Guide", "count": 1},
                {"name": "Black Lotus", "count": 1},   # NOT in pool -> dropped
            ],
            "basic_lands": {"Mountain": 17},
        }

    monkeypatch.setattr(llm, "pool_deck", fake_pool_deck)

    pool_items = [("Lightning Bolt", 1), ("Goblin Guide", 1)]
    deck = poolbuild.build_from_pool(pool_items, "limited", {})

    played = {c["name"] for g in deck["groups"] for c in g["cards"]}
    assert played == {"Lightning Bolt", "Goblin Guide"}  # Black Lotus filtered out
    assert deck["archetype"]["name"] == "Mono Red Aggro"
    assert deck["counts"]["total"] == 40
    # Basic lands were added from the model's suggestion.
    basics = {c["name"]: c["qty"] for c in deck["lands"]["basics"]}
    assert basics.get("Mountain", 0) > 0


def test_build_clamps_copies_to_pool_quantity(monkeypatch):
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(llm, "pool_deck", lambda spec, intent, lines: {
        "archetype": "Burn", "colors": ["R"], "strategy": "",
        "main_deck": [{"name": "Lightning Bolt", "count": 9}],  # only 2 in pool
        "basic_lands": {"Mountain": 17},
    })

    deck = poolbuild.build_from_pool([("Lightning Bolt", 2)], "limited", {})
    bolt = next(c for g in deck["groups"] for c in g["cards"] if c["name"] == "Lightning Bolt")
    assert bolt["qty"] == 2  # clamped to the 2 copies actually in the pool


def test_build_colorless_pool_no_hints(monkeypatch):
    # No intent colours and an empty selection colour list must not crash when
    # deriving colours for the basic-land split.
    colorless = {
        "ornithopter": _card("Ornithopter", "Artifact Creature — Thopter", [], 0.0),
        "darksteel relic": _card("Darksteel Relic", "Artifact", [], 0.0),
    }

    def resolve(names, client=None):
        return ({n.lower(): colorless[n.lower()] for n in names if n.lower() in colorless},
                [n for n in names if n.lower() not in colorless])

    monkeypatch.setattr(scryfall, "resolve_cards", resolve)
    monkeypatch.setattr(llm, "is_available", lambda: False)

    deck = poolbuild.build_from_pool(
        [("Ornithopter", 1), ("Darksteel Relic", 1)], "limited", {}
    )
    assert deck["counts"]["total"] == 40  # basics still fill the deck (Wastes)


def test_build_drops_unresolved_cards(monkeypatch):
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    monkeypatch.setattr(llm, "is_available", lambda: False)

    deck = poolbuild.build_from_pool(
        [("Lightning Bolt", 1), ("Not A Real Card", 1)], "limited", {"colors": ["R"]}
    )
    assert "Not A Real Card" in deck["invalid"]


# --- Constructed formats: legality + commander grounding ----------------

def test_constructed_drops_format_illegal_cards(monkeypatch):
    pool = {
        "lightning bolt": _card("Lightning Bolt", "Instant", ["R"], 1.0,
                                 legal={"modern": "legal"}),
        "black lotus": _card("Black Lotus", "Artifact", [], 0.0,
                              legal={"modern": "not_legal"}),
    }

    def resolve(names, client=None):
        return ({n.lower(): pool[n.lower()] for n in names if n.lower() in pool},
                [n for n in names if n.lower() not in pool])

    monkeypatch.setattr(scryfall, "resolve_cards", resolve)
    monkeypatch.setattr(llm, "is_available", lambda: False)

    deck = poolbuild.build_from_pool(
        [("Lightning Bolt", 4), ("Black Lotus", 1)], "modern", {"colors": ["R"]},
        with_bonus=False,
    )
    assert "Black Lotus" in deck["invalid"]  # not modern-legal
    played = {c["name"] for g in deck["groups"] for c in g["cards"]}
    assert "Black Lotus" not in played


def test_commander_enforces_colour_identity(monkeypatch):
    pool = {
        "krenko, mob boss": _card("Krenko, Mob Boss", "Legendary Creature — Goblin",
                                   ["R"], 4.0, legal={"commander": "legal"}),
        "lightning bolt": _card("Lightning Bolt", "Instant", ["R"], 1.0,
                                 legal={"commander": "legal"}),
        "llanowar elves": _card("Llanowar Elves", "Creature — Elf", ["G"], 1.0,
                                 legal={"commander": "legal"}),
    }

    def resolve(names, client=None):
        return ({n.lower(): pool[n.lower()] for n in names if n.lower() in pool},
                [n for n in names if n.lower() not in pool])

    monkeypatch.setattr(scryfall, "resolve_cards", resolve)
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(llm, "pool_deck", lambda spec, intent, lines: {
        "archetype": "Krenko Goblins", "colors": ["R"], "strategy": "Go wide.",
        "commander": "Krenko, Mob Boss",
        "main_deck": [{"name": "Lightning Bolt", "count": 1},
                      {"name": "Llanowar Elves", "count": 1}],  # off-identity -> dropped
        "basic_lands": {"Mountain": 98},
    })

    deck = poolbuild.build_from_pool(
        [("Krenko, Mob Boss", 1), ("Lightning Bolt", 1), ("Llanowar Elves", 1)],
        "commander", {}, with_bonus=False,
    )
    assert deck["commander"]["name"] == "Krenko, Mob Boss"
    played = {c["name"] for g in deck["groups"] for c in g["cards"]}
    assert "Lightning Bolt" in played
    assert "Llanowar Elves" not in played   # green, outside Krenko's red identity
    assert deck["counts"]["total"] == 100    # commander + 99
    # The commander must not also appear in the leftover sideboard.
    assert "Krenko, Mob Boss" not in {c["name"] for c in deck["sideboard"]}

def test_duelcommander_spec_enforces_duel_legality(monkeypatch):
    # Registered like "commander" but with its own (stricter) legality key.
    spec = poolbuild.SPECS["duelcommander"]
    assert spec.legality_format == "duel"
    assert spec.needs_commander
    assert spec.deck_size == 100 and spec.singleton

    pool = {
        "krenko, mob boss": _card("Krenko, Mob Boss", "Legendary Creature — Goblin",
                                   ["R"], 4.0, legal={"duel": "legal"}),
        "lightning bolt": _card("Lightning Bolt", "Instant", ["R"], 1.0,
                                 legal={"duel": "legal"}),
        # Banned in Duel Commander even though it's fine in plain Commander.
        "sol ring": _card("Sol Ring", "Artifact", [], 1.0, legal={"duel": "banned"}),
    }

    def resolve(names, client=None):
        return ({n.lower(): pool[n.lower()] for n in names if n.lower() in pool},
                [n for n in names if n.lower() not in pool])

    monkeypatch.setattr(scryfall, "resolve_cards", resolve)
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(llm, "pool_deck", lambda spec, intent, lines: {
        "archetype": "Krenko Goblins", "colors": ["R"], "strategy": "Go wide.",
        "commander": "Krenko, Mob Boss",
        "main_deck": [{"name": "Lightning Bolt", "count": 1}],
        "basic_lands": {"Mountain": 98},
    })

    deck = poolbuild.build_from_pool(
        [("Krenko, Mob Boss", 1), ("Lightning Bolt", 1), ("Sol Ring", 1)],
        "duelcommander", {}, with_bonus=False,
    )
    assert deck["commander"]["name"] == "Krenko, Mob Boss"
    assert "Sol Ring" in deck["invalid"]  # banned in Duel Commander -> excluded


def test_paupercommander_spec_registered():
    spec = poolbuild.SPECS["paupercommander"]
    assert spec.legality_format == "paupercommander"
    assert spec.needs_commander
    assert spec.deck_size == 100 and spec.singleton


def test_format_order_includes_new_commander_variants():
    assert "duelcommander" in poolbuild.FORMAT_ORDER
    assert "paupercommander" in poolbuild.FORMAT_ORDER
    assert all(f in poolbuild.SPECS for f in poolbuild.FORMAT_ORDER)


# --- Collection bonus: owned synergy + cards to buy ---------------------

def test_bonus_recommends_owned_and_buy(monkeypatch):
    catalog = {
        "lightning bolt": _card("Lightning Bolt", "Instant", ["R"], 1.0,
                                 legal={"modern": "legal"}),
        "goblin guide": _card("Goblin Guide", "Creature — Goblin", ["R"], 1.0,
                               legal={"modern": "legal"}),       # owned, eligible
        "llanowar elves": _card("Llanowar Elves", "Creature — Elf", ["G"], 1.0,
                                 legal={"modern": "legal"}),     # owned, off-colour
        "fireblast": _card("Fireblast", "Instant", ["R"], 4.0,
                            legal={"modern": "legal"}, eur=2.5),  # buy suggestion
    }

    def resolve(names, client=None):
        return ({n.lower(): catalog[n.lower()] for n in names if n.lower() in catalog},
                [n for n in names if n.lower() not in catalog])

    monkeypatch.setattr(scryfall, "resolve_cards", resolve)
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(llm, "pool_deck", lambda spec, intent, lines: {
        "archetype": "Mono Red", "colors": ["R"], "strategy": "Burn.",
        "main_deck": [{"name": "Lightning Bolt", "count": 4}], "basic_lands": {"Mountain": 17},
    })
    monkeypatch.setattr(db, "collection_names", lambda pid: [
        ("Goblin Guide", "goblin guide", 1),
        ("Llanowar Elves", "llanowar elves", 1),
    ])
    monkeypatch.setattr(llm, "pool_bonus", lambda spec, arch, cards, colors, owned: {
        "owned_bonus": ["Goblin Guide", "Llanowar Elves"],  # Elves filtered (off-colour)
        "buy_bonus": ["Fireblast"],
    })

    deck = poolbuild.build_from_pool(
        [("Lightning Bolt", 4)], "modern", {}, profile_id=1, with_bonus=True
    )
    bonus = deck["bonus"]
    assert bonus is not None
    owned_names = {c["name"] for c in bonus["owned"]}
    assert owned_names == {"Goblin Guide"}              # green Elves excluded as off-colour
    buy_names = {c["name"] for c in bonus["buy"]}
    assert "Fireblast" in buy_names
    assert bonus["buy_total_eur"] == 2.5
