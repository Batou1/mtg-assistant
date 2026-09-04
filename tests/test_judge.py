"""The rules judge (app/judge.py): answer splitting, citation verification,
the agent turn with a mocked Anthropic client, the key-free fallback, the
HTML rendering of citations, and the /rules routes. No network."""
import importlib
import time
import types

import pytest

from tests.test_rules import SAMPLE_CR


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Reload config/db/rules/chat/judge on a throwaway DB with the sample
    corpus loaded and no API key."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MTG_BULK_AUTO_REFRESH", "0")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.rules as rules
    importlib.reload(rules)
    import app.chat as chat
    importlib.reload(chat)
    import app.judge as judge
    importlib.reload(judge)
    db.init_db()
    rules.store(rules.parse(SAMPLE_CR), "test")
    return types.SimpleNamespace(db=db, rules=rules, chat=chat, judge=judge)


def _text_block(text):
    return types.SimpleNamespace(type="text", text=text)


def _tool_block(tool_id, name, payload):
    return types.SimpleNamespace(type="tool_use", id=tool_id, name=name, input=payload)


def _resp(content, stop_reason):
    return types.SimpleNamespace(content=content, stop_reason=stop_reason)


ANSWER = (
    "Réponse : 4 dégâts passent au joueur [702.19b].\n\n"
    "Explication :\n"
    "- Le piétinement (trample) exige d'abord des dégâts létaux au bloqueur [702.19b].\n"
    "- Avec le contact mortel, 1 dégât suffit [702.2c], voir aussi la règle 704.\n"
    "- Une règle inventée : [999.99z]."
)


# --- Answer post-processing ---------------------------------------------

def test_split_answer_on_markers(env):
    answer, explanation = env.judge.split_answer(ANSWER)
    assert answer == "4 dégâts passent au joueur [702.19b]."
    assert explanation.startswith("- Le piétinement")


def test_split_answer_tolerates_markdown_and_missing_markers(env):
    judge = env.judge
    answer, explanation = judge.split_answer(
        "**Réponse courte :** Oui.\n\n**Explication détaillée :** Parce que [702.19b]."
    )
    assert answer == "Oui." and explanation == "Parce que [702.19b]."
    answer, explanation = judge.split_answer("Premier paragraphe.\n\nSuite.\nEncore.")
    assert answer == "Premier paragraphe." and explanation == "Suite.\nEncore."
    assert judge.split_answer("") == ("", "")


def test_citations_are_verified_against_the_corpus(env):
    cites = env.judge.citations(ANSWER)
    by = {c["number"]: c for c in cites}
    assert by["702.19b"]["found"] and by["702.19b"]["title"] == "Keyword Abilities"
    assert by["702.2c"]["found"]
    assert by["704"]["found"]                     # "règle 704" chapter reference
    assert not by["999.99z"]["found"]
    # "rule 702.19b" must not ALSO produce a spurious "702" chapter citation.
    assert "702" not in by
    assert [c["number"] for c in cites] == ["702.19b", "702.2c", "999.99z", "704"]


def test_build_artifact(env):
    art = env.judge.build_artifact(ANSWER)
    assert art["type"] == "ruling"
    assert art["answer"].startswith("4 dégâts")
    assert art["unverified"] == ["999.99z"]
    assert art["rules_effective"] == "2026-08-07"


# --- Agent turn with a mocked LLM ---------------------------------------

