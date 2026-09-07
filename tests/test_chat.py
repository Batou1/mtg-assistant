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


def test_create_pool_conversation_seeds_deck(env, monkeypatch):
    db, chat = env.db, env.chat
    import app.scryfall as scryfall
    monkeypatch.setattr(scryfall, "resolve_cards", _lim_resolve)
    monkeypatch.setattr(chat.llm, "is_available", lambda: False)  # heuristic path

    pid = db.ensure_default_profile()
    pool_items = [("Goblin Guide", 1), ("Lightning Bolt", 2), ("Shock", 1)]
    cid = chat.create_pool_conversation(pid, pool_items, "limited", {"colors": ["R"]})

    msgs = db.get_messages(cid)
    assert msgs[0]["role"] == "user"
    art = msgs[-1]["artifacts"][0]
    assert art["type"] == "pool_deck"
    assert art["data"]["counts"]["total"] == 40
    # The pool is carried in the artifact so chat can rebuild from it.
    assert any(p["name"] == "Lightning Bolt" for p in art["data"]["pool"])


def test_build_pool_deck_tool_rebuilds_from_stored_pool(env, monkeypatch):
    db, chat = env.db, env.chat
    import app.scryfall as scryfall
    monkeypatch.setattr(scryfall, "resolve_cards", _lim_resolve)
    monkeypatch.setattr(chat.llm, "is_available", lambda: True)

    pid = db.ensure_default_profile()
    # Seed a conversation that already has a pool deck (heuristic-built).
    monkeypatch.setattr(chat.llm, "is_available", lambda: False)
    cid = chat.create_pool_conversation(
        pid, [("Goblin Guide", 1), ("Lightning Bolt", 2), ("Shock", 1)], "limited",
        {"colors": ["R"]}
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
    assert assistant["artifacts"][-1]["type"] == "pool_deck"
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


# --- Per-card price cap (max_card_price_eur) -----------------------------

def test_intent_from_carries_max_card_price(env):
    chat = env.chat
    parsed = chat._intent_from(
        {"budget_eur": 30, "max_card_price_eur": 5}, "commander"
    )
    assert parsed["budget_eur"] == 30.0
    assert parsed["max_card_price_eur"] == 5.0


def test_generate_decklist_tool_passes_max_card_price(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()

    captured = {}

    def fake_generate(commander, budget, theme, profile_id, max_card_price=None,
                      fmt="commander", include_cards=None, exclude_cards=None):
        captured["budget"] = budget
        captured["max_card_price"] = max_card_price
        captured["fmt"] = fmt
        return {
            "counts": {"total": 100, "owned": 0, "to_buy": 99},
            "buy_total_eur": 25.0, "max_card_price_eur": max_card_price,
        }, {}

    monkeypatch.setattr(chat.deckgen, "generate_full_deck", fake_generate)

    text, artifact = chat._exec_generate_decklist(
        {"commander": "Krenko, Mob Boss", "budget_eur": 30, "max_card_price_eur": 5},
        pid,
    )
    assert captured["budget"] == 30
    assert captured["max_card_price"] == 5
    assert captured["fmt"] == "commander"
    assert "max 5€/carte" in text
    assert artifact["type"] == "decklist"


def test_generate_decklist_tool_passes_requested_format(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()

    captured = {}

    def fake_generate(commander, budget, theme, profile_id, max_card_price=None,
                      fmt="commander", include_cards=None, exclude_cards=None):
        captured["fmt"] = fmt
        return {"counts": {"total": 100, "owned": 0, "to_buy": 99}, "buy_total_eur": 0}, {}

    monkeypatch.setattr(chat.deckgen, "generate_full_deck", fake_generate)

    chat._exec_generate_decklist(
        {"commander": "Tinybones, Trinket Thief", "format": "paupercommander"}, pid,
    )
    assert captured["fmt"] == "paupercommander"

    # An unrecognized format falls back to plain "commander" rather than
    # blowing up deckgen's own edhrec.fetch_commander lookup.
    chat._exec_generate_decklist(
        {"commander": "Krenko, Mob Boss", "format": "modern"}, pid,
    )
    assert captured["fmt"] == "commander"


def test_generate_decklist_tool_forwards_includes_and_reports_them(env, monkeypatch):
    """include/exclude_cards reach deckgen, and the tool result explicitly
    lists integrated vs rejected cards so the model can't misreport them."""
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()

    captured = {}

    def fake_generate(commander, budget, theme, profile_id, max_card_price=None,
                      fmt="commander", include_cards=None, exclude_cards=None):
        captured["include"] = include_cards
        captured["exclude"] = exclude_cards
        return {
            "counts": {"total": 100, "owned": 0, "to_buy": 99},
            "buy_total_eur": 12.0,
            "forced_cards": ["Lightning Bolt"],
            "rejected_includes": [
                {"name": "Counterspell",
                 "reason": "hors identité couleur du commandant"},
            ],
            "excluded_cards": ["Sol Ring"],
        }, {}

    monkeypatch.setattr(chat.deckgen, "generate_full_deck", fake_generate)

    text, artifact = chat._exec_generate_decklist(
        {"commander": "Krenko, Mob Boss",
         "include_cards": ["Lightning Bolt", "Counterspell"],
         "exclude_cards": ["Sol Ring"]},
        pid,
    )
    assert captured["include"] == ["Lightning Bolt", "Counterspell"]
    assert captured["exclude"] == ["Sol Ring"]
    assert "INTÉGRÉES : Lightning Bolt" in text
    assert "REFUSÉE : Counterspell (hors identité couleur du commandant)" in text
    assert "Cartes exclues : Sol Ring" in text
    # The snapshot rebuilt for the next turn carries the forced cards, so a
    # later regeneration can re-pass them via include_cards.
    snapshot = chat._snapshot_decklist(artifact)
    assert "Cartes imposées par le joueur" in snapshot
    assert "Lightning Bolt" in snapshot


def test_find_commanders_tool_builds_finder_artifact(env, monkeypatch):
    """The theme-first search tool works without any collection and flags its
    artifact as `finder` so the UI shows the validation (Retenir) flow."""
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()  # no collection imported

    captured = {}

    def fake_find(intent, profile_id):
        captured["format"] = intent.get("format")
        return {
            "results": [{
                "name": "Krenko, Mob Boss", "image": None, "color_identity": ["R"],
                "num_decks": 12000, "below_threshold": False, "owned": False,
                "price_eur": 0.5, "link_count": 0, "linked_owned_cards": [],
                "owned_count": 1, "total_recommended": 10, "pct": 10.0,
                "owned_cards": ["Goblin Recruiter"], "missing_cards": ["Goblin Chieftain"],
                "total_cost_eur": 2.5,
                "buylist": {"to_buy": [], "total_eur": 2.0, "bought_count": 1,
                            "budget_eur": None, "max_card_price_eur": None},
            }],
            "notices": [], "candidate_count": 3,
        }

    monkeypatch.setattr(chat.analysis, "find_commanders", fake_find)

    text, artifact = chat._exec_find_commanders(
        {"format": "commander", "colors": ["R"], "keywords": ["goblins"]}, pid
    )
    assert captured["format"] == "commander"
    assert artifact["type"] == "commanders"
    assert artifact["finder"] is True
    assert "Krenko" in text
    assert "retient" in text  # the model is told to ask for validation

    # Unknown format value falls back to plain "commander".
    chat._exec_find_commanders({"format": "bogus"}, pid)
    assert captured["format"] == "commander"


def test_find_commanders_tool_respects_unowned_only(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()

    base = {
        "image": None, "color_identity": ["R"], "num_decks": 100,
        "below_threshold": False, "price_eur": None, "link_count": 0,
        "linked_owned_cards": [], "owned_count": 0, "total_recommended": 10,
        "pct": 0.0, "owned_cards": [], "missing_cards": [], "total_cost_eur": 0.0,
        "buylist": {"to_buy": [], "total_eur": 0, "bought_count": 0,
                    "budget_eur": None, "max_card_price_eur": None},
    }
    monkeypatch.setattr(chat.analysis, "find_commanders", lambda intent, pid_: {
        "results": [
            {**base, "name": "Owned One", "owned": True},
            {**base, "name": "New One", "owned": False, "price_eur": 1.0},
        ],
        "notices": [], "candidate_count": 2,
    })

    _text, artifact = chat._exec_find_commanders({"unowned_only": True}, pid)
    assert [r["name"] for r in artifact["results"]] == ["New One"]


def test_suggest_commanders_tool_passes_requested_format(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Krenko, Mob Boss")])

    captured = {}

    def fake_analyze(intent, profile_id):
        captured["format"] = intent.get("format")
        return {"results": [], "notices": [], "candidate_count": 0}

    monkeypatch.setattr(chat.analysis, "analyze", fake_analyze)

    chat._exec_suggest_commanders({"format": "duelcommander", "colors": ["R"]}, pid)
    assert captured["format"] == "duelcommander"

    # Unknown format value falls back to plain "commander".
    chat._exec_suggest_commanders({"format": "bogus"}, pid)
    assert captured["format"] == "commander"


# --- Reflection / discussion mode ----------------------------------------
# The chat must be able to DISCUSS an idea (viability, synergies) without
# generating anything, grounded by the consultation tools.

_RADAGAST = {
    "name": "Radagast of Rhosgobel",
    "mana_cost": "{3}{G}",
    "type_line": "Legendary Creature — Avatar Wizard",
    "power": "3", "toughness": "3",
    "oracle_text": (
        "Vigilance\nAt the beginning of combat on your turn, look at the top X "
        "cards of your library, where X is Radagast's power."
    ),
    "color_identity": ["G"],
    "legalities": {"commander": "legal", "duel": "legal"},
}


def _radagast_resolve(names, client=None):
    return {n.lower(): dict(_RADAGAST) for n in names}, []


_RADAGAST_PAGE = {
    "container": {"json_dict": {
        "card": {"num_decks": 431},
        "cardlists": [
            {"tag": "highsynergycards", "header": "High Synergy Cards",
             "cardviews": [{"name": "Beorn the Fierce"}, {"name": "Elvish Piper"}]},
        ],
    }},
    "panels": {"taglinks": [{"slug": "creatures"}, {"value": "flash"}]},
}


def test_reflection_prompt_and_tool_registered(env):
    chat = env.chat
    assert "PARTENAIRE DE RÉFLEXION" in chat.SYSTEM_PROMPT
    # Every declared tool has an executor and vice versa.
    assert set(chat._EXECUTORS) == {t["name"] for t in chat.TOOLS}
    assert "get_commander_overview" in chat._EXECUTORS


def test_commander_overview_tool_grounds_discussion(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    db.replace_collection(pid, [_row("Beorn the Fierce")])

    monkeypatch.setattr(chat.scryfall, "resolve_cards", _radagast_resolve)
    monkeypatch.setattr(chat.edhrec, "fetch_commander",
                        lambda name, client=None: _RADAGAST_PAGE)

    text, artifact = chat._exec_commander_overview(
        {"name": "Radagast of Rhosgobel"}, pid
    )
    # Consultation only: no artifact, so the UI never shows a generation result.
    assert artifact is None
    # Card facts (exact text) ground the discussion.
    assert "Vigilance" in text
    assert "Legendary Creature" in text
    # Commander eligibility per format.
    assert "commander" in text and "duelcommander" in text
    # EDHREC popularity, themes and top cards.
    assert "431 decks" in text
    assert "creatures" in text and "flash" in text
    # Owned top cards are flagged, unowned ones are not.
    assert "Beorn the Fierce (possédée)" in text
    assert "Elvish Piper (possédée)" not in text
    assert "Elvish Piper" in text


def test_commander_overview_handles_edhrec_sentinels(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    monkeypatch.setattr(chat.scryfall, "resolve_cards", _radagast_resolve)

    # _error: never cached upstream, the overview says "retry later".
    monkeypatch.setattr(chat.edhrec, "fetch_commander",
                        lambda name, client=None: {"_error": True})
    text, _ = chat._exec_commander_overview({"name": "Radagast of Rhosgobel"}, pid)
    assert "indisponibles" in text

    # _not_found: an unplayed commander is flagged as original, not broken.
    monkeypatch.setattr(chat.edhrec, "fetch_commander",
                        lambda name, client=None: {"_not_found": True})
    text, _ = chat._exec_commander_overview({"name": "Radagast of Rhosgobel"}, pid)
    assert "Aucune page EDHREC" in text


def test_commander_overview_flags_non_commander(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    vanilla = {
        "name": "Grizzly Bears", "mana_cost": "{1}{G}",
        "type_line": "Creature — Bear", "power": "2", "toughness": "2",
        "oracle_text": "", "color_identity": ["G"],
        "legalities": {"commander": "legal"},
    }
    monkeypatch.setattr(
        chat.scryfall, "resolve_cards",
        lambda names, client=None: ({n.lower(): vanilla for n in names}, []),
    )
    monkeypatch.setattr(chat.edhrec, "fetch_commander",
                        lambda name, client=None: {"_not_found": True})
    text, _ = chat._exec_commander_overview({"name": "Grizzly Bears"}, pid)
    assert "ne peut PAS être un commandant" in text


def test_lookup_card_includes_card_text(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    monkeypatch.setattr(chat.scryfall, "resolve_cards", _radagast_resolve)
    monkeypatch.setattr(chat.prices, "buy_price_eur", lambda card: 0.5)

    text, artifact = chat._exec_lookup_card({"name": "Radagast of Rhosgobel"}, pid)
    assert artifact is None
    assert "Vigilance" in text          # oracle text
    assert "Legendary Creature" in text  # type line
    assert "0.5" in text                 # price
    assert "commander" in text           # legality


def test_reflection_turn_discusses_without_generating(env, monkeypatch):
    """An open question triggers consultation tools + a substantive answer,
    never a generation tool."""
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    cid = db.create_conversation(pid)

    monkeypatch.setattr(chat.llm, "is_available", lambda: True)
    monkeypatch.setattr(chat.scryfall, "resolve_cards", _radagast_resolve)
    monkeypatch.setattr(chat.edhrec, "fetch_commander",
                        lambda name, client=None: _RADAGAST_PAGE)

    def boom(*a, **k):
        raise AssertionError("no generation tool may run for an open question")
    monkeypatch.setattr(chat.deckgen, "generate_full_deck", boom)
    monkeypatch.setattr(chat.analysis, "analyze", boom)
    monkeypatch.setattr(chat.analysis, "find_commanders", boom)

    responses = [
        _resp([_tool_block("t1", "get_commander_overview",
                           {"name": "Radagast of Rhosgobel"})], "tool_use"),
        _resp([_text_block(
            "Oui, c'est jouable : Radagast triche des créatures en jeu et le "
            "flash permet de réagir. Veux-tu creuser la base de créatures ?"
        )], "end_turn"),
    ]
    calls = {"n": 0}

    def fake_create(system, messages, tools=None, max_tokens=None, tool_choice=None):
        r = responses[calls["n"]]
        calls["n"] += 1
        return r

    monkeypatch.setattr(chat.llm, "create_message", fake_create)

    chat.run_turn(cid, pid, "Est-ce que ça a du sens de faire un deck autour "
                            "de Radagast of Rhosgobel et Beorn the Fierce ?")

    assistant = db.get_messages(cid)[-1]
    assert "jouable" in assistant["content"]
    assert not assistant.get("artifacts")  # discussion leaves no artifact
    assert calls["n"] == 2


# --- Agent-loop robustness (the "D'accord." bug) -------------------------

def test_thinking_truncation_retries_with_bigger_budget(env, monkeypatch):
    """A response truncated inside thinking (stop_reason=max_tokens, no text)
    is retried once with the large token budget instead of yielding an empty
    reply."""
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    cid = db.create_conversation(pid)
    monkeypatch.setattr(chat.llm, "is_available", lambda: True)

    budgets = []

    def fake_create(system, messages, tools=None, max_tokens=None, tool_choice=None):
        budgets.append(max_tokens)
        if len(budgets) == 1:
            return _resp([], "max_tokens")  # all tokens went to thinking
        return _resp([_text_block("Réponse complète après réflexion.")], "end_turn")

    monkeypatch.setattr(chat.llm, "create_message", fake_create)

    chat.run_turn(cid, pid, "question difficile qui fait beaucoup réfléchir")
    assert budgets == [None, chat.settings.anthropic_deck_max_tokens]
    assert "Réponse complète" in db.get_messages(cid)[-1]["content"]


def test_exhausted_tool_iterations_force_final_text(env, monkeypatch):
    """When the tool budget runs out mid-flight, one last text-only call
    (tool_choice none) closes the turn instead of ending in silence."""
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    cid = db.create_conversation(pid)
    monkeypatch.setattr(chat.llm, "is_available", lambda: True)

    calls = {"n": 0, "final": None}

    def fake_create(system, messages, tools=None, max_tokens=None, tool_choice=None):
        calls["n"] += 1
        if tool_choice == {"type": "none"}:
            calls["final"] = tool_choice
            return _resp([_text_block("Voilà où j'en suis pour l'instant.")], "end_turn")
        return _resp(
            [_tool_block(f"t{calls['n']}", "get_collection_summary", {})], "tool_use"
        )

    monkeypatch.setattr(chat.llm, "create_message", fake_create)

    chat.run_turn(cid, pid, "vas-y")
    assert calls["final"] == {"type": "none"}
    assert calls["n"] == chat.settings.chat_max_tool_iterations + 1
    assert "Voilà où j'en suis" in db.get_messages(cid)[-1]["content"]


def test_empty_reply_fallback_is_explicit_not_daccord(env, monkeypatch):
    """Even if the model persistently returns no text, the player gets an
    honest message, never a bare "D'accord."."""
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    cid = db.create_conversation(pid)
    monkeypatch.setattr(chat.llm, "is_available", lambda: True)
    monkeypatch.setattr(
        chat.llm, "create_message",
        lambda system, messages, tools=None, max_tokens=None, tool_choice=None:
            _resp([], "end_turn"),
    )

    chat.run_turn(cid, pid, "Est-ce que ça a du sens ?")
    content = db.get_messages(cid)[-1]["content"]
    assert "D'accord" not in content
    assert "Reformule" in content


def test_serialize_content_preserves_thinking_blocks(env):
    """Thinking blocks must survive serialization: adaptive thinking rejects a
    resent tool round whose thinking was stripped."""
    chat = env.chat
    blocks = [
        types.SimpleNamespace(type="thinking", thinking="hmm", signature="sig"),
        types.SimpleNamespace(type="redacted_thinking", data="blob"),
        types.SimpleNamespace(type="text", text="ok"),
        types.SimpleNamespace(type="tool_use", id="t1", name="lookup_card",
                              input={"name": "Sol Ring"}),
    ]
    out = chat._serialize_content(blocks)
    assert out[0] == {"type": "thinking", "thinking": "hmm", "signature": "sig"}
    assert out[1] == {"type": "redacted_thinking", "data": "blob"}
    assert out[2]["type"] == "text" and out[3]["type"] == "tool_use"


def test_research_archetype_tool_passes_owned_only(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    captured = {}

    def fake_analyze(intent, profile_id):
        captured.update(intent)
        return {
            "format": "pauper", "owned_only": True, "owned_pool_empty": False,
            "owned_pool_size": 42, "not_owned": ["Lightning Bolt"],
            "archetype": {"name": "Goblins", "colors": ["R"], "strategy": ""},
            "deck": {"counts": {"total": 60, "lands": 24, "owned": 60,
                                "to_buy": 0, "spells": 36}},
            "buylist": {"to_buy": [], "total_eur": 0, "bought_count": 0},
            "deck_cost_eur": 0.0, "budget_eur": 0.0, "budget_exceeded": False,
            "llm_unavailable": False,
        }

    monkeypatch.setattr(chat.formats60, "analyze", fake_analyze)
    text, artifact = chat._exec_research_archetype(
        {"format": "pauper", "owned_only": True, "budget_eur": 0}, pid)
    assert captured["owned_only"] is True
    assert captured["budget_eur"] == 0.0
    assert artifact["type"] == "archetype"
    assert "100 % COLLECTION" in text
    assert "Lightning Bolt" in text
    assert "ATTENTION" not in text


def test_research_archetype_tool_reports_an_empty_owned_pool(env, monkeypatch):
    db, chat = env.db, env.chat
    pid = db.ensure_default_profile()
    monkeypatch.setattr(chat.formats60, "analyze", lambda intent, pid_: {
        "format": "pauper", "owned_only": True, "owned_pool_empty": True,
        "owned_pool_size": 0, "owned_pool_unresolved": 0,
        "archetype": {"name": "Deck 100 % collection", "colors": ["R"], "strategy": ""},
        "llm_unavailable": False,
    })
    text, artifact = chat._exec_research_archetype(
        {"format": "pauper", "colors": ["R"], "budget_eur": 0}, pid)
    assert "impossible" in text
    assert artifact["data"]["owned_pool_empty"] is True
