"""Tests for the iterative chat: DB round-trip, the agent tool loop (with a
mocked Anthropic client), and the key-free fallback."""
import importlib
import types

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Reload config/db/chat against a throwaway SQLite file."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.chat as chat
    importlib.reload(chat)
    db.init_db()
    return types.SimpleNamespace(db=db, chat=chat)


def _row(name):
    key = name.split("//")[0].strip().lower()
    return {"scryfall_id": "", "name_key": key, "raw_name": name,
            "set_code": "ABC", "foil": 0, "condition": "nm", "quantity": 1}


# --- Fake Anthropic blocks/responses ------------------------------------

def _text_block(text):
    return types.SimpleNamespace(type="text", text=text)


def _tool_block(tool_id, name, payload):
    return types.SimpleNamespace(type="tool_use", id=tool_id, name=name, input=payload)


def _resp(content, stop_reason):
    return types.SimpleNamespace(content=content, stop_reason=stop_reason)


# --- DB round-trip ------------------------------------------------------

def test_conversation_message_roundtrip(env):
    db = env.db
    pid = db.ensure_default_profile()
    cid = db.create_conversation(pid)
    assert db.list_conversations(pid)[0]["id"] == cid

    db.add_message(cid, "user", "salut")
    db.add_message(cid, "assistant", "bonjour", artifacts=[{"type": "commanders"}])
    msgs = db.get_messages(cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["artifacts"] == [{"type": "commanders"}]

    db.touch_conversation(cid, title="salut")
    assert db.get_conversation(cid)["title"] == "salut"

    db.delete_conversation(cid)
    assert db.get_conversation(cid) is None
    assert db.get_messages(cid) == []


def test_delete_profile_cascades_conversations(env):
    db = env.db
    pid = db.create_profile("Alice")
    cid = db.create_conversation(pid)
    db.add_message(cid, "user", "hi")
    db.delete_profile(pid)
    assert db.get_conversation(cid) is None
    assert db.get_messages(cid) == []


# --- Agent loop with a mocked LLM ---------------------------------------

def test_agent_loop_executes_tool_and_stores_artifact(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Krenko, Mob Boss")])
    cid = db.create_conversation(pid)

    # Mocked analysis result feeding the suggest_commanders tool.
    fake = {
        "results": [{
            "name": "Krenko, Mob Boss", "image": None, "color_identity": ["R"],
            "num_decks": 12000, "owned_count": 3, "total_recommended": 10, "pct": 30.0,
            "owned_cards": ["Sol Ring"], "missing_cards": ["Goblin Chieftain"],
            "buylist": {"to_buy": [], "total_eur": 0, "bought_count": 0, "budget_eur": None},
        }],
        "notices": [], "candidate_count": 1,
    }
    monkeypatch.setattr(chat.analysis, "analyze", lambda intent, profile_id: fake)
    monkeypatch.setattr(chat.llm, "is_available", lambda: True)

    responses = [
        _resp([_tool_block("t1", "suggest_commanders", {"colors": ["R"]})], "tool_use"),
        _resp([_text_block("Voici Krenko, un bon choix mono-rouge.")], "end_turn"),
    ]
    calls = {"n": 0}

    def fake_create(system, messages, tools=None, max_tokens=None):
        r = responses[calls["n"]]
        calls["n"] += 1
        return r

    monkeypatch.setattr(chat.llm, "create_message", fake_create)

    chat.run_turn(cid, pid, "un deck commander mono rouge gobelins")

    msgs = db.get_messages(cid)
    assert msgs[0]["role"] == "user"
    assistant = msgs[-1]
    assert "Krenko" in assistant["content"]
    assert assistant["artifacts"][0]["type"] == "commanders"
    assert assistant["artifacts"][0]["results"][0]["name"] == "Krenko, Mob Boss"
    # Both API rounds were used (tool round + final answer).
    assert calls["n"] == 2


def test_agent_loop_stops_when_llm_unavailable_midflight(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    cid = db.create_conversation(pid)
    monkeypatch.setattr(chat.llm, "is_available", lambda: True)
    monkeypatch.setattr(chat.llm, "create_message", lambda *a, **k: None)

    chat.run_turn(cid, pid, "salut")
    assistant = db.get_messages(cid)[-1]
    assert assistant["role"] == "assistant"
    assert "indisponible" in assistant["content"].lower()


# --- Context snapshot: answer follow-ups without regenerating -----------

def test_context_snapshot_replays_deck_without_regenerating(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    cid = db.create_conversation(pid)

    # A deck was generated on an earlier turn (artifact persisted).
    deck_artifact = {
        "type": "decklist",
        "commander": "Krenko, Mob Boss",
        "deck": {
            "counts": {"total": 100, "owned": 40, "to_buy": 60},
            "buy_total_eur": 50.0,
            "gameplan": "Spam des gobelins puis Krenko pour exploser le plateau.",
            "groups": [
                {"label": "Créatures", "cards": [
                    {"name": "Goblin Chieftain", "owned": False, "price_eur": 3.0, "qty": 1},
                    {"name": "Krenko's Command", "owned": True, "qty": 1},
                ]},
            ],
        },
    }
    db.add_message(cid, "user", "génère le deck Krenko")
    db.add_message(cid, "assistant", "Voici ton deck.", artifacts=[deck_artifact])

    monkeypatch.setattr(chat.llm, "is_available", lambda: True)

    # Regeneration must NOT happen for a follow-up question.
    def boom(*a, **k):
        raise AssertionError("generate_full_deck should not be called for a follow-up")
    monkeypatch.setattr(chat.deckgen, "generate_full_deck", boom)

    captured = {}

    def fake_create(system, messages, tools=None, max_tokens=None):
        captured["system"] = system
        return _resp(
            [_text_block("La courbe est basse : surtout des gobelins à 1-2 manas.")],
            "end_turn",
        )

    monkeypatch.setattr(chat.llm, "create_message", fake_create)

    chat.run_turn(cid, pid, "quelle est la courbe de mana de ce deck ?")

    # The generated deck (cards included) is replayed in the system prompt.
    assert "CONTEXTE ACTUEL" in captured["system"]
    assert "Goblin Chieftain" in captured["system"]
    assert "Krenko, Mob Boss" in captured["system"]
    # The model answered from context; the reply is stored.
    assert "courbe" in db.get_messages(cid)[-1]["content"].lower()


def test_no_snapshot_when_nothing_generated_yet(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    cid = db.create_conversation(pid)
    monkeypatch.setattr(chat.llm, "is_available", lambda: True)

    captured = {}

    def fake_create(system, messages, tools=None, max_tokens=None):
        captured["system"] = system
        return _resp([_text_block("Quel format et quelles couleurs vises-tu ?")], "end_turn")

    monkeypatch.setattr(chat.llm, "create_message", fake_create)

    chat.run_turn(cid, pid, "je veux un deck")

    # No prior artifacts: the system prompt is the plain base prompt (no
    # appended snapshot), and the assistant can ask a clarifying question
    # instead of generating.
    assert captured["system"] == chat.SYSTEM_PROMPT


# --- Limited (pool-built deck) ------------------------------------------

def _limited_card(name, type_line="Creature — Goblin", colors=("R",)):
    return {
        "name": name, "type_line": type_line, "color_identity": list(colors),
        "colors": list(colors), "cmc": 1.0, "image_uris": {"normal": "x"},
        "oracle_text": "t",
    }


_LIM_POOL = {
    "goblin guide": _limited_card("Goblin Guide"),
    "lightning bolt": _limited_card("Lightning Bolt", "Instant"),
    "shock": _limited_card("Shock", "Instant"),
}


def _lim_resolve(names, client=None):
    found = {n.lower(): _LIM_POOL[n.lower()] for n in names if n.lower() in _LIM_POOL}
    return found, [n for n in names if n.lower() not in _LIM_POOL]


def test_create_limited_conversation_seeds_deck(env, monkeypatch):
    db, chat = env.db, env.chat
    import app.scryfall as scryfall
    monkeypatch.setattr(scryfall, "resolve_cards", _lim_resolve)
    monkeypatch.setattr(chat.llm, "is_available", lambda: False)  # heuristic path

    pid = db.ensure_default_profile()
    pool_items = [("Goblin Guide", 1), ("Lightning Bolt", 2), ("Shock", 1)]
    cid = chat.create_limited_conversation(pid, pool_items, {"colors": ["R"]})

    msgs = db.get_messages(cid)
    assert msgs[0]["role"] == "user"
    art = msgs[-1]["artifacts"][0]
    assert art["type"] == "limited_deck"
    assert art["data"]["counts"]["total"] == 40
    # The pool is carried in the artifact so chat can rebuild from it.
    assert any(p["name"] == "Lightning Bolt" for p in art["data"]["pool"])


def test_build_pool_deck_tool_rebuilds_from_stored_pool(env, monkeypatch):
    db, chat = env.db, env.chat
    import app.scryfall as scryfall
    monkeypatch.setattr(scryfall, "resolve_cards", _lim_resolve)
    monkeypatch.setattr(chat.llm, "is_available", lambda: True)

    pid = db.ensure_default_profile()
    # Seed a conversation that already has a Limited deck (heuristic-built).
    monkeypatch.setattr(chat.llm, "is_available", lambda: False)
    cid = chat.create_limited_conversation(
        pid, [("Goblin Guide", 1), ("Lightning Bolt", 2), ("Shock", 1)], {"colors": ["R"]}
    )
    monkeypatch.setattr(chat.llm, "is_available", lambda: True)

    # Now the player asks to rebuild; the model calls build_pool_deck (no pool arg —
    # it must come from the stored artifact).
    responses = [
        _resp([_tool_block("t1", "build_pool_deck", {"colors": ["R"]})], "tool_use"),
        _resp([_text_block("Voici un nouveau deck mono-rouge depuis ton pool.")], "end_turn"),
    ]
    calls = {"n": 0}

    def fake_create(system, messages, tools=None, max_tokens=None):
        r = responses[calls["n"]]
        calls["n"] += 1
        return r

    monkeypatch.setattr(chat.llm, "create_message", fake_create)
    # The rebuild itself uses the heuristic (pool_deck not mocked, key "available").
    monkeypatch.setattr(chat.llm, "pool_deck", lambda spec, intent, lines: None)

    chat.run_turn(cid, pid, "refais le deck en plus agressif")

    assistant = db.get_messages(cid)[-1]
    assert assistant["artifacts"][-1]["type"] == "limited_deck"
    assert calls["n"] == 2


# --- Key-free fallback --------------------------------------------------

def test_fallback_without_key_uses_oneshot_pipeline(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Krenko, Mob Boss")])
    cid = db.create_conversation(pid)

    monkeypatch.setattr(chat.llm, "is_available", lambda: False)
    monkeypatch.setattr(chat.intent, "parse_intent",
                        lambda text: {"format": "commander", "colors": ["R"],
                                      "max_colors": None, "theme": "", "keywords": [],
                                      "budget_eur": None, "source": "heuristic"})
    fake = {"results": [{
        "name": "Krenko, Mob Boss", "image": None, "color_identity": ["R"],
        "num_decks": 12000, "owned_count": 1, "total_recommended": 10, "pct": 10.0,
        "owned_cards": [], "missing_cards": [],
        "buylist": {"to_buy": [], "total_eur": 0, "bought_count": 0, "budget_eur": None},
    }], "notices": [], "candidate_count": 1}
    monkeypatch.setattr(chat.analysis, "analyze", lambda intent, profile_id: fake)

    chat.run_turn(cid, pid, "commander rouge")
    assistant = db.get_messages(cid)[-1]
    assert "ANTHROPIC_API_KEY" in assistant["content"]
    assert assistant["artifacts"][0]["type"] == "commanders"