def test_turn_searches_rules_then_answers(env, monkeypatch):
    db, chat, judge = env.db, env.chat, env.judge
    pid = db.ensure_default_profile()
    cid = db.create_conversation(pid, kind="rules")
    monkeypatch.setattr(judge.llm, "is_available", lambda: True)
    style_refreshes = []
    monkeypatch.setattr(chat.playerprofile, "schedule_refresh",
                        lambda pid: style_refreshes.append(pid))

    calls = []

    def fake_create(system, messages, tools=None, max_tokens=None, tool_choice=None):
        calls.append({"system": system, "messages": list(messages), "tools": tools})
        if len(calls) == 1:
            return _resp([_tool_block("t1", "search_rules", {"query": "trample lethal damage"}),
                          _tool_block("t2", "get_rule", {"number": "702.2c"}),
                          _tool_block("t3", "lookup_glossary", {"term": "trample"})],
                         "tool_use")
        return _resp([_text_block(ANSWER)], "end_turn")

    monkeypatch.setattr(chat.llm, "create_message", fake_create)
    judge.run_turn(cid, pid, "5/5 trample bloqué par 1/1 deathtouch ?")

    # The judge's own tools were offered, and the corpus version is in the prompt.
    assert {t["name"] for t in calls[0]["tools"]} == {
        "search_rules", "get_rule", "lookup_glossary", "lookup_card"}
    assert "7 août 2026" in calls[0]["system"]
    # Tool results fed back: the search found the trample rule, get_rule the
    # deathtouch one with its context, the glossary the term.
    results = calls[1]["messages"][-1]["content"]
    by_id = {r["tool_use_id"]: r["content"] for r in results}
    assert "[702.19b]" in by_id["t1"]
    assert "[702.2c] Any nonzero amount" in by_id["t2"] and "Règle parente : [702.2] Deathtouch" in by_id["t2"]
    assert by_id["t3"].startswith("Trample : A keyword ability")

    msgs = db.get_messages(cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    art = msgs[1]["artifacts"][0]
    assert art["type"] == "ruling" and art["unverified"] == ["999.99z"]
    assert {c["number"] for c in art["citations"] if c["found"]} == {"702.19b", "702.2c", "704"}
    assert db.get_conversation(cid)["title"].startswith("5/5 trample")
    # Rules questions do not feed the deckbuilding style memory.
    assert style_refreshes == []


def test_tools_report_missing_rules_and_corpus(env):
    judge, db, rules = env.judge, env.db, env.rules
    assert "Aucune règle numérotée" in judge._exec_get_rule({"number": "123.4"}, 1)[0]
    assert "Aucune règle ne correspond" in judge._exec_search_rules({"query": "zzzz"}, 1)[0]
    assert "Aucune entrée" in judge._exec_lookup_glossary({"term": "zzzz"}, 1)[0]
    assert "Aucun nom" in judge._exec_lookup_card({"name": ""}, 1)[0]
    # Limit is clamped and tolerant.
    assert judge._exec_search_rules({"query": "damage", "limit": "oops"}, 1)[0]
    # Empty corpus: every rules tool says so instead of failing.
    db.replace_rules([], [])
    rules.invalidate()
    assert "pas encore chargées" in judge._exec_search_rules({"query": "trample"}, 1)[0]


def test_lookup_card_includes_oracle_and_rulings(env, monkeypatch):
    judge = env.judge
    card = {"name": "Mulldrifter", "id": "abc", "oracle_id": "o1", "type_line": "Creature — Elemental",
            "mana_cost": "{4}{U}", "oracle_text": "Flying\nWhen this creature enters, draw two cards.",
            "power": "2", "toughness": "2", "color_identity": ["U"], "keywords": ["Flying", "Evoke"]}
    monkeypatch.setattr(judge.scryfall, "resolve_cards", lambda names: ({"mulldrifter": card}, []))
    monkeypatch.setattr(judge.scryfall, "rulings",
                        lambda c: [{"published_at": "2008-08-01", "comment": "Evoke is an alternative cost."}])
    text, art = judge._exec_lookup_card({"name": "Mulldrifter"}, 1)
    assert art is None
    assert "draw two cards" in text and "Mots-clés : Flying, Evoke" in text
    assert "(2008-08-01) Evoke is an alternative cost." in text
    monkeypatch.setattr(judge.scryfall, "rulings", lambda c: None)
    assert "injoignable" in judge._exec_lookup_card({"name": "Mulldrifter"}, 1)[0]
    monkeypatch.setattr(judge.scryfall, "resolve_cards", lambda names: ({}, names))
    assert "introuvable" in judge._exec_lookup_card({"name": "Nope"}, 1)[0]


def test_fallback_without_key_lists_matching_rules(env):
    db, judge = env.db, env.judge
    pid = db.ensure_default_profile()
    cid = db.create_conversation(pid, kind="rules")
    judge.run_turn(cid, pid, "trample lethal damage")
    msg = db.get_messages(cid)[1]
    assert "ANTHROPIC_API_KEY" in msg["content"]
    art = msg["artifacts"][0]
    assert art["type"] == "ruling"
    assert any(c["number"] == "702.19b" and c["found"] for c in art["citations"])
    # The quoted rule text itself points at rules outside the mini corpus
    # ("See rules 510.1c–d"): flagged as unverified rather than linked.
    assert art["unverified"] == ["510.1c"]


# --- Rendering -----------------------------------------------------------

def test_render_rules_text_links_only_verified_numbers(env):
    html = str(env.judge.render_rules_text(
        "Voir [702.19b] et [999.99z], la règle 704 et **rule 702.19c** <b>x</b>."))
    assert '<a class="rule-ref" href="/rules/r/702.19b" data-rule="702.19b">702.19b</a>' in html
    assert '<span class="rule-ref missing"' in html and ">999.99z</span>" in html
    assert 'href="/rules/r/704"' in html
    assert "<strong>rule <a" in html
    assert 'href="/rules/r/702"' not in html          # no spurious chapter link
    assert "&lt;b&gt;x&lt;/b&gt;" in html               # escaped, never raw HTML
    assert "[" not in html.split("</a>")[0][-12:]       # brackets dropped around links


def test_render_rules_text_with_stored_citations(env):
    # A stored artifact's citation list decides what links, not the live corpus.
    cites = [{"number": "702.19b", "found": True}, {"number": "702.2c", "found": False}]
    html = str(env.judge.render_rules_text("[702.19b] [702.2c]", cites))
    assert 'data-rule="702.19b"' in html and 'missing" title' in html
    html = str(env.judge.render_rules_text("[702.2c]", {"702.2c"}))
    assert 'data-rule="702.2c"' in html


def test_render_rules_text_paragraphs_and_lists(env):
    html = str(env.judge.render_rules_text(
        "Intro ligne 1\nligne 2\n\n- premier [702.19b]\n- second\n1. troisième\n\nFin"))
    assert html.startswith("<p>Intro ligne 1<br>ligne 2</p><ul><li>premier ")
    assert html.count("<li>") == 3
    assert html.endswith("</ul><p>Fin</p>")
    assert str(env.judge.render_rules_text("")) == ""


# --- Conversation kinds --------------------------------------------------

def test_conversation_kinds_are_listed_separately(env):
    db = env.db
    pid = db.ensure_default_profile()
    deck = db.create_conversation(pid, title="deck")
    rules_conv = db.create_conversation(pid, title="règles", kind="rules")
    assert [c["id"] for c in db.list_conversations(pid)] == [deck]
    assert [c["id"] for c in db.list_conversations(pid, "rules")] == [rules_conv]
    assert db.get_conversation(rules_conv)["kind"] == "rules"
    assert db.get_conversation(deck)["kind"] == "deck"
    with pytest.raises(ValueError):
        db.create_conversation(pid, kind="other")


def test_kind_column_is_migrated_on_old_databases(tmp_path, monkeypatch):
    """A pre-existing conversations table without ``kind`` gains it, and its
    rows count as deckbuilding conversations."""
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO conversations (profile_id, title, created_at, updated_at) VALUES (1, 'vieux', 1, 1);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setenv("MTG_DB_PATH", str(path))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    db.init_db()
    assert db.get_conversation(1)["kind"] == "deck"
    assert [c["title"] for c in db.list_conversations(1)] == ["vieux"]


# --- Routes --------------------------------------------------------------

@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient
    for name in ("app.scryfall", "app.collection", "app.nlquery", "app.main"):
        importlib.reload(importlib.import_module(name))
    import app.main as main
    return TestClient(main.app)


def test_rules_page_and_rule_fragment(client, env):
    page = client.get("/rules")
    assert page.status_code == 200
    assert "Nouvelle question" in page.text
    assert "7 août 2026" in page.text and "rules_conversation_id" in page.headers.get("set-cookie", "")

    frag = client.get("/rules/r/702.19b")
    assert frag.status_code == 200
    assert "702.19b" in frag.text and "Keyword Abilities" in frag.text
    assert "Exemple officiel" in frag.text
    assert 'href="/rules/r/702.19"' in frag.text          # parent crumb
    assert client.get("/rules/r/123.4").status_code == 404


def test_rules_message_runs_a_turn_and_keeps_tabs_apart(client, env):
    db, chat = env.db, env.chat
    page = client.get("/rules")
    conv_id = int(page.cookies.get("rules_conversation_id"))
    assert db.get_conversation(conv_id)["kind"] == "rules"

    resp = client.post("/rules/message", data={"message": "trample damage", "conversation_id": str(conv_id)},
                       headers={"X-Requested-With": "fetch"})
    assert resp.status_code == 200 and resp.json()["conversation_id"] == conv_id
    for _ in range(50):                      # the key-free turn runs in a thread
        if not chat.is_pending(conv_id):
            break
        time.sleep(0.05)
    assert client.get(f"/rules/status?conversation_id={conv_id}").json() == {"pending": False}

    page = client.get("/rules")
    assert 'class="ruling-answer"' in page.text
    assert 'data-rule="702.19b"' in page.text
    # The deckbuilding chat neither lists nor adopts the rules conversation.
    pid = db.ensure_default_profile()
    assert db.list_conversations(pid) == []
    chat_page = client.get("/chat", cookies={"conversation_id": str(conv_id)})
    assert "trample damage" not in chat_page.text

    assert client.post(f"/rules/{conv_id}/delete", follow_redirects=False).status_code == 303
    assert db.get_conversation(conv_id) is None
