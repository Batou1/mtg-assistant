"""Tests for EDHREC payload extraction (pure parsing, no network)."""
import importlib
import types

import pytest

from app import edhrec


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Reload config/db/edhrec against a throwaway SQLite file."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.edhrec as edh
    importlib.reload(edh)
    db.init_db()
    return types.SimpleNamespace(db=db, edhrec=edh)


def test_fetch_commander_uses_format_specific_base_url(env, monkeypatch):
    calls = []

    def fake_get_with_retry(client, url, attempts=5):
        calls.append(url)
        return {"num_decks": 1}

    monkeypatch.setattr(env.edhrec, "_get_with_retry", fake_get_with_retry)

    env.edhrec.fetch_commander("Krenko, Mob Boss")
    env.edhrec.fetch_commander("Krenko, Mob Boss", fmt="duelcommander")
    env.edhrec.fetch_commander("Krenko, Mob Boss", fmt="paupercommander")

    assert calls[0] == "https://json.edhrec.com/pages/commanders/krenko-mob-boss.json"
    assert calls[1] == "https://json.edhrec.com/pages/commanders/duel/krenko-mob-boss.json"
    assert calls[2] == "https://json.edhrec.com/pages/commanders/pauper/krenko-mob-boss.json"


def test_fetch_commander_caches_per_format_without_collision(env, monkeypatch):
    # Different formats for the same commander name must be cached separately
    # (a card could be a legal Duel Commander AND Pauper Commander pick with
    # very different popularity data on each page).
    pages = {
        "https://json.edhrec.com/pages/commanders/krenko-mob-boss.json": {"num_decks": 100},
        "https://json.edhrec.com/pages/commanders/duel/krenko-mob-boss.json": {"num_decks": 7},
    }
    monkeypatch.setattr(env.edhrec, "_get_with_retry", lambda client, url, attempts=5: pages[url])

    normal = env.edhrec.fetch_commander("Krenko, Mob Boss")
    duel = env.edhrec.fetch_commander("Krenko, Mob Boss", fmt="duelcommander")
    assert normal["num_decks"] == 100
    assert duel["num_decks"] == 7

    # Cached rows are reused (no second call needed) and keep their own format.
    monkeypatch.setattr(env.edhrec, "_get_with_retry",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should be cached")))
    assert env.edhrec.fetch_commander("Krenko, Mob Boss")["num_decks"] == 100
    assert env.edhrec.fetch_commander("Krenko, Mob Boss", fmt="duelcommander")["num_decks"] == 7


def _page(sections):
    return {"container": {"json_dict": {"cardlists": sections}}}


def test_extract_top_commanders_matches_by_tag():
    data = _page([
        {"tag": "topcommanders", "cardviews": [
            {"name": "Krenko, Mob Boss"}, {"name": "Krenko, Tin Street Kingpin"}]},
        {"tag": "creatures", "cardviews": [{"name": "Goblin Chieftain"}]},
    ])
    assert edhrec.extract_top_commanders(data) == [
        "Krenko, Mob Boss", "Krenko, Tin Street Kingpin"]


def test_extract_top_commanders_matches_by_header():
    data = _page([
        {"header": "Top Commanders", "cardviews": [{"name": "Prosper, Tome-Bound"}]},
    ])
    assert edhrec.extract_top_commanders(data) == ["Prosper, Tome-Bound"]


def test_extract_top_commanders_dedupes_and_ignores_other_sections():
    data = _page([
        {"tag": "commanders", "cardviews": [{"name": "A"}, {"name": "A"}, {"name": "B"}]},
        {"tag": "instants", "cardviews": [{"name": "Lightning Bolt"}]},
    ])
    assert edhrec.extract_top_commanders(data) == ["A", "B"]


def test_extract_top_commanders_empty_when_no_commander_section():
    data = _page([{"tag": "creatures", "cardviews": [{"name": "Goblin Chieftain"}]}])
    assert edhrec.extract_top_commanders(data) == []
