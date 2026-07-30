"""French question -> Scryfall query (app/nlquery.py). No network: the LLM
call is monkeypatched, the DB (translation cache) points at a temp file."""
import importlib

import pytest


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """(nlquery, db) with a throwaway SQLite file and no API key by default."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.nlquery as nlquery
    importlib.reload(nlquery)
    db.init_db()
    return nlquery, db


def _answers(nlquery, monkeypatch, *replies):
    """Make the LLM available and return ``replies`` in order; record calls."""
    calls = []

    def fake(question, previous=None, parse_error=None):
        calls.append((question, previous, parse_error))
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(nlquery.llm, "is_available", lambda: True)
    monkeypatch.setattr(nlquery.llm, "collection_query", fake)
    return calls


# --- Heuristic (no API key) ----------------------------------------------

@pytest.mark.parametrize("question, expected", [
    ("mes créatures vertes", "id<=g t:creature"),
    ("les artefacts à moins de 5 euros", "t:artifact eur<=5"),
    ("créatures avec le vol", "t:creature kw:flying"),
    ("cartes rouges de moins de 3 manas", "id<=r mv<=3"),
    ("mes rares en bleu", "id<=u r:rare"),
    ("mes commandants Simic", "id<=gu is:commander"),
    ("les cartes déjà en deck", "is:indeck"),
    ("mes terrains légaux en modern", "t:land f:modern"),
    ("les mythiques à plus de 20 €", "r:mythic eur>=20"),
])
def test_heuristic_translates_common_asks(fresh, question, expected):
    nlquery, _db = fresh
    assert nlquery._heuristic(question) == expected


def test_heuristic_output_is_always_a_valid_query(fresh):
    """Whatever the heuristic emits must compile — it is fed to the engine."""
    nlquery, _db = fresh
    from app import scryquery
    for question in ("mes créatures vertes à moins de 3 manas",
                     "artefacts rares disponibles", "cartes izzet avec piétinement"):
        scryquery.parse(nlquery._heuristic(question))


def test_heuristic_says_nothing_when_it_understands_nothing(fresh):
    nlquery, _db = fresh
    assert nlquery._heuristic("quelque chose de sympa pour vendredi soir") == ""


def test_without_api_key_translate_falls_back_and_warns(fresh):
    nlquery, _db = fresh
    out = nlquery.translate("mes créatures vertes")
    assert out["query"] == "id<=g t:creature"
    assert out["source"] == "heuristic"
    assert "clé API" in out["error"]


def test_without_api_key_and_nothing_recognised_returns_a_message(fresh):
    nlquery, _db = fresh
    out = nlquery.translate("un truc rigolo")
    assert out["query"] == "" and out["source"] == ""
    assert out["error"]


def test_empty_question_is_a_no_op(fresh):
    nlquery, _db = fresh
    assert nlquery.translate("   ") == {"query": "", "source": "", "error": None}


# --- LLM path ------------------------------------------------------------

def test_llm_answer_is_used_and_cached(fresh, monkeypatch):
    nlquery, db = fresh
    calls = _answers(nlquery, monkeypatch, "t:creature id<=g mv<=3")

    out = nlquery.translate("mes créatures vertes à moins de 3 manas")
    assert out == {"query": "t:creature id<=g mv<=3", "source": "llm", "error": None}

    # Same question again: served from the meta cache, no second API call.
    again = nlquery.translate("Mes créatures vertes à moins de 3 manas")
    assert again["query"] == out["query"] and again["source"] == "cache"
    assert len(calls) == 1


def test_markdown_and_quotes_are_stripped_from_the_answer(fresh, monkeypatch):
    nlquery, _db = fresh
    _answers(nlquery, monkeypatch, '```\n"t:creature mv<=3"\n```')
    assert nlquery.translate("des petites créatures")["query"] == "t:creature mv<=3"


def test_an_unparseable_answer_triggers_one_repair_attempt(fresh, monkeypatch):
    """The model's output is re-parsed by the real engine, never trusted."""
    nlquery, _db = fresh
    calls = _answers(nlquery, monkeypatch, "couleur:vert t:creature", "id<=g t:creature")

    out = nlquery.translate("mes créatures vertes")
    assert out == {"query": "id<=g t:creature", "source": "llm", "error": None}
    assert len(calls) == 2
    # The repair prompt carries the rejected query and the parser's complaint.
    assert calls[1][1] == "couleur:vert t:creature"
    assert "couleur" in calls[1][2]


def test_two_bad_answers_fall_back_to_the_heuristic(fresh, monkeypatch):
    nlquery, _db = fresh
    calls = _answers(nlquery, monkeypatch, "couleur:vert", "encore:faux")

    out = nlquery.translate("mes créatures vertes")
    assert out["query"] == "id<=g t:creature"
    assert out["source"] == "heuristic"
    assert out["error"] is None          # the LLM is available: no key warning
    assert len(calls) == 2


def test_a_bad_answer_is_never_cached(fresh, monkeypatch):
    nlquery, db = fresh
    _answers(nlquery, monkeypatch, "couleur:vert")
    nlquery.translate("mes créatures vertes")
    assert db.get_meta(nlquery._cache_key("mes créatures vertes")) is None


def test_none_answer_reports_that_nothing_was_understood(fresh, monkeypatch):
    nlquery, _db = fresh
    _answers(nlquery, monkeypatch, "NONE")
    out = nlquery.translate("bonjour comment ça va")
    assert out["query"] == "" and out["error"]


def test_cache_key_depends_on_the_prompt_version_and_model(fresh, monkeypatch):
    nlquery, _db = fresh
    first = nlquery._cache_key("mes créatures vertes")
    monkeypatch.setattr(nlquery, "_PROMPT_VERSION", "99")
    assert nlquery._cache_key("mes créatures vertes") != first
