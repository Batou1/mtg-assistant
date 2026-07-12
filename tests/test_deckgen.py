from app import deckgen


def card(name, eur=None, colors=None):
    c = {"name": name, "image_uris": {"normal": f"http://img/{name}"},
         "color_identity": colors or []}
    if eur is not None:
        c["prices"] = {"eur": str(eur)}
    return c


COMMANDER = card("Krenko, Mob Boss", colors=["R"])

DATA = {"container": {"json_dict": {"cardlists": [
    {"tag": "creatures", "cardviews": [
        {"name": "Goblin A", "inclusion": 90},
        {"name": "Expensive Bomb", "inclusion": 85},
        {"name": "Goblin B", "inclusion": 80},
    ]},
    {"tag": "lands", "cardviews": [
        {"name": "Cool Land", "inclusion": 50},
    ]},
]}}}

CARDS = {
    "krenko, mob boss": COMMANDER,
    "goblin a": card("Goblin A", eur=0.2, colors=["R"]),
    "expensive bomb": card("Expensive Bomb", eur=100.0, colors=["R"]),
    "goblin b": card("Goblin B", eur=1.0, colors=["R"]),
    "cool land": card("Cool Land", eur=2.0),
    "mountain": card("Mountain"),
}


def _build(budget, owned=frozenset({"goblin a"})):
    return deckgen.build_deck(
        COMMANDER, DATA, set(owned), budget, CARDS, target_lands=4, deck_size=10
    )


def test_budget_excludes_too_expensive_and_buys_affordable():
    deck = _build(budget=5.0)
    names = {c["name"] for g in deck["groups"] for c in g["cards"]}
    assert "Goblin A" in names      # owned -> free
    assert "Goblin B" in names      # affordable -> bought
    assert "Expensive Bomb" not in names  # over budget -> skipped
    # Bought: Goblin B (1.0) + Cool Land (2.0)
    assert deck["buy_total_eur"] == 3.0
    assert deck["counts"]["to_buy"] == 2


def test_basic_lands_fill_to_target():
    deck = _build(budget=5.0)
    lands_group = next(g for g in deck["groups"] if g["label"] == "Terrains")
    basics = [c for c in lands_group["cards"] if c["is_basic"]]
    assert sum(b["qty"] for b in basics) == 3  # 4 target - 1 Cool Land
    assert all(b["name"] == "Mountain" for b in basics)  # mono-red identity
    assert deck["counts"]["lands"] == 4


def test_unfilled_spells_reported_when_pool_too_small():
    deck = _build(budget=5.0)
    # spells_target = 10 - 1 - 4 = 5, but only 2 non-land cards fit
    assert deck["counts"]["spells"] == 2
    assert deck["unfilled_spells"] == 3


def test_max_card_price_caps_individual_purchases_even_with_budget_left():
    # A generous total budget (30) shouldn't let a single card past a strict
    # per-card cap (1.5): Cool Land (2.0) is skipped even though the budget
    # has plenty of room, while the cheaper Goblin B (1.0) is still bought.
    deck = deckgen.build_deck(
        COMMANDER, DATA, {"goblin a"}, 30.0, CARDS, target_lands=4, deck_size=10,
        max_card_price=1.5,
    )
    names = {c["name"] for g in deck["groups"] for c in g["cards"]}
    assert "Goblin B" in names
    assert "Cool Land" not in names
    assert "Expensive Bomb" not in names
    assert deck["max_card_price_eur"] == 1.5
    assert deck["buy_total_eur"] == 1.0


def test_no_budget_includes_expensive_cards():
    deck = _build(budget=None)
    names = {c["name"] for g in deck["groups"] for c in g["cards"]}
    assert "Expensive Bomb" in names
    assert deck["buy_total_eur"] == 103.0  # 100 + 1 + 2


def test_decklist_text_has_commander_and_basics():
    deck = _build(budget=5.0)
    text = deck["decklist_text"]
    assert text.startswith("Commander\n1 Krenko, Mob Boss")
    assert "3 Mountain" in text


def test_sideboard_holds_relevant_cards_not_in_main_deck():
    # Under budget, Expensive Bomb is excluded from the 100 but is a relevant
    # alternative -> it should surface in the sideboard, flagged to buy.
    deck = _build(budget=5.0)
    main = {c["name"] for g in deck["groups"] for c in g["cards"]}
    sb_names = {c["name"] for c in deck["sideboard"]}
    assert "Expensive Bomb" in sb_names
    assert not (main & sb_names)  # no overlap with the main deck
    assert deck["counts"]["sideboard"] == len(deck["sideboard"])
    # Sideboard ignores the main budget so pricey staples still show up to buy.
    assert deck["counts"]["sideboard_to_buy"] == 1
    assert deck["sideboard_buy_total_eur"] == 100.0
    assert "Sideboard" in deck["decklist_text"]


