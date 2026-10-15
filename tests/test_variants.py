"""Commander-variant card sources: PDHREC (Pauper Commander), MTGTop8 (Duel
Commander), the EDHREC fallback, and how deckgen uses them. No network: the
scrapers' ``webfetch.get_text`` is monkeypatched with canned pages."""
import importlib
import types

import pytest

_PDH_PAGE = """
<html><body>
  <a class="gallery-item" href="#"><img alt="Fynn, the Fangbearer" src="x">
    <div class="info">In 1,079 decks</div></a>
  <a class="gallery-item" synergy="0.87" popularity="1019" typeline="Creature">
    <img alt="Ambush Viper" src="x"><div class="info">Ambush Viper <br/> In 94.4% of 1079 decks</div></a>
  <a class="gallery-item" synergy="0.49" popularity="734" typeline="Instant">
    <img alt="Ram Through" src="x"></a>
  <a class="gallery-item" synergy="0.26" popularity="582" typeline="Land">
    <img alt="Tranquil Thicket" src="x"></a>
  <a class="gallery-item" synergy="0.1" popularity="300" typeline="Artifact Creature">
    <img alt="Ornithopter" src="x"></a>
</body></html>
"""

_PDH_LIST = """
<a href="/commander/dina-soul-steeper/" class="gallery-item"><img alt="Dina, Soul Steeper">
  <div class="info">In 672 decks</div></a>
<a href="/commander/mayhem-devil/" class="gallery-item"><img alt="Mayhem Devil">
  <div class="info">In 410 decks</div></a>
"""

_T8_SEARCH = ("<div onclick=\"AddCom('ecl285','1','Brigid, Clachan&#039;s Heart');\">x</div>"
              "<div onclick=\"AddCom('ecl1018','1','Brigid, Doun&#039;s Mind');\">x</div>")
_T8_DECKS = ('<div class=S14 align=center>4 decks / 5365</div>'
             '<a href=/event?e=1&d=101&f=EDH>a</a><a href=/event?e=1&d=102&f=EDH>b</a>'
             '<a href=/event?e=2&d=103&f=EDH>c</a><a href=/event?e=2&d=104&f=EDH>d</a>')
_T8_DECK_A = """1 Llanowar Elves
1 Swords to Plowshares
1 Windswept Heath
7 Snow-Covered Forest
Sideboard
1 Brigid, Clachan's Heart
"""
_T8_DECK_B = """1 Llanowar Elves
1 Windswept Heath
1 Savannah
6 Plains
Sideboard
1 Brigid, Clachan's Heart
"""
_T8_META = ("<div><a href=archetype?a=2902&meta=209&f=EDH&show=pop>Brigid, Clachan&#039;s Heart</a></div>"
            "<div align=center><img src=/graph/manas/G.png><img src=/graph/manas/W.png></div>"
            "<div class=S14 align=center>58.7 <span>&permil;</span></div>")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MTG_REQUEST_DELAY", "0")
    import app.config as config
    importlib.reload(config)
    mods = {}
    for name in ("db", "webfetch", "edhrec", "pdhrec", "mtgtop8", "variants", "deckgen"):
        mod = importlib.import_module(f"app.{name}")
        mods[name] = importlib.reload(mod)
    mods["db"].init_db()
    return types.SimpleNamespace(**mods)


def _serve(env, monkeypatch, pages: dict):
    """Route webfetch.get_text by URL substring; record every request."""
    calls = []

    def fake(client, url, encoding=None, attempts=4):
        calls.append(url)
        for needle, body in pages.items():
            if needle in url:
                return body
        return env.webfetch.NOT_FOUND

    monkeypatch.setattr(env.webfetch, "get_text", fake)
    return calls


# --- PDHREC --------------------------------------------------------------

def test_pdhrec_page_becomes_an_edhrec_shaped_payload(env):
    data = env.pdhrec.parse_commander_page(_PDH_PAGE, "Fynn, the Fangbearer")
    assert data["num_decks"] == 1079
    assert env.edhrec.extract_num_decks(data) == 1079
    sections = {s["tag"]: s["cardviews"] for s in data["container"]["json_dict"]["cardlists"]}
    assert sections["creatures"][0] == {"name": "Ambush Viper", "inclusion": 1019,
                                        "num_decks": 1019, "synergy": 0.87}
    assert [v["name"] for v in sections["lands"]] == ["Tranquil Thicket"]
    # An artifact creature is a creature; the commander tile is not a card.
    assert "Ornithopter" in [v["name"] for v in sections["creatures"]]
    ordered = env.edhrec.extract_recommended_ordered(data)
    assert "Fynn, the Fangbearer" not in ordered
    assert ordered[0] == "Ambush Viper"


