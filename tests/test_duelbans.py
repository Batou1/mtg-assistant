"""Duel Commander "banned as commander" list (app/duelbans.py). Offline: the
official page is served by a monkeypatched ``webfetch.get_text``."""
import importlib
import types

import pytest

_PAGE = """
<p id="update-info"><strong>Last Updated:</strong> July 27, 2026
</p>
<section class="card ban-category" aria-labelledby="banned-commanders-heading">
  <div class="ban-grid" id="banned-commanders" role="list">
    <div class="ban-item banned commander-restricted" role="listitem" data-card-name="Edgar Markov">x</div>
    <div class="ban-item banned commander-restricted" role="listitem" data-card-name="Minsc &amp;amp; Boo, Timeless Heroes">x</div>
    <div class="ban-item banned commander-restricted" role="listitem" data-card-name="Ajani, Nacatl Pariah // Ajani, Nacatl Avenger">x</div>
  </div>
</section>
<section class="card ban-category">
  <div class="ban-item banned" role="listitem" data-card-name="Sol Ring">x</div>
</section>
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    import app.config as config
    importlib.reload(config)
    mods = {}
    for name in ("db", "webfetch", "duelbans", "commanders", "deckgen"):
        mods[name] = importlib.reload(importlib.import_module(f"app.{name}"))
    mods["db"].init_db()
    mods["duelbans"].clear_cache()
    yield types.SimpleNamespace(**mods)
    mods["duelbans"].clear_cache()


def _serve(env, monkeypatch, body):
    calls = []

    def fake(client, url, encoding=None, attempts=4):
        calls.append(url)
        return body

    monkeypatch.setattr(env.webfetch, "get_text", fake)
    return calls


def _legend(name):
    return {"name": name, "type_line": "Legendary Creature — Vampire",
            "oracle_text": "", "legalities": {"duel": "legal", "commander": "legal"}}


def test_parse_only_reads_the_banned_as_commander_section(env):
    data = env.duelbans.parse(_PAGE)
    assert data == {"names": ["Ajani, Nacatl Pariah // Ajani, Nacatl Avenger",
                              "Edgar Markov", "Minsc & Boo, Timeless Heroes"],
                    "updated": "July 27, 2026"}
    assert env.duelbans.parse("<html>redesigned</html>") is None


def test_banned_as_commander_is_rejected_only_in_duel(env, monkeypatch):
    calls = _serve(env, monkeypatch, _PAGE)
    edgar = _legend("Edgar Markov")
    assert not env.commanders.is_eligible_commander(edgar, "duelcommander")
    assert env.commanders.is_eligible_commander(edgar, "commander")
    assert env.commanders.is_eligible_commander(_legend("Krenko, Mob Boss"), "duelcommander")
    # Double-faced: matched on the front face; double-escaped names unescaped.
    assert env.duelbans.is_banned_as_commander("Ajani, Nacatl Pariah")
    assert env.duelbans.is_banned_as_commander("Minsc & Boo, Timeless Heroes")
    assert len(calls) == 1  # memoised across checks


def test_commander_warning_says_it_stays_legal_in_the_99(env, monkeypatch):
    _serve(env, monkeypatch, _PAGE)
    warning = env.deckgen.commander_warning(_legend("Edgar Markov"), "duelcommander")
    assert "COMME COMMANDANT" in warning and "99" in warning and "July 27, 2026" in warning


def test_outage_keeps_the_last_known_list(env, monkeypatch):
    _serve(env, monkeypatch, _PAGE)
    assert env.duelbans.is_banned_as_commander("Edgar Markov")
    # Page cache expired and the site is down: the meta copy still applies.
    env.db.set_edhrec("duelbans:page", {"names": [], "updated": ""})
    with env.db.get_conn() as conn:
        conn.execute("DELETE FROM edhrec WHERE slug='duelbans:page'")
    env.duelbans.clear_cache()
    _serve(env, monkeypatch, None)
    assert env.duelbans.is_banned_as_commander("Edgar Markov")


def test_without_any_list_scryfall_restricted_is_the_fallback(env, monkeypatch):
    calls = _serve(env, monkeypatch, None)
    assert env.commanders.is_eligible_commander(_legend("Edgar Markov"), "duelcommander")
    restricted = {**_legend("Edgar Markov"), "legalities": {"duel": "restricted"}}
    assert not env.commanders.is_eligible_commander(restricted, "duelcommander")
    assert "Scryfall" in env.deckgen.commander_warning(restricted, "duelcommander")
    assert len(calls) == 1  # a failure is not retried on every check


def test_the_official_list_wins_over_a_stale_scryfall_status(env, monkeypatch):
    # Unbanned by the committee (absent from the page), still "restricted" on
    # Scryfall for a few days: the official list decides.
    _serve(env, monkeypatch, _PAGE)
    najeela = {**_legend("Najeela, the Blade-Blossom"), "legalities": {"duel": "restricted"}}
    assert env.commanders.is_eligible_commander(najeela, "duelcommander")