def test_sideboard_empty_when_pool_fully_used():
    # Without a budget every candidate enters the main deck, leaving no leftovers.
    deck = _build(budget=None)
    assert deck["sideboard"] == []
    assert deck["counts"]["sideboard"] == 0


def test_cards_locked_in_other_decks_are_used_last_and_flagged():
    # Goblin A is owned but every copy sits in another deck: the generator
    # prefers buying Goblin B / Expensive Bomb, then falls back to Goblin A
    # only to fill the remaining slot — flagged from_deck.
    deck = deckgen.build_deck(
        COMMANDER, DATA, {"goblin a"}, None, CARDS, target_lands=4, deck_size=10,
        in_deck_keys={"goblin a"},
    )
    items = {c["name"]: c for g in deck["groups"] for c in g["cards"]}
    assert items["Goblin A"]["owned"] is True
    assert items["Goblin A"]["from_deck"] is True
    assert items["Goblin B"]["from_deck"] is False
    # Goblin A is free (owned), not in the buy list.
    assert "Goblin A" not in {c["name"] for c in deck["buy_list"]}
    assert deck["counts"]["from_decks"] == 1


def test_budget_exhausted_falls_back_to_deck_locked_cards():
    # With no money to buy anything, a card locked in another deck still fills
    # the deck rather than leaving the slot empty.
    deck = deckgen.build_deck(
        COMMANDER, DATA, {"goblin a"}, 0.0, CARDS, target_lands=4, deck_size=10,
        in_deck_keys={"goblin a"},
    )
    names = {c["name"] for g in deck["groups"] for c in g["cards"]}
    assert "Goblin A" in names
    assert deck["buy_total_eur"] == 0.0


def test_free_owned_cards_are_not_flagged_from_deck():
    deck = _build(budget=5.0)
    items = {c["name"]: c for g in deck["groups"] for c in g["cards"]}
    assert items["Goblin A"]["from_deck"] is False
    assert deck["counts"]["from_decks"] == 0
    assert deck["commander"]["from_deck"] is False


def test_min_basics_reserves_land_slots_for_basics():
    # Three nonbasic candidates could fill all 4 land slots; a min_basics floor
    # of 2 caps them at 2 (best inclusion first) and tops up with basics.
    data = {"container": {"json_dict": {"cardlists": [
        {"tag": "creatures", "cardviews": [{"name": "Goblin A", "inclusion": 90}]},
        {"tag": "lands", "cardviews": [
            {"name": "Cool Land", "inclusion": 50},
            {"name": "Nice Land", "inclusion": 40},
            {"name": "Meh Land", "inclusion": 30},
        ]},
    ]}}}
    cards = {
        **CARDS,
        "nice land": card("Nice Land", eur=1.0),
        "meh land": card("Meh Land", eur=1.0),
    }
    deck = deckgen.build_deck(
        COMMANDER, data, {"goblin a"}, None, cards, target_lands=4, deck_size=10,
        min_basics=2,
    )
    lands = next(g for g in deck["groups"] if g["label"] == "Terrains")
    by_name = {c["name"]: c for c in lands["cards"]}
    assert set(by_name) == {"Cool Land", "Nice Land", "Mountain"}
    assert "Meh Land" not in by_name          # capped by the basics floor
    assert by_name["Mountain"]["qty"] == 2    # the reserved basic slots
    assert deck["counts"]["lands"] == 4


def test_untyped_edhrec_section_falls_back_to_scryfall_type():
    # A card seen only in an untyped EDHREC section (High Synergy / Top Cards)
    # has no category tag: its Scryfall type line must place it in the right
    # group instead of "Autres".
    data = {"container": {"json_dict": {"cardlists": [
        {"tag": "highsynergycards", "cardviews": [
            {"name": "Synergy Trick", "inclusion": 95},
            {"name": "Mystery Blob", "inclusion": 60},
        ]},
        {"tag": "creatures", "cardviews": [{"name": "Goblin A", "inclusion": 90}]},
    ]}}}
    cards = {
        "krenko, mob boss": COMMANDER,
        "goblin a": card("Goblin A", eur=0.2, colors=["R"]),
        "synergy trick": {**card("Synergy Trick", eur=0.5, colors=["R"]),
                          "type_line": "Instant"},
        "mystery blob": card("Mystery Blob", eur=0.5, colors=["R"]),  # no type line
        "mountain": card("Mountain"),
    }
    deck = deckgen.build_deck(
        COMMANDER, data, {"goblin a"}, None, cards, target_lands=4, deck_size=10
    )
    by_label = {g["label"]: [c["name"] for c in g["cards"]] for g in deck["groups"]}
    assert "Synergy Trick" in by_label["Éphémères"]
    # Without any type information the card still falls back to "Autres".
    assert "Mystery Blob" in by_label["Autres"]


