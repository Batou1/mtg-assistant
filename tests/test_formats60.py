from app import formats60, scryfall, db, llm, research


def _card(name, legal=True, eur=None, type_line="Creature — Test"):
    c = {
        "name": name,
        "type_line": type_line,
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
    "card e": _card("Card E", legal=True, eur=0.3),  # not in the LLM's deck
    "dual land": _card("Dual Land", legal=True, eur=0.5, type_line="Land"),
}


def _fake_resolve(names, client=None):
    found = {n.lower(): RESOLVED[n.lower()] for n in names if n.lower() in RESOLVED}
    missing = [n for n in names if n.lower() not in RESOLVED]
    return found, missing


ARCHETYPE = {
    "archetype": "Mono Red Aggro", "colors": ["R"], "strategy": "Tape vite.",
    "main_deck": [
        {"name": "Card A", "count": 6},      # clamped to the 4-of rule
        {"name": "Card B", "count": 4},
        {"name": "Card C", "count": 4},      # illegal in pauper -> dropped
        {"name": "Card D", "count": 4},      # nonexistent -> dropped
        {"name": "Dual Land", "count": 4},
        {"name": "Mountain", "count": 2},    # basic in main_deck -> basic pile
    ],
    "basic_lands": {"Mountain": 18},
}


def _analyze(monkeypatch, archetype=ARCHETYPE, owned=(), budget=5.0, extra=None):
    """``owned`` entries: (name, qty) or (name, qty, deck_qty)."""
    monkeypatch.setattr(research, "brave_search", lambda q, count=8: [])
    monkeypatch.setattr(llm, "archetype_research", lambda fmt, intent, context: archetype)
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    monkeypatch.setattr(db, "owned_quantities",
                        lambda pid: {e[0].lower(): (e[1], e[2] if len(e) > 2 else 0)
                                     for e in owned})
    intent = {"format": "pauper", "keywords": ["aggro"], "colors": ["R"],
              "budget_eur": budget, **(extra or {})}
    return formats60.analyze(intent, profile_id=1)


def test_query_includes_format_keywords_colors():
    q = formats60._query("pauper", {"keywords": ["aggro"], "colors": ["R"]})
    assert "pauper" in q and "aggro" in q and "red" in q


def test_full_deck_with_copies_and_manabase(monkeypatch):
    data = _analyze(monkeypatch, owned=[("Card A", 2)])

    deck = data["deck"]
    assert deck["counts"]["total"] == 60          # always topped up to 60
    assert set(data["dropped"]) == {"Card C", "Card D"}

    cards = {c["name"]: c for g in deck["groups"] for c in g["cards"]}
    assert cards["Card A"]["qty"] == 4            # 6 clamped to 4
    assert cards["Card B"]["qty"] == 4

    # Manabase: the 4 dual lands plus basics filling up to 60 total.
    nonbasic = {c["name"]: c for c in deck["lands"]["nonbasic"]}
    assert nonbasic["Dual Land"]["qty"] == 4
    basics = {c["name"]: c for c in deck["lands"]["basics"]}
    assert basics["Mountain"]["qty"] == 60 - 4 - 4 - 4  # spells + duals
    assert deck["counts"]["lands"] == 4 + 48
    assert deck["counts"]["total"] == deck["counts"]["spells"] + deck["counts"]["lands"]


def test_ownership_is_counted_per_copy(monkeypatch):
    data = _analyze(monkeypatch, owned=[("Card A", 2)])

    deck = data["deck"]
    cards = {c["name"]: c for g in deck["groups"] for c in g["cards"]}
    assert cards["Card A"]["owned_qty"] == 2
    assert cards["Card A"]["missing_qty"] == 2
    assert cards["Card A"]["owned"] is False
    # 2 Card A owned + all basics; missing: 2 Card A + 4 Card B + 4 Dual Land.
    assert deck["counts"]["owned"] == 2 + 48
    assert deck["counts"]["to_buy"] == 10
    assert data["owned_count"] == deck["counts"]["owned"]
    assert data["valid_count"] == 60

    by_name = {i["name"]: i for i in data["buylist"]["to_buy"]}
    assert by_name["Card A"]["qty"] == 2          # only the missing copies
    assert by_name["Card B"]["qty"] == 4
    assert by_name["Card B"]["line_eur"] == 4.0


