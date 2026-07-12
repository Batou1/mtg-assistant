"""Tests for the theme-first commander finder (independent of the collection):
candidates come from EDHREC theme/typal pages and the local Scryfall pool, not
from the owned cards. Network-touching functions are mocked; the real
extraction + filtering + scoring logic runs."""
import importlib
import types
from datetime import date, timedelta

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.analysis as analysis
    importlib.reload(analysis)
    db.init_db()
    return types.SimpleNamespace(db=db, analysis=analysis)


def _row(name):
    key = name.split("//")[0].strip().lower()
    return {"scryfall_id": "", "name_key": key, "raw_name": name,
            "set_code": "ABC", "foil": 0, "condition": "nm", "quantity": 1}


# Scryfall card dicts the mocked resolver returns.
_REGISTRY = {
    "krenko, mob boss": {
        "name": "Krenko, Mob Boss", "type_line": "Legendary Creature — Goblin",
        "color_identity": ["R"], "prices": {"eur": "0.50"},
        "oracle_text": "{T}: Create X 1/1 red Goblin creature tokens.",
    },
    "muxus, goblin grandee": {
        "name": "Muxus, Goblin Grandee", "type_line": "Legendary Creature — Goblin",
        "color_identity": ["R"], "prices": {"eur": "12.00"},
        "oracle_text": "When Muxus enters, reveal Goblin cards.",
    },
    "wort, boggart auntie": {
        "name": "Wort, Boggart Auntie", "type_line": "Legendary Creature — Goblin",
        "color_identity": ["B", "R"], "prices": {"eur": "1.00"},
        "oracle_text": "Return target Goblin card from your graveyard.",
    },
    "goblin recruiter": {
        "name": "Goblin Recruiter", "type_line": "Creature — Goblin",
        "color_identity": ["R"], "prices": {"eur": "0.50"},
    },
    # A commander that only exists in a set released two months ago.
    "shiny, brand-new boss": {
        "name": "Shiny, Brand-New Boss", "type_line": "Legendary Creature — Goblin",
        "color_identity": ["R"], "prices": {"eur": "2.00"},
        "released_at": (date.today() - timedelta(days=60)).isoformat(),
        "reprint": False,
    },
}

# EDHREC theme page: top commanders of the "goblins" theme, best first.
_THEME_PAGE = {
    "container": {"json_dict": {"cardlists": [
        {"tag": "topcommanders", "cardviews": [
            {"name": "Krenko, Mob Boss"},
            {"name": "Muxus, Goblin Grandee"},
            {"name": "Wort, Boggart Auntie"},
        ]},
    ]}},
}

_COMMANDER_PAGE = {
    "num_decks": 12000,
    "container": {"json_dict": {"cardlists": [
        {"tag": "creatures", "cardviews": [
            {"name": "Goblin Chieftain"},
            {"name": "Goblin Recruiter"},
        ]},
    ]}},
    "panels": {"taglinks": [{"slug": "goblins"}]},
}


def _wire(analysis, monkeypatch, *, theme_page=_THEME_PAGE, local_cards=()):
    def fake_resolve_cards(names, client=None):
        res, nf = {}, []
        for n in names:
            k = n.split("//")[0].strip().lower()
            (res.__setitem__(k, _REGISTRY[k]) if k in _REGISTRY else nf.append(n))
        return res, nf

    monkeypatch.setattr(analysis.scryfall, "resolve_cards", fake_resolve_cards)
    monkeypatch.setattr(analysis.edhrec, "fetch_theme", lambda slug, client=None: theme_page)
    monkeypatch.setattr(
        analysis.edhrec, "fetch_commander", lambda name, client=None: _COMMANDER_PAGE
    )
    monkeypatch.setattr(analysis.cardsearch, "search", lambda **kw: list(local_cards))
    monkeypatch.setattr(
        analysis.buylist, "build",
        lambda missing, budget, max_card_price=None, client=None: {
            "budget_eur": budget, "max_card_price_eur": max_card_price, "to_buy": [],
            "total_eur": 2.0, "bought_count": 1, "considered": len(missing),
            "missing_total": len(missing), "unpriced": 0, "over_cap": 0,
        },
    )


def _intent(**over):
    base = {"format": "commander", "colors": ["R"], "max_colors": None,
            "theme": "gobelins", "keywords": ["goblins"], "budget_eur": None,
            "source": "llm"}
    base.update(over)
    return base


def test_finds_theme_commanders_with_empty_collection(env, monkeypatch):
    """The search must work with NO collection at all — candidates come from
    the EDHREC theme page, not from owned cards."""
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    _wire(analysis, monkeypatch)

    data = analysis.find_commanders(_intent(), pid)

    assert data["finder"] is True
    names = [r["name"] for r in data["results"]]
    # Wort (B/R) is excluded by the mono-R colour filter; the two others stay.
    assert "Krenko, Mob Boss" in names
    assert "Muxus, Goblin Grandee" in names
    assert "Wort, Boggart Auntie" not in names
    assert all(r["owned"] is False for r in data["results"])
    assert data["proposed_count"] == len(data["results"])
    # An unowned commander carries its own price + total acquisition cost.
    krenko = next(r for r in data["results"] if r["name"] == "Krenko, Mob Boss")
    assert krenko["price_eur"] == 0.5
    assert krenko["total_cost_eur"] == 2.5


