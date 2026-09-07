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
    # Budget-pass fixtures: an unaffordable bomb and its cheap replacement.
    "power card": _card("Power Card", legal=True, eur=500.0),
    "cheap card": _card("Cheap Card", legal=True, eur=0.05),
    "unpriced card": _card("Unpriced Card", legal=True),  # no EUR price at all
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


def _analyze(monkeypatch, archetype=ARCHETYPE, owned=(), budget=5.0, extra=None,
             revise=None):
    """``owned`` entries: (name, qty) or (name, qty, deck_qty).

    ``revise`` is the archetype the budget rewrite pass returns (None disables
    the pass, which is also what happens without an Anthropic key).
    """
    monkeypatch.setattr(research, "brave_search", lambda q, count=8: [])
    monkeypatch.setattr(llm, "archetype_research", lambda fmt, intent, context: archetype)
    monkeypatch.setattr(llm, "is_available", lambda: revise is not None)
    monkeypatch.setattr(llm, "archetype_revise",
                        lambda fmt, intent, previous, report, cost: revise)
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


# --- Budget ---------------------------------------------------------------
#
# Regression: the deck used to be built with no notion of what it costs, and
# the UI quoted the BUYLIST total (which stops at the budget) as "the price",
# so a 4000 € Vintage deck was announced as "190 € / 200 € budget".

def _deck_of(*entries, basics=20):
    return {"archetype": "Test", "colors": ["R"], "strategy": "",
            "main_deck": [{"name": n, "count": c} for n, c in entries],
            "basic_lands": {"Mountain": basics}}


EXPENSIVE = _deck_of(("Power Card", 4), ("Card A", 4), ("Dual Land", 4))
CHEAP = _deck_of(("Cheap Card", 4), ("Card A", 4), ("Dual Land", 4))


def test_deck_cost_is_the_full_price_of_the_missing_copies(monkeypatch):
    # 4 Card A (0.10) + 4 Card B (1.00) + 4 Dual Land (0.50) = 6.40 €, of which
    # only 5 € fits the budget: both figures are reported, and they differ.
    data = _analyze(monkeypatch, budget=5.0)
    assert data["deck_cost_eur"] == 6.4
    assert data["budget_exceeded"] is True
    assert data["over_budget_eur"] == 1.4
    assert data["buylist"]["total_eur"] < data["deck_cost_eur"]


def test_owned_copies_are_not_counted_in_the_deck_cost(monkeypatch):
    data = _analyze(monkeypatch, owned=[("Card B", 4)], budget=5.0)
    assert data["deck_cost_eur"] == 0.4 + 2.0    # Card A + Dual Land only
    assert data["budget_exceeded"] is False
    assert data["over_budget_eur"] == 0.0


def test_over_budget_deck_is_rebuilt_with_real_prices(monkeypatch):
    # First proposal: 4x a 500 € card. Given the actual prices, the model
    # comes back with an affordable list and that one is kept.
    data = _analyze(monkeypatch, archetype=EXPENSIVE, budget=5.0, revise=CHEAP)
    cards = {c["name"] for g in data["deck"]["groups"] for c in g["cards"]}
    assert "Power Card" not in cards
    assert "Cheap Card" in cards
    assert data["deck_cost_eur"] == 0.2 + 0.4 + 2.0
    assert data["budget_exceeded"] is False
    assert data["budget_passes"] == 1
    assert data["deck"]["counts"]["total"] == 60


def test_revision_that_is_not_cheaper_is_discarded(monkeypatch):
    pricier = _deck_of(("Power Card", 4), ("Card B", 4), ("Dual Land", 4))
    data = _analyze(monkeypatch, archetype=EXPENSIVE, budget=5.0, revise=pricier)
    cards = {c["name"] for g in data["deck"]["groups"] for c in g["cards"]}
    assert cards == {"Power Card", "Card A"}     # the original build survives
    assert data["budget_exceeded"] is True


