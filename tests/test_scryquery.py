"""Scryfall-syntax parsing and matching (app/scryquery.py)."""
import pytest

from app import scryquery as sq

MATRON = {
    "name": "Goblin Matron", "mana_cost": "{2}{R}", "cmc": 3.0,
    "type_line": "Creature — Goblin",
    "oracle_text": "When Goblin Matron enters, search your library for a Goblin card.",
    "colors": ["R"], "color_identity": ["R"], "keywords": [],
    "rarity": "uncommon", "set": "mh3", "set_name": "Modern Horizons 3",
    "released_at": "2024-06-14", "power": "1", "toughness": "1",
    "legalities": {"commander": "legal", "modern": "legal", "legacy": "banned"},
    "prices": {"eur": "1.20"}, "artist": "Rebecca Guay", "layout": "normal",
}
MATRON_EXTRA = {"qty": 3, "deck_qty": 1, "line_total": 3.6, "resolved": True}

SOL_RING = {
    "name": "Sol Ring", "mana_cost": "{1}", "cmc": 1.0, "type_line": "Artifact",
    "oracle_text": "{T}: Add {C}{C}.", "colors": [], "color_identity": [],
    "keywords": [], "rarity": "uncommon", "set": "c21", "released_at": "2021-04-23",
    "legalities": {"commander": "legal"}, "prices": {"eur": "1.90"},
    "reserved": False, "layout": "normal",
}

WURM = {
    "name": "Wurm // Beast", "cmc": 6.0, "colors": ["G", "U"],
    "color_identity": ["G", "U"], "keywords": ["Trample"], "rarity": "mythic",
    "set": "znr", "released_at": "2020-09-25", "layout": "modal_dfc",
    "legalities": {"commander": "legal"},
    "card_faces": [
        {"name": "Wurm", "mana_cost": "{4}{G}{G}", "type_line": "Creature — Wurm",
         "oracle_text": "Trample", "power": "8", "toughness": "8"},
        {"name": "Beast", "mana_cost": "{U}", "type_line": "Land",
         "oracle_text": "{T}: Add {U}."},
    ],
}


def match(query, card=MATRON, extra=None):
    return sq.parse(query).match(card, extra if extra is not None else MATRON_EXTRA)


@pytest.mark.parametrize("query, expected", [
    ("", True),
    ("goblin", True),
    ("matron goblin", True),
    ("sol", False),
    ('!"Goblin Matron"', True),
    ('!"goblin"', False),
    ("name=/^goblin/", True),
    ("t:creature", True),
    ("t:goblin", True),          # subtypes live on the same type line
    ("t:instant", False),
    ("-t:instant", True),
    ("mv:3", True), ("mv<=3", True), ("mv>3", False),
    ("pow:1", True), ("tou>=2", False),
    ('o:"search your library"', True),
    ("o:/^when/", True),
    ("o:~", True),               # `~` stands for the card's own name
    ("r:uncommon", True), ("r>=rare", False), ("r<rare", True), ("r:u", True),
    ("c:r", True), ("c=r", True), ("c:rg", False), ("c>=r", True),
    ("id<=rg", True), ("id:rg", True), ("id=r", True),
    ("c:m", False), ("is:mono", True), ("c:1", True),
    ("kw:flying", False),
    ("s:mh3", True), ("e:c21", False),
    ("f:modern", True), ("f:legacy", False), ("banned:legacy", True),
    ("eur<=2", True), ("eur>5", False),
    ("year>=2024", True), ("year<2024", False),
    ('a:"rebecca guay"', True),
    ("m:{R}", True), ("m:{2}{R}", True), ("m:{G}", False), ("m={2}{R}", True),
    ("is:permanent", True), ("is:commander", False), ("is:legendary", False),
    ("is:vanilla", False), ("not:vanilla", True),
])
def test_single_filters(query, expected):
    assert match(query) is expected