def test_pdhrec_slugs_match_the_site():
    from app import pdhrec
    assert pdhrec.slugify("Fynn, the Fangbearer") == "fynn-the-fangbearer"
    assert pdhrec.slugify("Abdel Adrian, Gorion's Ward") == "abdel-adrian-gorions-ward"
    assert pdhrec.slugify("Black Waltz No. 3") == "black-waltz-no-3"
    assert (pdhrec.slugify("Exdeath, Void Warlock // Neo Exdeath, Dimension's End")
            == "exdeath-void-warlock--neo-exdeath-dimensions-end")
    assert pdhrec.color_page(["G", "B"]) == "golgari"
    assert pdhrec.color_page(["W", "U", "B", "R"]) is None  # no 4-colour pages


def test_pdhrec_fetch_is_cached_and_an_empty_page_is_not_found(env, monkeypatch):
    calls = _serve(env, monkeypatch, {"/commander/fynn-the-fangbearer/": _PDH_PAGE,
                                      "/commander/nobody/": "<html>no cards</html>"})
    data = env.pdhrec.fetch_commander("Fynn, the Fangbearer")
    assert data["_source"] == "pdhrec" and data["_url"].endswith("/fynn-the-fangbearer/")
    env.pdhrec.fetch_commander("Fynn, the Fangbearer")
    assert len(calls) == 1
    assert env.pdhrec.fetch_commander("Nobody") == {"_not_found": True}


def test_pdhrec_commander_lists(env, monkeypatch):
    calls = _serve(env, monkeypatch, {"/colors/golgari/": _PDH_LIST})
    rows = env.pdhrec.fetch_commanders(["B", "G"], pages=1)
    assert rows == [
        {"name": "Dina, Soul Steeper", "slug": "dina-soul-steeper", "num_decks": 672},
        {"name": "Mayhem Devil", "slug": "mayhem-devil", "num_decks": 410},
    ]
    assert calls[0].endswith("/colors/golgari/")


# --- MTGTop8 -------------------------------------------------------------

def test_mtgtop8_parsers():
    from app import mtgtop8
    assert mtgtop8.parse_search(_T8_SEARCH, "Brigid, Clachan's Heart") == "ecl285"
    assert mtgtop8.parse_search(_T8_SEARCH, "Brigid") is None  # exact match only
    assert mtgtop8.parse_deck_list(_T8_DECKS) == {"total": 4,
                                                  "ids": ["101", "102", "103", "104"]}
    deck = mtgtop8.parse_deck(_T8_DECK_A)
    assert deck["commanders"] == ["Brigid, Clachan's Heart"]
    assert [7, "Snow-Covered Forest"] in deck["main"]
    assert mtgtop8.parse_metagame(_T8_META) == [
        {"name": "Brigid, Clachan's Heart", "colors": ["G", "W"], "share": 58.7}]


def _typed(name, type_line):
    return {"name": name, "type_line": type_line}


_TYPES = {
    "llanowar elves": _typed("Llanowar Elves", "Creature — Elf Druid"),
    "swords to plowshares": _typed("Swords to Plowshares", "Instant"),
    "windswept heath": _typed("Windswept Heath", "Land"),
    "savannah": _typed("Savannah", "Land — Forest Plains"),
}


def test_mtgtop8_aggregate_counts_decks_and_measures_the_manabase():
    from app import mtgtop8
    decks = [mtgtop8.parse_deck(_T8_DECK_A), mtgtop8.parse_deck(_T8_DECK_B)]
    data = mtgtop8.aggregate("Brigid, Clachan's Heart", decks, 150, _TYPES)
    views = {v["name"]: v["inclusion"]
             for s in data["container"]["json_dict"]["cardlists"] for v in s["cardviews"]}
    # Basics are left to deckgen; the commander is never in its own pool.
    assert views == {"Llanowar Elves": 2, "Windswept Heath": 2,
                     "Swords to Plowshares": 1, "Savannah": 1}
    assert data["num_decks"] == 150 and data["sample_size"] == 2
    # Deck A: heath + 7 snow forests = 8 lands, 7 basics; deck B: 2 + 6 = 8, 6.
    assert data["deck_stats"] == {"lands": 8.0, "basics": 6.5}


