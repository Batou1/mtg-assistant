"""The /collection page's filter plumbing: which input wins, and what the
query box / filter panel look like afterwards.

Exercised through the real route because the rules being checked are about form
state (hidden ``qgen`` / ``panel`` fields round-tripping), not about a single
function. No network: the DB is a throwaway file and the bulk-data thread is
disabled before ``app.main`` is imported.
"""
import importlib
import re

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """(TestClient, db) on a throwaway SQLite file holding three cards."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MTG_BULK_AUTO_REFRESH", "0")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from fastapi.testclient import TestClient
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    for name in ("app.scryfall", "app.collection", "app.nlquery", "app.main"):
        importlib.reload(importlib.import_module(name))
    import app.main as main

    pid = db.ensure_default_profile()
    db.set_card("goblin matron", _card("Goblin Matron", "{2}{R}", 3.0,
                                       "Creature — Goblin", ["R"], "uncommon"))
    db.set_card("sol ring", _card("Sol Ring", "{1}", 1.0, "Artifact", [], "uncommon"))
    db.set_card("counterspell", _card("Counterspell", "{U}{U}", 2.0, "Instant",
                                      ["U"], "common"))
    db.replace_collection(pid, [
        _row("Goblin Matron", 2), _row("Sol Ring", 4), _row("Counterspell", 1),
    ])
    return TestClient(main.app), db


def _card(name, cost, cmc, type_line, colors, rarity):
    return {
        "name": name, "mana_cost": cost, "cmc": cmc, "type_line": type_line,
        "oracle_text": "x", "colors": colors, "color_identity": colors,
        "keywords": [], "rarity": rarity, "set": "abc", "released_at": "2020-01-01",
        "legalities": {"commander": "legal"}, "prices": {"eur": "1.00"},
    }


def _row(name, qty):
    return {"scryfall_id": "", "name_key": name.lower(), "raw_name": name,
            "set_code": "ABC", "foil": 0, "condition": "nm", "quantity": qty,
            "binder_type": "binder"}


def _page(client, url):
    """(effective query, names shown, panel open?, panel-priority warning?)."""
    body = client.get(url).text
    query = re.search(r'name="qgen" value="([^"]*)"', body)
    panel = re.search(r'<details class="filter-panel"([^>]*)>', body)
    return {
        "query": (query.group(1) if query else "").replace("&lt;", "<").replace("&gt;", ">"),
        "names": re.findall(r'"name": "([^"]+)"', body),
        "panel_open": "open" in (panel.group(1) if panel else ""),
        "warned": "prioritaire" in body,
        "ask_value": (re.search(r'name="ask"[^>]*value="([^"]*)"', body) or [None, ""])[1],
    }


# --- The panel's open state is the user's own ----------------------------

def test_panel_stays_closed_when_filtering(client):
    """Applying a filter must not re-open the disclosure by itself."""
    c, _db = client
    assert _page(c, "/collection")["panel_open"] is False
    assert _page(c, "/collection?type=creature")["panel_open"] is False
    assert _page(c, "/collection?q=t:creature")["panel_open"] is False
    assert _page(c, "/collection?ask=mes+cr%C3%A9atures")["panel_open"] is False


def test_panel_stays_open_once_the_user_opened_it(client):
    c, _db = client
    assert _page(c, "/collection?panel=1&type=creature")["panel_open"] is True
    assert _page(c, "/collection?panel=0&type=creature")["panel_open"] is False


# --- The query box is the state -----------------------------------------

def test_panel_fields_are_compiled_into_the_query_box_and_cleared(client):
    c, _db = client
    page = _page(c, "/collection?type=creature&rarity=uncommon")
    assert page["query"] == "t:creature r:uncommon"
    assert page["names"] == ["Goblin Matron"]
    # The panel gave everything it had to the box; it must not keep it too.
    body = c.get("/collection?type=creature&rarity=uncommon").text
    assert 'value="creature" selected' not in body
    assert 'value="uncommon" selected' not in body


def test_panel_terms_refine_the_current_query(client):
    """qgen unchanged => the box was not edited => the panel adds to it."""
    c, _db = client
    page = _page(c, "/collection?q=t%3Acreature&qgen=t%3Acreature&rarity=uncommon")
    assert page["query"] == "t:creature r:uncommon"
    assert page["names"] == ["Goblin Matron"]


def test_refining_a_query_with_an_or_keeps_it_grouped(client):
    """`a or b` + a panel term must not bind the term to `b` alone."""
    c, _db = client
    page = _page(
        c, "/collection?q=t%3Aartifact+or+t%3Ainstant&qgen=t%3Aartifact+or+t%3Ainstant"
           "&rarity=uncommon")
    assert page["query"] == "(t:artifact or t:instant) r:uncommon"
    assert page["names"] == ["Sol Ring"]


def test_a_hand_edited_query_wins_over_the_panel(client):
    """q differs from qgen => the user typed it => nothing else is applied."""
    c, _db = client
    page = _page(c, "/collection?q=t%3Aartifact&qgen=t%3Acreature&rarity=common")
    assert page["query"] == "t:artifact"
    assert page["names"] == ["Sol Ring"]
    assert page["warned"] is True


def test_a_hand_edited_query_wins_over_the_french_box(client):
    c, _db = client
    page = _page(c, "/collection?q=t%3Aartifact&qgen=&ask=mes+cr%C3%A9atures+rouges")
    assert page["query"] == "t:artifact"
    assert page["names"] == ["Sol Ring"]
    assert page["warned"] is True


def test_clearing_the_box_by_hand_clears_the_filter(client):
    c, _db = client
    page = _page(c, "/collection?q=&qgen=t%3Acreature")
    assert page["query"] == ""
    assert len(page["names"]) == 3
    assert page["warned"] is False


def test_the_french_box_replaces_the_query_and_is_emptied(client):
    """Without an API key the heuristic answers; either way the box takes over."""
    c, _db = client
    page = _page(c, "/collection?ask=mes+cr%C3%A9atures+rouges")
    assert page["query"] == "id<=r t:creature"
    assert page["names"] == ["Goblin Matron"]
    assert page["ask_value"] == ""


def test_the_french_box_wins_over_the_panel(client):
    c, _db = client
    page = _page(c, "/collection?ask=mes+cr%C3%A9atures+rouges&rarity=common")
    assert page["query"] == "id<=r t:creature"


def test_an_untouched_query_survives_a_sort_change(client):
    c, _db = client
    page = _page(c, "/collection?q=t%3Acreature&qgen=t%3Acreature&sort=name")
    assert page["query"] == "t:creature"
    assert page["warned"] is False


def test_an_invalid_query_reports_instead_of_filtering(client):
    c, _db = client
    body = c.get("/collection?q=foo%3Abar").text
    assert "Filtre inconnu" in body
    assert re.findall(r'"name": "([^"]+)"', body) == []


def test_home_shows_scryfall_freshness_and_reddens_past_a_week(client):
    import time
    tc, db = client
    html = tc.get("/").text
    assert "Dernière update Scryfall" in html and "jamais" in html
    assert 'freshness stale' in html

    db.set_meta("bulk_oracle_cards_synced_at", str(time.time() - 2 * 3600))
    db.set_meta("bulk_all_cards_synced_at", str(time.time() - 2 * 3600))
    html = tc.get("/").text
    assert "il y a 2 heures" in html
    assert 'freshness stale' not in html

    db.set_meta("bulk_all_cards_synced_at", str(time.time() - 9 * 86400))
    html = tc.get("/").text
    assert "il y a 9 jours" in html and 'freshness stale' in html