def test_groups_are_sorted_alphabetically():
    # Fill order is popularity/price driven (Goblin A owned, then Expensive
    # Bomb by inclusion, then Goblin B) but display must be alphabetical.
    deck = _build(budget=None)
    creatures = next(g for g in deck["groups"] if g["label"] == "Créatures")
    assert [c["name"] for c in creatures["cards"]] == \
        ["Expensive Bomb", "Goblin A", "Goblin B"]
    lands = next(g for g in deck["groups"] if g["label"] == "Terrains")
    assert [c["name"] for c in lands["cards"]] == ["Cool Land", "Mountain"]


def test_commander_excluded_from_candidates():
    nonland, lands = deckgen.candidate_names(DATA, "Krenko, Mob Boss")
    assert "Krenko, Mob Boss" not in nonland + lands


def test_build_deck_filters_pool_by_duelcommander_legality():
    # EDHREC has no Duel Commander page: the regular Commander page's pool is
    # reused, but a card banned there must be excluded from both the main deck
    # and the sideboard.
    cards = {
        **CARDS,
        "goblin a": {**CARDS["goblin a"], "legalities": {"duel": "legal"}},
        "goblin b": {**CARDS["goblin b"], "legalities": {"duel": "legal"}},
        "cool land": {**CARDS["cool land"], "legalities": {"duel": "legal"}},
        "expensive bomb": {**CARDS["expensive bomb"], "legalities": {"duel": "banned"}},
    }
    deck = deckgen.build_deck(
        COMMANDER, DATA, {"goblin a"}, None, cards, target_lands=4, deck_size=10,
        fmt="duelcommander",
    )
    names = {c["name"] for g in deck["groups"] for c in g["cards"]}
    assert "Expensive Bomb" not in names
    assert "Expensive Bomb" not in {c["name"] for c in deck["sideboard"]}
    assert deck["format"] == "duelcommander"


def test_include_cards_are_forced_in_even_past_budget():
    # The player explicitly asked for Expensive Bomb: it takes a slot before
    # any budget logic, its price still counts in (and here exhausts) the
    # total, and it is flagged forced for the UI/snapshot.
    deck = deckgen.build_deck(
        COMMANDER, DATA, {"goblin a"}, 5.0, CARDS, target_lands=4, deck_size=10,
        include_cards=["Expensive Bomb"],
    )
    items = {c["name"]: c for g in deck["groups"] for c in g["cards"]}
    assert "Expensive Bomb" in items
    assert items["Expensive Bomb"]["forced"] is True
    assert items["Goblin A"]["forced"] is False
    assert deck["forced_cards"] == ["Expensive Bomb"]
    # The forced purchase consumed the whole budget: nothing else is bought.
    assert deck["buy_total_eur"] == 100.0
    assert "Goblin B" not in items and "Cool Land" not in items


def test_include_cards_outside_edhrec_pool_are_injected():
    cards = {**CARDS, "off pool card": card("Off Pool Card", eur=0.5, colors=["R"])}
    deck = deckgen.build_deck(
        COMMANDER, DATA, set(), None, cards, target_lands=4, deck_size=10,
        include_cards=["Off Pool Card"],
    )
    items = {c["name"]: c for g in deck["groups"] for c in g["cards"]}
    assert items["Off Pool Card"]["forced"] is True
    assert "Off Pool Card" in deck["forced_cards"]


def test_exclude_cards_removed_from_deck_and_sideboard():
    deck = deckgen.build_deck(
        COMMANDER, DATA, {"goblin a"}, 5.0, CARDS, target_lands=4, deck_size=10,
        exclude_cards=["Goblin B", "Expensive Bomb"],
    )
    names = {c["name"] for g in deck["groups"] for c in g["cards"]}
    assert "Goblin B" not in names
    # Expensive Bomb used to land in the sideboard under this budget: an
    # explicit player exclusion must remove it from there too.
    assert "Expensive Bomb" not in {c["name"] for c in deck["sideboard"]}


def test_validate_includes_rejects_unknown_offcolor_and_illegal():
    resolved = {
        "goblin a": card("Goblin A", colors=["R"]),
        "blue guy": card("Blue Guy", colors=["U"]),
        "banned card": {**card("Banned Card", colors=["R"]),
                        "legalities": {"commander": "banned"}},
        "krenko, mob boss": COMMANDER,
    }
    valid, rejected = deckgen.validate_includes(
        ["Goblin A", "Blue Guy", "Banned Card", "Ghost Card", "Krenko, Mob Boss"],
        resolved, COMMANDER, "commander",
    )
    assert valid == ["Goblin A"]  # the commander itself is silently skipped
    reasons = {r["name"]: r["reason"] for r in rejected}
    assert "couleur" in reasons["Blue Guy"]
    assert "légale" in reasons["Banned Card"]
    assert "introuvable" in reasons["Ghost Card"]


def test_build_deck_plain_commander_keeps_all_cards_regardless_of_legality():
    # No legality filter applies to plain "commander" (unchanged behaviour):
    # a card with no legality data at all is still usable.
    deck = _build(budget=None)
    names = {c["name"] for g in deck["groups"] for c in g["cards"]}
    assert "Expensive Bomb" in names
    assert deck["format"] == "commander"