def test_degenerate_revision_is_refused(monkeypatch):
    # A rewrite that keeps almost nothing is cheap only because it isn't a
    # deck any more: it must not replace a working (if pricey) list.
    empty = _deck_of(("Cheap Card", 1), basics=59)
    data = _analyze(monkeypatch, archetype=EXPENSIVE, budget=5.0, revise=empty)
    cards = {c["name"] for g in data["deck"]["groups"] for c in g["cards"]}
    assert "Power Card" in cards
    assert data["budget_exceeded"] is True


def test_no_revision_pass_without_a_budget(monkeypatch):
    data = _analyze(monkeypatch, archetype=EXPENSIVE, budget=None, revise=CHEAP)
    cards = {c["name"] for g in data["deck"]["groups"] for c in g["cards"]}
    assert "Power Card" in cards                  # nothing to fit into
    assert data["budget_exceeded"] is False
    assert data["budget_passes"] == 0
    assert data["deck_cost_eur"] == 2000.4 + 2.0


def test_cards_above_the_per_card_cap_are_reported(monkeypatch):
    data = _analyze(monkeypatch, archetype=EXPENSIVE, budget=5000.0,
                    extra={"max_card_price_eur": 10.0})
    assert data["over_cap_cards"] == ["Power Card"]
    # Total budget is fine, the per-card ceiling is not: the two constraints
    # stay independent.
    assert data["budget_exceeded"] is False


def test_missing_copies_without_a_price_are_flagged_not_counted_as_free(monkeypatch):
    arch = _deck_of(("Unpriced Card", 4), ("Card A", 4))
    data = _analyze(monkeypatch, archetype=arch, budget=5.0)
    assert data["unpriced_missing"] == 4
    assert data["deck_cost_eur"] == 0.4


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


# --- Collection-only mode (budget 0 € / owned_only) -----------------------

def _analyze_owned_only(monkeypatch, owned, selection, budget=0.0, extra=None,
                        llm_available=True):
    """Run ``analyze`` in collection-only mode.

    ``selection`` is what the collection-aware LLM call returns (None = no key,
    the heuristic pool selection takes over). The metagame research calls
    must never run in this mode, so they fail loudly if reached.
    """
    def never(*_a, **_k):
        raise AssertionError("metagame research must not run in owned-only mode")

    calls = {}
    monkeypatch.setattr(research, "brave_search", lambda q, count=8: [])
    monkeypatch.setattr(llm, "archetype_research", never)
    monkeypatch.setattr(llm, "archetype_revise", never)
    monkeypatch.setattr(llm, "is_available", lambda: llm_available)

    def from_collection(fmt, intent, pool_lines, context=""):
        calls["pool_lines"] = pool_lines
        return selection

    monkeypatch.setattr(llm, "archetype_from_collection", from_collection)
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    quantities = {e[0].lower(): (e[1], e[2] if len(e) > 2 else 0) for e in owned}
    monkeypatch.setattr(db, "owned_quantities", lambda pid: quantities)
    monkeypatch.setattr(db, "collection_names",
                        lambda pid: [(e[0], e[0].lower(), e[1]) for e in owned])
    monkeypatch.setattr(db, "get_cards",
                        lambda keys, ttl_days=None: {k: RESOLVED[k] for k in keys
                                                     if k in RESOLVED})
    intent = {"format": "pauper", "keywords": ["aggro"], "colors": ["R"],
              "budget_eur": budget, **(extra or {})}
    return formats60.analyze(intent, profile_id=1), calls


OWNED = [("Card A", 4), ("Card B", 2), ("Card C", 4), ("Card E", 3),
         ("Dual Land", 2), ("Mountain", 20)]