@pytest.mark.parametrize("query, expected", [
    ("t:creature or t:instant", True),
    ("t:instant or t:sorcery", False),
    ("(t:instant or t:sorcery) mv<=3", False),
    ("-(t:instant or t:sorcery)", True),
    ("t:goblin AND mv:3", True),
    ("t:goblin mv:3 -eur>5", True),
])
def test_boolean_composition(query, expected):
    assert match(query) is expected


def test_collection_keys_read_the_extra_mapping():
    assert match("qty>=3") is True
    assert match("qty>3") is False
    assert match("deck>0") is True
    assert match("is:indeck") is True
    assert match("is:spare") is True           # 3 owned, 1 in a deck -> 2 free
    assert match("total>=3", extra={"qty": 3, "deck_qty": 1, "line_total": 3.6}) is True
    assert match("is:indeck", extra={"qty": 1, "deck_qty": 0}) is False
    assert match("is:spare", extra={"qty": 1, "deck_qty": 1}) is False


def test_unresolved_cards_are_findable():
    """A just-imported card has no Scryfall data yet — it must still show up."""
    extra = {"qty": 1, "deck_qty": 0, "resolved": False}
    assert match("is:unresolved", card={"name": "Mystery Card"}, extra=extra) is True
    assert match("mystery", card={"name": "Mystery Card"}, extra=extra) is True
    assert match("t:creature", card={"name": "Mystery Card"}, extra=extra) is False


def test_faces_are_searched_on_both_sides():
    """Double-faced cards match on either face (type, text, power)."""
    extra = {"qty": 1, "deck_qty": 0}
    assert match("t:land", card=WURM, extra=extra) is True
    assert match("t:wurm", card=WURM, extra=extra) is True
    assert match("pow>=8", card=WURM, extra=extra) is True
    assert match("is:dfc", card=WURM, extra=extra) is True
    assert match("c:gu", card=WURM, extra=extra) is True
    assert match("m:{G}{G}", card=WURM, extra=extra) is True


def test_colorless_and_identity_semantics():
    extra = {"qty": 1, "deck_qty": 0}
    # `id:` is a subset test: a colorless card fits any commander's identity.
    assert match("id<=r", card=SOL_RING, extra=extra) is True
    assert match("c:r", card=SOL_RING, extra=extra) is False
    assert match("c:c", card=SOL_RING, extra=extra) is True
    assert match("is:colorless", card=SOL_RING, extra=extra) is True
    assert match("id:izzet", card=SOL_RING, extra=extra) is True


def test_quoted_values_keep_their_spaces_and_parens():
    assert match('o:"for a Goblin card"') is True
    assert match('o:"for a Goblin card" t:creature') is True
    assert match("o:'a Goblin card.'") is True


@pytest.mark.parametrize("query", [
    "foo:bar",          # unknown key
    "mv:abc",           # non-numeric value for a numeric filter
    "r:legendary",      # not a rarity
    "is:nope",          # unknown boolean predicate
    "(t:creature",      # unbalanced parenthesis
    "t:creature)",
    "t:",               # missing value
    "o:/[/",            # invalid regex
    "-",
])
def test_bad_queries_raise_with_a_message(query):
    with pytest.raises(sq.QueryError) as exc:
        sq.parse(query)
    assert str(exc.value)


def test_scryfall_only_keys_are_accepted_and_ignored():
    """A query pasted from scryfall.com must still run here."""
    assert match("lang:fr game:paper unique:prints t:goblin") is True


def test_inline_order_is_exposed():
    query = sq.parse("order:price dir:asc t:creature")
    assert (query.order, query.direction) == ("price", "asc")
    assert query.match(MATRON, MATRON_EXTRA) is True


def test_quote_only_quotes_when_needed():
    assert sq.quote("goblin") == "goblin"
    assert sq.quote("draw a card") == '"draw a card"'
    assert sq.quote("  ") == ""