def test_mtgtop8_fetch_commander_end_to_end(env, monkeypatch):
    calls = _serve(env, monkeypatch, {
        "cEDH_card_search": _T8_SEARCH,
        "show=decks": _T8_DECKS,
        "d=101": _T8_DECK_A, "d=102": _T8_DECK_B,
        "d=103": _T8_DECK_A, "d=104": _T8_DECK_B,
    })
    monkeypatch.setattr(env.mtgtop8.scryfall, "resolve_cards",
                        lambda names, client=None: (_TYPES, []))
    data = env.mtgtop8.fetch_commander("Brigid, Clachan's Heart")
    assert data["_source"] == "mtgtop8"
    assert data["sample_size"] == 4
    n = len(calls)
    assert env.mtgtop8.fetch_commander("Brigid, Clachan's Heart") == data
    assert len(calls) == n  # aggregate cached


def test_mtgtop8_too_few_tournament_decks_is_not_found(env, monkeypatch):
    _serve(env, monkeypatch, {
        "cEDH_card_search": _T8_SEARCH,
        "show=decks": '<div class=S14>1 decks / 5365</div><a href=/event?e=1&d=101&f=EDH>a</a>',
    })
    assert env.mtgtop8.fetch_commander("Brigid, Clachan's Heart") == {"_not_found": True}


# --- Dispatcher ----------------------------------------------------------

_EDHREC_PAGE = {"num_decks": 5000, "container": {"json_dict": {"cardlists": [
    {"tag": "creatures", "cardviews": [{"name": "Llanowar Elves", "inclusion": 10}]}]}}}


def test_variant_falls_back_to_edhrec_and_says_so(env, monkeypatch):
    _serve(env, monkeypatch, {})  # PDHREC: 404 for everything
    monkeypatch.setattr(env.edhrec, "fetch_commander", lambda name, client=None: _EDHREC_PAGE)
    data = env.variants.fetch_commander("Some Uncommon", "paupercommander")
    assert data["_fallback"] is True and data["_source"] == "edhrec"
    info = env.variants.source_info(data, "paupercommander")
    assert info["fallback"] and not info["native"]
    assert "EDHREC" in env.variants.describe(info)


def test_variant_uses_its_native_source(env, monkeypatch):
    _serve(env, monkeypatch, {"/commander/fynn-the-fangbearer/": _PDH_PAGE})
    monkeypatch.setattr(env.edhrec, "fetch_commander",
                        lambda name, client=None: pytest.fail("EDHREC must not be used"))
    data = env.variants.fetch_commander("Fynn, the Fangbearer", "paupercommander")
    info = env.variants.source_info(data, "paupercommander")
    assert info["native"] and info["label"] == "PDHREC" and info["num_decks"] == 1079


def test_a_transient_variant_error_is_never_reported_as_not_found(env, monkeypatch):
    monkeypatch.setattr(env.webfetch, "get_text", lambda *a, **k: None)
    monkeypatch.setattr(env.edhrec, "fetch_commander",
                        lambda name, client=None: {"_not_found": True})
    assert env.variants.fetch_commander("X", "paupercommander") == {"_error": True}


def test_duel_scoring_only_aggregates_commanders_of_the_metagame(env, monkeypatch):
    calls = _serve(env, monkeypatch, {"show=pop": _T8_META})
    monkeypatch.setattr(env.edhrec, "fetch_commander", lambda name, client=None: _EDHREC_PAGE)
    data = env.variants.fetch_commander("Krenko, Mob Boss", "duelcommander", scoring=True)
    assert data["_fallback"] is True
    assert not any("cEDH_card_search" in u for u in calls)


# --- deckgen -------------------------------------------------------------

def test_manabase_targets_follow_the_measured_decks():
    from app import deckgen
    assert deckgen.manabase_targets({}, 36, 18) == (36, 18)
    assert deckgen.manabase_targets({"deck_stats": {"lands": 38.8, "basics": 11.8}},
                                    36, 18) == (39, 12)
    assert deckgen.manabase_targets({"deck_stats": {"lands": 60, "basics": 50}},
                                    36, 18) == (42, 42)