def test_zero_budget_builds_from_the_collection_only(monkeypatch):
    # The model was shown the owned pool but still names a card the player
    # doesn't have (Power Card) and asks for more Card B than owned: neither
    # may become a purchase — the deck costs 0 € by construction.
    data, calls = _analyze_owned_only(monkeypatch, OWNED, _deck_of(
        ("Card A", 4), ("Card B", 4), ("Power Card", 4), ("Dual Land", 2)))

    assert data["owned_only"] is True
    assert data["owned_pool_empty"] is False
    deck = data["deck"]
    cards = {c["name"]: c for g in deck["groups"] for c in g["cards"]}
    assert "Power Card" not in cards
    assert data["not_owned"] == ["Power Card"]
    assert cards["Card A"]["qty"] == 4
    assert cards["Card B"]["qty"] == 2           # clamped to the owned copies
    assert deck["counts"]["to_buy"] == 0
    assert deck["counts"]["total"] == 60
    assert data["deck_cost_eur"] == 0
    assert data["budget_exceeded"] is False
    assert data["budget_passes"] == 0
    assert data["buylist"]["to_buy"] == []


def test_owned_pool_is_legal_free_copies_only(monkeypatch):
    # Card C is illegal in pauper and basics are handled by the manabase:
    # neither reaches the model. Copies sleeved in other decks don't either.
    owned = OWNED + [("Cheap Card", 3, 3)]
    data, calls = _analyze_owned_only(monkeypatch, owned, _deck_of(("Card A", 4)))
    lines = "\n".join(calls["pool_lines"])
    assert "Card A (x4)" in lines
    assert "Card B (x2)" in lines
    assert "Card C" not in lines
    assert "Mountain" not in lines
    assert "Cheap Card" not in lines
    assert data["owned_pool_size"] == 4      # A, B, E, Dual Land


def test_owned_only_flag_works_without_a_budget(monkeypatch):
    data, _calls = _analyze_owned_only(
        monkeypatch, OWNED, _deck_of(("Card A", 4), ("Card B", 2)),
        budget=None, extra={"owned_only": True})
    assert data["owned_only"] is True
    assert data["deck"]["counts"]["to_buy"] == 0
    assert data["budget_exceeded"] is False


def test_owned_only_falls_back_to_the_heuristic_without_llm(monkeypatch):
    data, _calls = _analyze_owned_only(monkeypatch, OWNED, None, llm_available=False)
    assert data.get("llm_unavailable") is False
    deck = data["deck"]
    cards = {c["name"]: c for g in deck["groups"] for c in g["cards"]}
    assert set(cards) <= {"Card A", "Card B", "Card E"}
    assert cards["Card A"]["qty"] == 4
    assert deck["counts"]["to_buy"] == 0
    assert deck["counts"]["total"] == 60


def test_owned_only_with_nothing_playable_says_so(monkeypatch):
    # Only an illegal card and basics: no deck is padded out of basic lands.
    data, _calls = _analyze_owned_only(
        monkeypatch, [("Card C", 4), ("Mountain", 20)], _deck_of(("Card A", 4)))
    assert data["owned_pool_empty"] is True
    assert "deck" not in data
    assert data["archetype"]["colors"] == ["R"]


def test_owned_only_keeps_a_forced_card_even_if_not_owned(monkeypatch):
    # An explicit player request wins over the "nothing bought" rule, and its
    # cost is reported honestly rather than hidden.
    data, _calls = _analyze_owned_only(
        monkeypatch, OWNED, _deck_of(("Card A", 4)),
        extra={"include_cards": ["Cheap Card"]})
    cards = {c["name"]: c for g in data["deck"]["groups"] for c in g["cards"]}
    assert cards["Cheap Card"]["forced"] is True
    assert cards["Cheap Card"]["missing_qty"] == 1
    assert data["deck_cost_eur"] == 0.05
    assert data["forced_cards"] == ["Cheap Card"]


def test_owned_only_helper():
    assert formats60.owned_only({"budget_eur": 0}) is True
    assert formats60.owned_only({"budget_eur": 0.0}) is True
    assert formats60.owned_only({"owned_only": True}) is True
    assert formats60.owned_only({"budget_eur": 5.0}) is False
    assert formats60.owned_only({"budget_eur": None}) is False