def test_owned_commander_is_flagged_and_completion_uses_collection(env, monkeypatch):
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Krenko, Mob Boss"), _row("Goblin Recruiter")])
    _wire(analysis, monkeypatch)

    data = analysis.find_commanders(_intent(), pid)
    krenko = next(r for r in data["results"] if r["name"] == "Krenko, Mob Boss")
    assert krenko["owned"] is True
    assert krenko["price_eur"] is None  # owned: no acquisition price
    # Collection overlap is still reported (Goblin Recruiter is recommended).
    assert krenko["owned_count"] == 1
    assert "Goblin Recruiter" in krenko["owned_cards"]


def test_unowned_commander_over_budget_is_excluded(env, monkeypatch):
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    _wire(analysis, monkeypatch)

    # Muxus costs 12 €; a 5 € budget can't even afford the commander.
    data = analysis.find_commanders(_intent(budget_eur=5.0), pid)
    names = [r["name"] for r in data["results"]]
    assert "Krenko, Mob Boss" in names
    assert "Muxus, Goblin Grandee" not in names


def test_local_scryfall_pool_complements_when_no_theme_page(env, monkeypatch):
    """No EDHREC theme page for the wording: candidates still come from the
    local Scryfall pool (legendaries matching the theme), with a notice."""
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    _wire(
        analysis, monkeypatch,
        theme_page={"_not_found": True},
        local_cards=[_REGISTRY["krenko, mob boss"]],
    )

    data = analysis.find_commanders(_intent(), pid)
    assert [r["name"] for r in data["results"]] == ["Krenko, Mob Boss"]
    assert data["themes"] == []
    assert any("Scryfall" in n for n in data["notices"])


def test_non_commander_format_falls_back_to_commander(env, monkeypatch):
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    _wire(analysis, monkeypatch)

    data = analysis.find_commanders(_intent(format="modern"), pid)
    assert data["format"] == "commander"
    assert any("modern" in n for n in data["notices"])


def test_recent_set_commander_is_demoted_not_excluded(env, monkeypatch):
    """EDHREC lists the newest hyped commander first; at equal theme fit it
    must rank BELOW established ones — being from a fresh set is not a
    criterion — while still appearing in the results."""
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    hyped_first = {
        "container": {"json_dict": {"cardlists": [
            {"tag": "topcommanders", "cardviews": [
                {"name": "Shiny, Brand-New Boss"},   # EDHREC hype order
                {"name": "Krenko, Mob Boss"},
                {"name": "Muxus, Goblin Grandee"},
            ]},
        ]}},
    }
    _wire(analysis, monkeypatch, theme_page=hyped_first)

    data = analysis.find_commanders(_intent(), pid)
    names = [r["name"] for r in data["results"]]
    assert names[-1] == "Shiny, Brand-New Boss"      # demoted, not dropped
    assert names[0] == "Krenko, Mob Boss"
    shiny = next(r for r in data["results"] if r["name"] == "Shiny, Brand-New Boss")
    assert shiny["recent"] is True
    assert all(r["recent"] is False for r in data["results"] if r["name"] != shiny["name"])


def test_recent_reprint_of_an_old_commander_is_not_demoted(env, monkeypatch):
    # The cached canonical printing of an old commander is often a recent
    # reprint: reprint=True must shield it from the novelty demotion.
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    reprinted = dict(
        _REGISTRY["krenko, mob boss"],
        released_at=(date.today() - timedelta(days=30)).isoformat(),
        reprint=True,
    )
    monkeypatch.setitem(_REGISTRY, "krenko, mob boss", reprinted)
    _wire(analysis, monkeypatch)

    data = analysis.find_commanders(_intent(), pid)
    krenko = next(r for r in data["results"] if r["name"] == "Krenko, Mob Boss")
    assert krenko["recent"] is False


def test_scan_depth_lets_older_commander_win_the_display_cut(env, monkeypatch):
    """With limit=1 the finder must still score candidates beyond the first
    viable one: the hyped new commander tops EDHREC's list but the established
    one wins the single display slot."""
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    hyped_first = {
        "container": {"json_dict": {"cardlists": [
            {"tag": "topcommanders", "cardviews": [
                {"name": "Shiny, Brand-New Boss"},
                {"name": "Krenko, Mob Boss"},
            ]},
        ]}},
    }
    _wire(analysis, monkeypatch, theme_page=hyped_first)

    data = analysis.find_commanders(_intent(), pid, limit=1)
    assert [r["name"] for r in data["results"]] == ["Krenko, Mob Boss"]


def test_theme_slugs_bounded_and_skip_long_free_text(env):
    analysis = env.analysis
    slugs = analysis._theme_slugs({
        "keywords": ["aristocrats", "sacrifice"],
        "theme": "un deck aristocrats sacrifice en noir rouge",
    })
    # The long free-text theme is not probed as a slug; keywords are.
    assert slugs == ["aristocrats", "sacrifice"]

    short = analysis._theme_slugs({"keywords": [], "theme": "group hug"})
    assert short == ["group-hug"]
