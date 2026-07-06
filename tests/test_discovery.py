"""Integration tests for commander discovery: propose commanders you don't own
but that your cards point to. Network-touching functions are mocked; the real
EDHREC extraction + scoring + budget logic runs."""
import importlib
import types

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
    "goblin bombardment": {
        "name": "Goblin Bombardment", "type_line": "Enchantment",
        "color_identity": ["R"], "prices": {"eur": "1.00"},
    },
    "goblin recruiter": {
        "name": "Goblin Recruiter", "type_line": "Creature — Goblin",
        "color_identity": ["R"], "prices": {"eur": "0.50"},
    },
    "krenko, mob boss": {
        "name": "Krenko, Mob Boss", "type_line": "Legendary Creature — Goblin",
        "color_identity": ["R"], "prices": {"eur": "0.50"},
        "image_uris": {"normal": "http://img/krenko.png"},
    },
}

_COMMANDER_PAGE = {
    "num_decks": 12000,
    "container": {"json_dict": {"cardlists": [
        {"tag": "creatures", "cardviews": [
            {"name": "Goblin Chieftain"},
            {"name": "Krenko's Command"},
            {"name": "Goblin Recruiter"},  # owned
        ]},
    ]}},
    "panels": {"taglinks": [{"slug": "goblins"}]},
}

_CARD_PAGE = {
    "container": {"json_dict": {"cardlists": [
        {"tag": "topcommanders", "cardviews": [{"name": "Krenko, Mob Boss"}]},
    ]}},
}


def _wire(analysis, monkeypatch, *, commander_page=_COMMANDER_PAGE):
    def fake_resolve_cards(names, client=None):
        res, nf = {}, []
        for n in names:
            k = n.split("//")[0].strip().lower()
            (res.__setitem__(k, _REGISTRY[k]) if k in _REGISTRY else nf.append(n))
        return res, nf

    monkeypatch.setattr(analysis.scryfall, "resolve_cards", fake_resolve_cards)
    monkeypatch.setattr(analysis.edhrec, "fetch_card", lambda name, client=None: _CARD_PAGE)
    monkeypatch.setattr(
        analysis.edhrec, "fetch_commander",
        lambda name, client=None, fmt="commander": commander_page
    )
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
            "theme": "gobelins", "keywords": ["goblins"], "budget_eur": 50.0,
            "source": "llm"}
    base.update(over)
    return base


def test_discovers_unowned_commander_linked_to_owned_cards(env, monkeypatch):
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Goblin Bombardment"), _row("Goblin Recruiter")])
    _wire(analysis, monkeypatch)

    data = analysis.analyze(_intent(), pid)

    # No owned commanders; Krenko is proposed from the two owned goblin cards.
    assert data["proposed_count"] == 1
    proposed = [r for r in data["results"] if not r["owned"]]
    assert len(proposed) == 1
    krenko = proposed[0]
    assert krenko["name"] == "Krenko, Mob Boss"
    assert krenko["owned"] is False
    assert krenko["price_eur"] == 0.5
    assert krenko["link_count"] == 2
    assert set(krenko["linked_owned_cards"]) == {"Goblin Bombardment", "Goblin Recruiter"}
    # owned recommended card (Goblin Recruiter) is counted in completion.
    assert krenko["owned_count"] == 1
    # Total cost folds the commander's own price into the buylist total.
    assert krenko["total_cost_eur"] == 2.5
    # Discovery diagnostics surfaced.
    assert data["discovery"]["queried_cards"] == 2
    assert data["discovery"]["candidate_count"] == 1


def test_proposed_commander_over_budget_is_excluded(env, monkeypatch):
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Goblin Bombardment")])
    _wire(analysis, monkeypatch)

    # Krenko costs 0.50 €; a 0.10 € budget can't even afford the commander.
    data = analysis.analyze(_intent(budget_eur=0.10), pid)
    assert data["proposed_count"] == 0


def test_owned_commander_not_re_proposed(env, monkeypatch):
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    # The player already owns Krenko: it must surface as owned, never proposed.
    db.replace_collection(pid, [_row("Krenko, Mob Boss"), _row("Goblin Bombardment")])
    _wire(analysis, monkeypatch)

    data = analysis.analyze(_intent(), pid)
    assert data["proposed_count"] == 0
    assert any(r["owned"] and r["name"] == "Krenko, Mob Boss" for r in data["results"])


def test_discovery_can_be_disabled(env, monkeypatch):
    import dataclasses
    db, analysis = env.db, env.analysis
    monkeypatch.setattr(
        analysis, "settings",
        dataclasses.replace(analysis.settings, discovery_enabled=False),
    )
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Goblin Bombardment"), _row("Goblin Recruiter")])
    _wire(analysis, monkeypatch)

    data = analysis.analyze(_intent(), pid)
    assert data["proposed_count"] == 0


# --- Duel Commander / Pauper Commander --------------------------------------

def _wire_resolver(analysis, monkeypatch, registry):
    """Point scryfall.resolve_cards at a custom (name -> card) registry."""
    def fake_resolve_cards(names, client=None):
        res, nf = {}, []
        for n in names:
            k = n.split("//")[0].strip().lower()
            (res.__setitem__(k, registry[k]) if k in registry else nf.append(n))
        return res, nf

    monkeypatch.setattr(analysis.scryfall, "resolve_cards", fake_resolve_cards)


def test_analyze_threads_format_to_edhrec_fetch(env, monkeypatch):
    """analyze() must fetch the requested Commander variant's EDHREC page."""
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Krenko, Mob Boss")])
    _wire(analysis, monkeypatch)
    _wire_resolver(analysis, monkeypatch, {
        **_REGISTRY,
        "krenko, mob boss": {**_REGISTRY["krenko, mob boss"], "legalities": {"duel": "legal"}},
    })

    seen_fmts = []

    def fake_fetch_commander(name, client=None, fmt="commander"):
        seen_fmts.append(fmt)
        return _COMMANDER_PAGE

    monkeypatch.setattr(analysis.edhrec, "fetch_commander", fake_fetch_commander)

    data = analysis.analyze(_intent(format="duelcommander"), pid)
    assert "duelcommander" in seen_fmts
    assert data["format"] == "duelcommander"


def test_analyze_excludes_commander_banned_in_duelcommander(env, monkeypatch):
    """A card banned in Duel Commander must not surface as an owned commander,
    even though it's a legal (structural) commander in plain Commander."""
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Krenko, Mob Boss")])
    _wire(analysis, monkeypatch)
    _wire_resolver(analysis, monkeypatch, {
        **_REGISTRY,
        "krenko, mob boss": {**_REGISTRY["krenko, mob boss"], "legalities": {"duel": "banned"}},
    })

    data = analysis.analyze(_intent(format="duelcommander"), pid)
    assert data["candidate_count"] == 0
    assert not any(r["name"] == "Krenko, Mob Boss" for r in data["results"])


def test_analyze_unknown_format_falls_back_to_commander(env, monkeypatch):
    db, analysis = env.db, env.analysis
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Krenko, Mob Boss")])
    _wire(analysis, monkeypatch)

    data = analysis.analyze(_intent(format="modern"), pid)
    assert data["format"] == "commander"
    assert any("modern" in n for n in data["notices"])
    assert any(r["name"] == "Krenko, Mob Boss" for r in data["results"])
