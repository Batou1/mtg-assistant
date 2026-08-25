"""Player-style memory: deck signals from the ManaBox library, event log,
LLM synthesis with card-name anchoring, heuristic fallback, and the home-page
card. No network: the LLM is monkeypatched, the DB is a temp file."""
import importlib
import json
import types

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Reload config/db/playerprofile against a throwaway SQLite file."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MTG_BULK_AUTO_REFRESH", "0")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.collection as collection
    importlib.reload(collection)
    import app.playerprofile as playerprofile
    importlib.reload(playerprofile)
    db.init_db()
    return types.SimpleNamespace(db=db, pp=playerprofile)


def _card(name, identity, type_line="Creature — Goblin"):
    return {
        "name": name, "mana_cost": "{R}", "cmc": 1.0, "type_line": type_line,
        "oracle_text": "x", "colors": identity, "color_identity": identity,
        "keywords": [], "rarity": "rare", "set": "abc",
        "released_at": "2020-01-01", "legalities": {"commander": "legal"},
        "prices": {"eur": "1.00"},
    }


def _row(name, qty=1, binder_type="binder", binder_name=""):
    return {"scryfall_id": "", "name_key": name.split("//")[0].strip().lower(),
            "raw_name": name, "set_code": "ABC", "foil": 0, "condition": "nm",
            "quantity": qty, "binder_type": binder_type,
            "binder_name": binder_name}


def _seed(env):
    db = env.db
    pid = db.ensure_default_profile()
    db.set_card("krenko, mob boss",
                _card("Krenko, Mob Boss", ["R"], "Legendary Creature — Goblin"))
    db.set_card("goblin matron", _card("Goblin Matron", ["R"]))
    db.set_card("counterspell", _card("Counterspell", ["U"], "Instant"))
    db.replace_collection(pid, [
        _row("Krenko, Mob Boss", 1, "deck", "Gobelins"),
        _row("Goblin Matron", 2, "deck", "Gobelins"),
        _row("Counterspell", 4),
    ])
    return pid


# --- Deck signals from the ManaBox library --------------------------------

def test_deck_memberships_groups_by_deck_name(env):
    pid = _seed(env)
    decks = env.db.deck_memberships(pid)
    assert set(decks) == {"Gobelins"}
    assert {c["raw_name"] for c in decks["Gobelins"]} == {
        "Krenko, Mob Boss", "Goblin Matron"
    }


def test_deck_without_name_still_counts(env):
    db = env.db
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Goblin Matron", 1, "deck")])
    decks = db.deck_memberships(pid)
    assert list(decks) == [""]


def test_deck_signals_resolve_colors_and_commanders(env):
    pid = _seed(env)
    decks = env.pp._deck_signals(pid)
    assert len(decks) == 1
    assert decks[0]["name"] == "Gobelins"
    assert decks[0]["colors"] == ["R"]
    assert decks[0]["commanders"] == ["Krenko, Mob Boss"]


# --- Heuristic synthesis (no API key) -------------------------------------

def test_refresh_heuristic_builds_and_stores_profile(env):
    pid = _seed(env)
    profile = env.pp.refresh(pid)
    assert profile is not None
    assert profile["source"] == "heuristic"
    assert "Gobelins" in profile["summary"]
    assert profile["colors"][0] == "R"  # deck colors outweigh the collection
    stored = env.pp.get_profile(pid)
    assert stored["summary"] == profile["summary"]
    assert stored["deck_count"] == 1


def test_refresh_skips_when_signals_unchanged(env):
    pid = _seed(env)
    assert env.pp.refresh(pid) is not None
    assert env.pp.refresh(pid) is None          # same fingerprint: skipped
    assert env.pp.refresh(pid, force=True) is not None


def test_refresh_without_any_signal_returns_none(env):
    pid = env.db.ensure_default_profile()
    assert env.pp.refresh(pid) is None
    assert env.pp.get_profile(pid) is None


def test_event_updates_fingerprint_and_profile(env):
    pid = _seed(env)
    env.pp.refresh(pid)
    env.pp.record_event(pid, "analyse", {"wish": "un deck contrôle bleu",
                                         "format": "modern"})
    profile = env.pp.refresh(pid)
    assert profile is not None
    assert "modern" in profile["formats"]


# --- LLM synthesis + anchoring --------------------------------------------

def test_llm_profile_drops_invented_cards_and_colors(env, monkeypatch):
    pid = _seed(env)
    monkeypatch.setattr(env.pp.llm, "is_available", lambda: True)
    monkeypatch.setattr(env.pp.llm, "chat_json", lambda system, user: {
        "summary": "Tu aimes les gobelins agressifs.",
        "style_tags": ["gobelins", "aggro"],
        "colors": ["R", "Z"],
        "formats": ["Commander"],
        "favorite_cards": ["Krenko, Mob Boss", "Black Lotus"],
    })
    profile = env.pp.refresh(pid)
    assert profile["source"] == "llm"
    assert profile["colors"] == ["R"]
    # Black Lotus was never in the signals: anchored out (invariant 1).
    assert profile["favorite_cards"] == ["Krenko, Mob Boss"]
    assert profile["formats"] == ["commander"]


