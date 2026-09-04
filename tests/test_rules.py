"""Comprehensive Rules corpus (app/rules.py): parsing the official .txt,
storage + lookup, keyword search, and the refresh cycle against a fake
rules page. No network: httpx is replaced by a stub client."""
import importlib
import time
import types

import httpx
import pytest

# A miniature of the real document: header, table of contents (which repeats
# the section/chapter headings), numbered rules with subrules and examples, a
# wrapped line, then the glossary and credits.
SAMPLE_CR = """﻿Magic: The Gathering Comprehensive Rules

These rules are effective as of August 7, 2026.

Introduction

This document is the ultimate authority for Magic: The Gathering® competitive game play.

Contents

1. Game Concepts

100. General

7. Additional Rules

702. Keyword Abilities

704. State-Based Actions

Glossary

Credits


1. Game Concepts

100. General

100.1. These Magic rules apply to any Magic game with two or more players.

100.1a A two-player game is a game that begins with only two players.

100.2. To play, each player needs their own deck of traditional Magic cards.

7. Additional Rules

702. Keyword Abilities

702.1. Most abilities describe exactly what they do in the object's rules text.

702.2. Deathtouch

702.2a Deathtouch is a static ability.

702.2c Any nonzero amount of combat damage assigned to a creature by a source with deathtouch is considered to be lethal damage, regardless of that creature's toughness. See rules 510.1c–d.

702.19. Trample

702.19a Trample is a static ability that modifies the rules for assigning an attacking creature's combat damage.

702.19b The controller of an attacking creature with trample first assigns damage to the creature(s) blocking it. Once all those blocking creatures are assigned lethal damage, any excess damage is assigned as its controller chooses among those blocking creatures and the player the creature is attacking.
Example: A 2/2 creature that can block an additional creature blocks two attackers: a 1/1 with no abilities and a 3/3 with trample.
Example: A 6/6 green creature with trample is blocked by a 2/2 creature with protection from green.

702.19c Trample over planeswalkers is a variant of trample
that modifies the rules for assigning combat damage to planeswalkers.

704. State-Based Actions

704.5. The state-based actions are as follows:

704.5h If a creature has toughness greater than 0, and it's been dealt damage by a source with deathtouch since the last time state-based actions were checked, that creature is destroyed.

Glossary

Deathtouch
A keyword ability that causes damage dealt by an object to be especially effective. See rule 702.2, "Deathtouch."

Trample
A keyword ability that modifies how a creature assigns combat damage. See rule 702.19, "Trample."

Trample Over Planeswalkers
A variant of trample. See rule 702.19, "Trample"

Credits

Magic: The Gathering Original Game Design: Richard Garfield
"""

