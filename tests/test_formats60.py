from app import formats60, scryfall, db, llm, research, buylist


def _card(name, legal=True, eur=None):
    c = {
        "name": name,
        "legalities": {"pauper": "legal" if legal else "not_legal"},
        "image_uris": {"normal": f"http://img/{name}"},
    }
    if eur is not None:
        c["prices"] = {"eur": str(eur)}
    return c


RESOLVED = {
    "card a": _card("Card A", legal=True, eur=0.1),
    "card b": _card("Card B", legal=True, eur=1.0),
    "card c": _card("Card C", legal=False, eur=2.0),
    # "Card D" intentionally absent -> nonexistent card
}


def _fake_resolve(names, client=None):
    found = {n.lower(): RESOLVED[n.lower()] for n in names if n.lower() in RESOLVED}
    missing = [n for n in names if n.lower() not in RESOLVED]
    return found, missing


def test_query_includes_format_keywords_colors():
    q = formats60._query("pauper", {"keywords": ["aggro"], "colors": ["R"]})
    assert "pauper" in q and "aggro" in q and "red" in q


def test_validation_drops_illegal_and_nonexistent(monkeypatch):
    monkeypatch.setattr(research, "brave_search", lambda q, count=8: [])
    monkeypatch.setattr(
        llm, "archetype_research",
        lambda fmt, intent, context: {
            "archetype": "Mono Red Aggro", "colors": ["R"], "strategy": "Tape vite.",
            "key_cards": ["Card A", "Card B", "Card C", "Card D"],
        },
    )
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    monkeypatch.setattr(db, "owned_name_keys", lambda pid: {"card a"})

    intent = {"format": "pauper", "keywords": ["aggro"], "colors": ["R"], "budget_eur": 5.0}
    data = formats60.analyze(intent, profile_id=1)

    assert data["valid_count"] == 2          # A and B (legal + existing)
    assert data["owned_count"] == 1          # A owned
    assert data["missing_count"] == 1        # B missing
    assert set(data["dropped"]) == {"Card C", "Card D"}  # illegal + nonexistent
    assert data["archetype"]["name"] == "Mono Red Aggro"
    # Buylist prices the missing legal card within budget.
    bought = {i["name"] for i in data["buylist"]["to_buy"]}
    assert "Card B" in bought


def test_llm_unavailable_is_reported(monkeypatch):
    monkeypatch.setattr(research, "brave_search", lambda q, count=8: [])
    monkeypatch.setattr(llm, "archetype_research", lambda fmt, intent, context: None)
    data = formats60.analyze({"format": "standard"}, profile_id=1)
    assert data["llm_unavailable"] is True


def test_formats_set_excludes_commander():
    assert "commander" not in formats60.FORMATS
    assert {"standard", "pauper"} <= formats60.FORMATS