def test_commander_warning_for_an_illegal_pdh_commander():
    from app import deckgen
    rare = {"name": "Krenko, Mob Boss", "type_line": "Legendary Creature — Goblin",
            "rarity": "rare", "legalities": {"paupercommander": "not_legal"}}
    assert "peu commune" in deckgen.commander_warning(rare, "paupercommander")
    assert deckgen.commander_warning(rare, "commander") is None


def test_complement_cards_only_fill_slots_the_primary_pool_cannot():
    """A short PDHREC/tournament pool is topped up from EDHREC, but an owned or
    cheap complement card never displaces a primary-source card."""
    from app import deckgen, variants

    def c(name, eur, type_line="Creature — Elf"):
        return {"name": name, "type_line": type_line, "color_identity": ["G"],
                "prices": {"eur": str(eur)}, "legalities": {"paupercommander": "legal"}}

    primary = {"_source": "pdhrec", "num_decks": 10, "container": {"json_dict": {"cardlists": [
        {"tag": "creatures", "cardviews": [{"name": "Native A", "inclusion": 10},
                                           {"name": "Native B", "inclusion": 5}]}]}}}
    edh = {"num_decks": 999, "container": {"json_dict": {"cardlists": [
        {"tag": "creatures", "cardviews": [{"name": "Native A", "inclusion": 900},
                                           {"name": "Edh Owned", "inclusion": 800},
                                           {"name": "Edh Cheap", "inclusion": 700}]}]}}}
    merged = variants._with_complement(primary, edh)
    commander = {"name": "Cmd", "type_line": "Creature — Elf", "color_identity": ["G"]}
    cards = {k: c(n, e) for k, n, e in [("native a", "Native A", 5), ("native b", "Native B", 9),
                                        ("edh owned", "Edh Owned", 1), ("edh cheap", "Edh Cheap", 0.1)]}
    deck = deckgen.build_deck(commander, merged, {"edh owned"}, 100, cards,
                              target_lands=0, deck_size=3, fmt="paupercommander")
    names = [x["name"] for g in deck["groups"] for x in g["cards"]]
    assert sorted(names) == ["Native A", "Native B"]

    deck = deckgen.build_deck(commander, merged, {"edh owned"}, 100, cards,
                              target_lands=0, deck_size=4, fmt="paupercommander")
    names = [x["name"] for g in deck["groups"] for x in g["cards"]]
    assert sorted(names) == ["Edh Owned", "Native A", "Native B"]


def test_pdhrec_staples_parse_and_top_up_within_the_colour_identity(env, monkeypatch):
    page = ('<a class="gallery-item" color-identity=U><img alt="Counterspell">'
            '<div class="info">Counterspell: 60.6%</div></a>'
            '<a class="gallery-item" color-identity=><img alt="Ash Barrens">'
            '<div class="info">Ash Barrens: 40%</div></a>'
            '<a class="gallery-item" color-identity=R><img alt="Lightning Bolt">'
            '<div class="info">Lightning Bolt: 55%</div></a>')
    assert env.pdhrec.parse_staples(page)[0] == {"name": "Counterspell",
                                                 "color_identity": ["U"], "pct": 60.6}
    _serve(env, monkeypatch, {"/staples": page})
    cards = {
        "blue guy": {"name": "Blue Guy", "type_line": "Creature", "color_identity": ["U"]},
        "counterspell": {"name": "Counterspell", "type_line": "Instant"},
        "ash barrens": {"name": "Ash Barrens", "type_line": "Land"},
        "lightning bolt": {"name": "Lightning Bolt", "type_line": "Instant"},
    }
    monkeypatch.setattr(env.variants.scryfall, "resolve_cards",
                        lambda names, client=None: ({k: v for k, v in cards.items()}, []))
    data = env.variants._pdh_staples_payload("Blue Guy", None)
    sections = {s["tag"]: [v["name"] for v in s["cardviews"]]
                for s in data["container"]["json_dict"]["cardlists"]}
    # Off-colour Lightning Bolt is left out; the land is typed as a land.
    assert sections == {"instants": ["Counterspell"], "lands": ["Ash Barrens"]}
