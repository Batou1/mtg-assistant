from app import commanders


def _card(type_line, legalities=None):
    return {"name": "Test Card", "type_line": type_line, "oracle_text": "",
            "legalities": legalities or {}}


def test_plain_commander_ignores_legality():
    # No Scryfall legality entry for "commander": structural check is enough,
    # even without a "legalities" field at all (existing behaviour preserved).
    card = _card("Legendary Creature — Human")
    assert commanders.is_eligible_commander(card, "commander")


def test_duelcommander_requires_duel_legality():
    banned = _card("Legendary Creature — Human", {"duel": "banned"})
    assert not commanders.is_eligible_commander(banned, "duelcommander")

    legal = _card("Legendary Creature — Human", {"duel": "legal"})
    assert commanders.is_eligible_commander(legal, "duelcommander")


def test_paupercommander_commander_is_an_uncommon_creature_legendary_or_not():
    # Scryfall marks uncommons "not_legal" in paupercommander (they are illegal
    # in the 99): that status must NOT reject them as commander.
    devil = {**_card("Creature — Devil", {"paupercommander": "not_legal"}),
             "rarity": "uncommon"}
    assert commanders.is_eligible_commander(devil, "paupercommander")

    rare_legend = {**_card("Legendary Creature — Goblin", {"paupercommander": "not_legal"}),
                   "rarity": "rare"}
    assert not commanders.is_eligible_commander(rare_legend, "paupercommander")

    common = {**_card("Creature — Elf", {"paupercommander": "legal"}), "rarity": "common"}
    assert not commanders.is_eligible_commander(common, "paupercommander")

    banned = {**_card("Creature — Human", {"paupercommander": "banned"}),
              "rarity": "uncommon"}
    assert not commanders.is_eligible_commander(banned, "paupercommander")

    background = {**_card("Legendary Enchantment — Background"), "rarity": "uncommon"}
    assert commanders.is_eligible_commander(background, "paupercommander")


def test_paupercommander_uses_any_uncommon_printing(monkeypatch):
    # The canonical printing is a rare reprint, but the card was printed at
    # uncommon once: the printing index (bulk import) makes it eligible.
    monkeypatch.setattr(commanders.prices, "printed_rarities",
                        lambda name: {"uncommon", "rare"})
    card = {**_card("Creature — Elf"), "rarity": "rare"}
    assert commanders.is_eligible_commander(card, "paupercommander")


def test_non_commander_card_never_eligible():
    sorcery = _card("Sorcery", {"duel": "legal", "paupercommander": "legal"})
    assert not commanders.is_eligible_commander(sorcery, "commander")
    assert not commanders.is_eligible_commander(sorcery, "duelcommander")
    assert not commanders.is_eligible_commander(sorcery, "paupercommander")


def test_formats_and_legality_map():
    assert commanders.FORMATS == {"commander", "duelcommander", "paupercommander"}
    assert commanders.SCRYFALL_LEGALITY == {"duelcommander": "duel",
                                            "paupercommander": "paupercommander"}