def test_copies_in_other_decks_do_not_count_as_available(monkeypatch):
    # 4 copies owned but 3 already sleeved in other decks: only the free copy
    # counts, the 3 missing ones go to the buylist and are flagged in_deck_qty.
    data = _analyze(monkeypatch, owned=[("Card A", 4, 3)])

    deck = data["deck"]
    cards = {c["name"]: c for g in deck["groups"] for c in g["cards"]}
    assert cards["Card A"]["owned_qty"] == 1
    assert cards["Card A"]["missing_qty"] == 3
    assert cards["Card A"]["in_deck_qty"] == 3
    assert cards["Card A"]["owned"] is False

    by_name = {i["name"]: i for i in data["buylist"]["to_buy"]}
    assert by_name["Card A"]["qty"] == 3


def test_decklist_text_is_exportable(monkeypatch):
    data = _analyze(monkeypatch)
    text = data["deck"]["decklist_text"]
    lines = text.splitlines()
    assert "4 Card A" in lines
    assert "4 Dual Land" in lines
    assert "48 Mountain" in lines
    # A full 60-card export: quantities across all lines sum to the deck size.
    assert sum(int(ln.split(" ", 1)[0]) for ln in lines if ln) == 60


def test_legacy_key_cards_output_still_builds_a_deck(monkeypatch):
    legacy = {"archetype": "Mono Red", "colors": ["R"], "strategy": "",
              "key_cards": ["Card A", "Card B"]}
    data = _analyze(monkeypatch, archetype=legacy)
    deck = data["deck"]
    assert deck["counts"]["total"] == 60
    cards = {c["name"]: c for g in deck["groups"] for c in g["cards"]}
    assert cards["Card A"]["qty"] == 1            # bare names = one copy each


def test_llm_unavailable_is_reported(monkeypatch):
    monkeypatch.setattr(research, "brave_search", lambda q, count=8: [])
    monkeypatch.setattr(llm, "archetype_research", lambda fmt, intent, context: None)
    data = formats60.analyze({"format": "standard"}, profile_id=1)
    assert data["llm_unavailable"] is True


def test_formats_set_excludes_commander():
    assert "commander" not in formats60.FORMATS
    assert {"standard", "pauper"} <= formats60.FORMATS


def test_include_cards_forced_in_and_rejects_reported(monkeypatch):
    # The LLM's proposed deck ignored "Card E": it must still be force-added
    # (1 copy, flagged forced). Invalid requests are rejected with a reason
    # instead of being silently dropped, and exclusions leave the deck.
    data = _analyze(monkeypatch, extra={
        "include_cards": ["Card E", "Card C", "Card D"],
        "exclude_cards": ["Card B"],
    })
    deck = data["deck"]
    cards = {c["name"]: c for g in deck["groups"] for c in g["cards"]}
    assert cards["Card E"]["qty"] == 1
    assert cards["Card E"]["forced"] is True
    assert "Card B" not in cards
    assert data["forced_cards"] == ["Card E"]
    reasons = {r["name"]: r["reason"] for r in data["rejected_includes"]}
    assert "légale" in reasons["Card C"]
    assert "introuvable" in reasons["Card D"]
    assert data["excluded_cards"] == ["Card B"]
    assert deck["counts"]["total"] == 60  # basics re-top-up after changes


def test_include_cards_already_in_deck_keep_their_count(monkeypatch):
    # Requesting a card the LLM already put in the deck must not reset its
    # count to 1 — it just gets the forced flag.
    data = _analyze(monkeypatch, extra={"include_cards": ["Card A"]})
    cards = {c["name"]: c for g in data["deck"]["groups"] for c in g["cards"]}
    assert cards["Card A"]["qty"] == 4
    assert cards["Card A"]["forced"] is True
    assert data["forced_cards"] == ["Card A"]


def test_groups_are_sorted_alphabetically(monkeypatch):
    # The LLM proposes Card B before Card A; the displayed group (and the
    # exported decklist text, which follows group order) must be alphabetical.
    arch = {
        "archetype": "Aggro", "colors": ["R"], "strategy": "",
        "main_deck": [
            {"name": "Card B", "count": 4},
            {"name": "Card A", "count": 4},
        ],
        "basic_lands": {"Mountain": 20},
    }
    data = _analyze(monkeypatch, archetype=arch)
    creatures = data["deck"]["groups"][0]["cards"]
    assert [c["name"] for c in creatures] == ["Card A", "Card B"]