def test_llm_failure_falls_back_to_heuristic(env, monkeypatch):
    pid = _seed(env)
    monkeypatch.setattr(env.pp.llm, "is_available", lambda: True)
    monkeypatch.setattr(env.pp.llm, "chat_json", lambda system, user: None)
    profile = env.pp.refresh(pid)
    assert profile["source"] == "heuristic"


def test_key_arrival_upgrades_heuristic_profile(env, monkeypatch):
    pid = _seed(env)
    assert env.pp.refresh(pid)["source"] == "heuristic"
    monkeypatch.setattr(env.pp.llm, "is_available", lambda: True)
    monkeypatch.setattr(env.pp.llm, "chat_json", lambda system, user: {
        "summary": "Tu aimes le rouge.", "style_tags": [], "colors": ["R"],
        "formats": [], "favorite_cards": [],
    })
    # Signals unchanged, but the stored portrait is heuristic and the LLM is
    # now available: refresh must regenerate instead of skipping.
    assert env.pp.refresh(pid)["source"] == "llm"


# --- Event log bounds ------------------------------------------------------

def test_event_log_is_bounded(env):
    pid = env.db.ensure_default_profile()
    for i in range(env.pp._EVENTS_MAX + 10):
        env.pp.record_event(pid, "analyse", {"wish": f"wish {i}"})
    events = env.pp._events(pid)
    assert len(events) == env.pp._EVENTS_MAX
    assert events[-1]["wish"] == f"wish {env.pp._EVENTS_MAX + 9}"


# --- Chat integration ------------------------------------------------------

def test_style_prompt_block_contains_summary(env):
    pid = _seed(env)
    assert env.pp.style_prompt_block(pid) == ""
    env.pp.refresh(pid)
    block = env.pp.style_prompt_block(pid)
    assert "PROFIL DU JOUEUR" in block
    assert "Gobelins" in block


def test_chat_signals_read_wishes_and_artifacts(env):
    db = env.db
    pid = db.ensure_default_profile()
    cid = db.create_conversation(pid)
    db.add_message(cid, "user", "un deck aristocrates noir")
    db.add_message(cid, "assistant", "voilà", artifacts=[
        {"type": "decklist", "commander": "Krenko, Mob Boss",
         "deck": {"format": "commander"}},
    ])
    signals = env.pp._chat_signals(pid)
    assert signals["wishes"] == ["un deck aristocrates noir"]
    assert any("Krenko" in g for g in signals["generated"])


# --- Profile deletion cleans the memory ------------------------------------

def test_delete_profile_drops_style_memory(env):
    db = env.db
    pid = db.create_profile("Alice")
    db.set_meta(f"player_profile:{pid}", json.dumps({"summary": "x"}))
    db.set_meta(f"profile_events:{pid}", "[]")
    db.delete_profile(pid)
    assert db.get_meta(f"player_profile:{pid}") is None
    assert db.get_meta(f"profile_events:{pid}") is None


# --- ManaBox parsing carries the deck name ---------------------------------

def test_manabox_parses_binder_name():
    import app.manabox as manabox
    csv_text = (
        "Binder Name,Binder Type,Name,Set code,Quantity\n"
        "Mon deck gobelins,deck,Krenko Mob Boss,DOM,1\n"
        "Classeur,binder,Counterspell,MH2,4\n"
    )
    rows, errors = manabox.parse_manabox_csv(csv_text)
    assert not errors
    assert rows[0]["binder_name"] == "Mon deck gobelins"
    assert rows[0]["binder_type"] == "deck"
    assert rows[1]["binder_name"] == "Classeur"


# --- Home page card ---------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MTG_BULK_AUTO_REFRESH", "0")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    for name in ("app.scryfall", "app.collection", "app.playerprofile",
                 "app.chat", "app.main"):
        importlib.reload(importlib.import_module(name))
    import app.main as main
    import app.playerprofile as playerprofile
    return TestClient(main.app), db, playerprofile


def test_home_shows_style_card(client):
    http, db, pp = client
    body = http.get("/").text
    assert "Comment l'app te perçoit" in body
    assert "ne te connaît pas encore" in body

    pid = db.ensure_default_profile()
    db.set_meta(f"player_profile:{pid}", json.dumps({
        "summary": "Tu aimes les gobelins agressifs.",
        "style_tags": ["gobelins"], "colors": ["R"], "formats": ["commander"],
        "favorite_cards": [], "source": "heuristic", "updated_at": 1700000000,
        "deck_count": 2, "conversation_count": 1, "fingerprint": "x",
    }))
    body = http.get("/").text
    assert "Tu aimes les gobelins agressifs." in body
    assert "gobelins" in body
    assert "2 deck(s)" in body
