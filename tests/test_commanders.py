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


def test_paupercommander_requires_paupercommander_legality():
    # A legendary creature not eligible as a Pauper Commander (e.g. too rare).
    ineligible = _card("Legendary Creature — Human", {"paupercommander": "not_legal"})
    assert not commanders.is_eligible_commander(ineligible, "paupercommander")

    eligible = _card("Legendary Creature — Human", {"paupercommander": "legal"})
    assert commanders.is_eligible_commander(eligible, "paupercommander")


def test_non_commander_card_never_eligible():
    sorcery = _card("Sorcery", {"duel": "legal", "paupercommander": "legal"})
    assert not commanders.is_eligible_commander(sorcery, "commander")
    assert not commanders.is_eligible_commander(sorcery, "duelcommander")
    assert not commanders.is_eligible_commander(sorcery, "paupercommander")


def test_formats_and_legality_map():
    assert commanders.FORMATS == {"commander", "duelcommander", "paupercommander"}
    assert commanders.SCRYFALL_LEGALITY == {"duelcommander": "duel",
                                            "paupercommander": "paupercommander"}
