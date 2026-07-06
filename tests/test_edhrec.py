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


def test_fetch_commander_always_uses_the_regular_commander_page(env, monkeypatch):
    # EDHREC has no separate Duel/Pauper Commander section: every lookup goes
    # through the one regular (paper) Commander JSON endpoint, regardless of
    # which variant the caller is ultimately building a suggestion/deck for.
    calls = []

    def fake_get_with_retry(client, url, attempts=5):
        calls.append(url)
        return {"num_decks": 1}

    monkeypatch.setattr(env.edhrec, "_get_with_retry", fake_get_with_retry)

    env.edhrec.fetch_commander("Krenko, Mob Boss")
    assert calls == ["https://json.edhrec.com/pages/commanders/krenko-mob-boss.json"]

    # A second call for the same commander is served from cache (no new request).
    env.edhrec.fetch_commander("Krenko, Mob Boss")
    assert len(calls) == 1


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
