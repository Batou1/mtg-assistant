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