PAGE_HTML = """<html><body>
<a href="https://media.wizards.com/2026/downloads/MagicCompRules 20260807.docx">DOCX</a>
<a href="https://media.wizards.com/2026/downloads/MagicCompRules 20260819.txt">TXT</a>
</body></html>"""
TXT_URL = "https://media.wizards.com/2026/downloads/MagicCompRules%2020260819.txt"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """(rules, db) on a throwaway SQLite file, empty corpus."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MTG_BULK_AUTO_REFRESH", "0")
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.rules as rules
    importlib.reload(rules)
    db.init_db()
    return types.SimpleNamespace(rules=rules, db=db)


@pytest.fixture()
def loaded(env):
    env.rules.store(env.rules.parse(SAMPLE_CR), "test")
    return env


# --- Parsing -------------------------------------------------------------

def test_parse_structure(env):
    parsed = env.rules.parse(SAMPLE_CR)
    assert parsed["effective"] == "2026-08-07"
    by = {r[0]: r for r in parsed["rules"]}
    # TOC headings are merged with the body ones: one row per number.
    assert [r[0] for r in parsed["rules"]].count("702") == 1
    assert by["702"][1] == "chapter" and by["702"][3] == "Keyword Abilities"
    assert by["7"][1] == "section" and by["7"][3] == "Additional Rules"
    assert by["702.19b"][1] == "rule"
    assert by["702.19b"][2] == "Keyword Abilities"           # enclosing chapter
    assert by["702.19b"][3].startswith("The controller of an attacking creature")
    # Two examples attached to the rule above them.
    assert by["702.19b"][4].count("\n") == 1
    assert by["702.19b"][4].startswith("A 2/2 creature")
    # A wrapped line folds into the rule.
    assert by["702.19c"][3] == (
        "Trample over planeswalkers is a variant of trample that modifies the rules "
        "for assigning combat damage to planeswalkers."
    )
    assert by["100.1a"][3] == "A two-player game is a game that begins with only two players."
    # The glossary starts after the rules, stops at the credits.
    terms = [g[0] for g in parsed["glossary"]]
    assert terms == ["Deathtouch", "Trample", "Trample Over Planeswalkers"]
    assert parsed["glossary"][1][1].endswith('See rule 702.19, "Trample."')
    assert not any("Richard Garfield" in g[1] for g in parsed["glossary"])


def test_parse_empty_document(env):
    parsed = env.rules.parse("nothing here")
    assert parsed["rules"] == [] and parsed["glossary"] == []
    assert parsed["effective"] is None


def test_format_date_fr(env):
    assert env.rules.format_date_fr("2026-08-07") == "7 août 2026"
    assert env.rules.format_date_fr("2026-01-01") == "1er janvier 2026"
    assert env.rules.format_date_fr(None) == ""
    assert env.rules.format_date_fr("bizarre") == "bizarre"


# --- Storage + lookup ---------------------------------------------------

def test_store_and_status(loaded):
    rules, db = loaded.rules, loaded.db
    assert db.rules_count() == 13
    assert rules.is_loaded()
    st = rules.status()
    assert st["loaded"] and st["count"] == 13
    assert st["effective"] == "2026-08-07" and st["effective_fr"] == "7 août 2026"
    assert st["checked_days_ago"] == 0 and st["stale"] is False


def test_get_rule_hierarchy(loaded):
    rules = loaded.rules
    found = rules.get_rule("702.19b")
    assert found["rule"]["number"] == "702.19b"
    assert found["parent"]["number"] == "702.19" and found["parent"]["text"] == "Trample"
    assert found["chapter"]["number"] == "702"
    assert found["children"] == []

    parent = rules.get_rule("702.19")
    assert [c["number"] for c in parent["children"]] == ["702.19a", "702.19b", "702.19c"]
    assert parent["chapter"]["number"] == "702"

    chapter = rules.get_rule("702")
    assert chapter["rule"]["kind"] == "chapter" and chapter["chapter"] is None
    assert chapter["parent"]["number"] == "7"
    # A chapter lists its main rules only, not every subrule.
    assert [c["number"] for c in chapter["children"]] == ["702.1", "702.2", "702.19"]

    assert rules.get_rule("999.1") is None


@pytest.mark.parametrize("raw, expected", [
    ("702.19b", "702.19b"),
    ("CR 702.19B.", "702.19b"),
    ("rule 702.19", "702.19"),
    ("règle 704", "704"),
    ("  702.19b ", "702.19b"),
])
def test_normalize_number(env, raw, expected):
    assert env.rules.normalize_number(raw) == expected


def test_exists(loaded):
    assert loaded.rules.exists("702.19b")
    assert loaded.rules.exists("[702.19b]".strip("[]"))
    assert not loaded.rules.exists("702.19z")


# --- Search -------------------------------------------------------------

def test_search_ranks_the_relevant_rule_first(loaded):
    res = loaded.rules.search("trample damage assignment")
    assert res and res[0]["number"] in ("702.19b", "702.19a")
    numbers = [r["number"] for r in res]
    assert "702.19b" in numbers
    # Deathtouch's SBA rule mentions damage but not trample: ranked lower.
    assert numbers.index("702.19b") < numbers.index("704.5h")


def test_search_by_rule_number_lists_subrules(loaded):
    res = loaded.rules.search("702.19")
    assert [r["number"] for r in res] == ["702.19", "702.19a", "702.19b", "702.19c"]
    assert loaded.rules.search("702.2c")[0]["text"].startswith("Any nonzero amount")


def test_search_edge_cases(loaded):
    rules = loaded.rules
    assert rules.search("") == []
    assert rules.search("the and of") == []               # stopwords only
    assert rules.search("planeswalker xyzzy", limit=3)   # partial match still answers
    assert len(rules.search("damage", limit=2)) == 2


def test_search_uses_stemming_and_accents(loaded):
    # "blocking creatures" ~ "blocks"/"blocked"; accents are folded.
    res = loaded.rules.search("créatures blocking")
    assert any(r["number"] == "702.19b" for r in res)


def test_lookup_glossary(loaded):
    rules = loaded.rules
    found = rules.lookup_glossary("trample")
    assert [g["term"] for g in found] == ["Trample", "Trample Over Planeswalkers"]
    assert rules.lookup_glossary("DEATHTOUCH")[0]["definition"].startswith("A keyword ability")
    assert rules.lookup_glossary("nothing") == []


# --- Refresh cycle -------------------------------------------------------

class _FakeClient:
    """Stands in for httpx.Client: serves the rules page and the .txt."""
    calls: list[str] = []
    page = PAGE_HTML
    txt = SAMPLE_CR
    fail = False

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        _FakeClient.calls.append(url)
        if _FakeClient.fail:
            raise httpx.ConnectError("offline")
        body = _FakeClient.page if url.endswith("/rules") else _FakeClient.txt
        return httpx.Response(200, content=body.encode("utf-8"),
                              request=httpx.Request("GET", url))


@pytest.fixture()
def fake_http(env, monkeypatch):
    _FakeClient.calls = []
    _FakeClient.page = PAGE_HTML
    _FakeClient.txt = SAMPLE_CR
    _FakeClient.fail = False
    monkeypatch.setattr(env.rules.httpx, "Client", _FakeClient)
    return _FakeClient


def test_find_txt_url(env):
    assert env.rules.find_txt_url(PAGE_HTML) == TXT_URL
    encoded = PAGE_HTML.replace("MagicCompRules 20260819", "MagicCompRules%2020260819")
    assert env.rules.find_txt_url(encoded) == TXT_URL
    assert env.rules.find_txt_url("<html>no rules here</html>") is None


def test_refresh_imports_then_reports_current(env, fake_http):
    rules, db = env.rules, env.db
    first = rules.refresh()
    assert first["status"] == "updated" and first["rules"] == 13
    assert first["effective"] == "2026-08-07"
    assert db.get_meta("rules_source_url") == TXT_URL
    assert rules.get_rule("702.19b") is not None

    # Same link on the page: nothing re-downloaded, but the check is stamped.
    db.set_meta("rules_checked_at", "1")
    second = rules.refresh()
    assert second["status"] == "current"
    assert float(db.get_meta("rules_checked_at")) > 1
    assert fake_http.calls[-1].endswith("/rules")       # no .txt fetch

    # A newer file on the page triggers a re-import.
    fake_http.page = PAGE_HTML.replace("20260819", "20261101")
    fake_http.txt = SAMPLE_CR.replace("August 7, 2026", "November 1, 2026")
    third = rules.refresh()
    assert third["status"] == "updated" and third["effective"] == "2026-11-01"
    assert rules.status()["effective_fr"] == "1er novembre 2026"

    # Forced: re-downloads even when the link is unchanged.
    assert rules.refresh(force=True)["status"] == "updated"


def test_refresh_network_error_keeps_corpus(env, fake_http):
    rules = env.rules
    assert rules.refresh()["status"] == "updated"
    fake_http.fail = True
    res = rules.refresh(force=True)
    assert res["status"] == "error" and "offline" in res["error"]
    assert rules.get_rule("702.19b") is not None      # untouched


def test_refresh_without_txt_link(env, fake_http):
    fake_http.page = "<html>redesigned page</html>"
    res = env.rules.refresh()
    assert res["status"] == "error"
    assert not env.rules.is_loaded()


def test_needs_check_follows_refresh_window(env, fake_http, monkeypatch):
    rules, db = env.rules, env.db
    assert rules.needs_check()                     # nothing loaded yet
    rules.refresh()
    assert not rules.needs_check()
    old = time.time() - (env.rules.settings.rules_refresh_days + 1) * 86400
    db.set_meta("rules_checked_at", str(old))
    assert rules.needs_check()
    assert rules.status()["stale"] is True


def test_import_file(env, tmp_path):
    path = tmp_path / "MagicCompRules 20260819.txt"
    path.write_text(SAMPLE_CR, encoding="utf-8")
    res = env.rules.import_file(str(path))
    assert res["status"] == "updated" and res["rules"] == 13
    assert env.db.get_meta("rules_source_url") == "file:MagicCompRules 20260819.txt"
    assert env.rules.get_rule("704.5h")["rule"]["chapter"] == "State-Based Actions"


def test_background_refresh_is_gated(env, monkeypatch):
    """The scheduler thread must never start with background network off
    (the tests' setting) — otherwise importing app.main would hit wizards.com."""
    started = []
    import threading
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **kw: started.append(kw.get("name")) or types.SimpleNamespace(start=lambda: None))
    env.rules.start_background_refresh()
    assert started == []
