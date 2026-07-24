from app import intent


def test_heuristic_colors_budget_and_keywords():
    parsed = intent._heuristic(
        "un deck Commander aristocrats sacrifice en noir et rouge, budget 50€"
    )
    assert parsed["format"] == "commander"
    assert set(parsed["colors"]) == {"B", "R"}
    assert parsed["budget_eur"] == 50.0
    assert "aristocrats" in parsed["keywords"]
    assert parsed["source"] == "heuristic"


def test_heuristic_english_and_euros_word():
    parsed = intent._heuristic("blue control deck, budget 120 euros")
    assert parsed["colors"] == ["U"]
    assert parsed["budget_eur"] == 120.0
    assert "control" in parsed["keywords"]


def test_coerce_filters_invalid_values():
    out = intent.coerce(
        {"format": "pauperish", "colors": ["W", "X", "u"], "budget_eur": "nope"}
    )
    assert out["format"] is None
    assert out["colors"] == ["W", "U"]
    assert out["budget_eur"] is None


def test_parse_intent_empty():
    out = intent.parse_intent("   ")
    assert out["colors"] == [] and out["budget_eur"] is None


def test_heuristic_mono_lifegain_black_or_white():
    parsed = intent._heuristic(
        "Un deck gain de vie monocouleur en noir ou en blanc. Budget max 50€"
    )
    assert set(parsed["colors"]) == {"B", "W"}
    assert parsed["max_colors"] == 1
    assert parsed["budget_eur"] == 50.0


def test_heuristic_premodern_format():
    parsed = intent._heuristic("un deck Premodern agressif en rouge")
    assert parsed["format"] == "premodern"
    assert parsed["colors"] == ["R"]


def test_heuristic_premodern_hyphen_not_confused_with_modern():
    # "pre-modern" must resolve to premodern, never to modern.
    assert intent._heuristic("un deck pre-modern blanc")["format"] == "premodern"
    assert intent._heuristic("un deck prémoderne")["format"] == "premodern"
    # plain "modern" still works.
    assert intent._heuristic("un deck modern burn")["format"] == "modern"


def test_coerce_accepts_premodern():
    assert intent.coerce({"format": "premodern"})["format"] == "premodern"


def test_heuristic_duel_commander_format():
    parsed = intent._heuristic("un deck duel commander agressif en rouge")
    assert parsed["format"] == "duelcommander"
    assert parsed["colors"] == ["R"]


def test_heuristic_pauper_commander_format():
    parsed = intent._heuristic("un deck pauper commander en bleu noir")
    assert parsed["format"] == "paupercommander"
    assert set(parsed["colors"]) == {"U", "B"}


def test_heuristic_pdh_abbreviation():
    assert intent._heuristic("un deck pdh mono vert")["format"] == "paupercommander"


def test_heuristic_duel_commander_not_confused_with_plain_commander():
    # "commander" is a substring word of "duel commander" — the more specific
    # multi-word entry must win, not the generic "commander" fallback.
    assert intent._heuristic("commander duel en boros")["format"] == "duelcommander"


def test_heuristic_pauper_commander_not_confused_with_plain_pauper():
    # Same trap as above but for "pauper" vs "pauper commander".
    assert intent._heuristic("deck pauper agro rouge")["format"] == "pauper"
    assert intent._heuristic("deck pauper commander rouge")["format"] == "paupercommander"


def test_coerce_accepts_duelcommander_and_paupercommander():
    assert intent.coerce({"format": "duelcommander"})["format"] == "duelcommander"
    assert intent.coerce({"format": "paupercommander"})["format"] == "paupercommander"


def test_heuristic_shard_name_expands_to_colors():
    # "Grixis commanders" must constrain the colour filter to U/B/R, otherwise
    # off-colour commanders surface and the requested ones get buried.
    parsed = intent._heuristic("propose Grixis commanders with less than 300€")
    assert set(parsed["colors"]) == {"U", "B", "R"}
    assert parsed["budget_eur"] == 300.0


def test_heuristic_guild_and_wedge_names():
    assert set(intent._heuristic("deck rakdos aggro")["colors"]) == {"B", "R"}
    assert set(intent._heuristic("deck jeskai control")["colors"]) == {"U", "R", "W"}
    assert set(intent._heuristic("commandants esper")["colors"]) == {"W", "U", "B"}


def test_heuristic_euro_symbol_budget_without_keyword():
    # Regression: "<n>€" / "<n> €" must be detected even without the word "budget".
    assert intent._heuristic("un deck mono noir, 50€")["budget_eur"] == 50.0
    assert intent._heuristic("moins de 300 € en bleu")["budget_eur"] == 300.0
    # "euros" word form still works and "europe" is not mistaken for a budget.
    assert intent._heuristic("budget 40 euros")["budget_eur"] == 40.0
    assert intent._heuristic("un deck europe")["budget_eur"] is None


def test_coerce_max_colors():
    assert intent.coerce({"max_colors": 2})["max_colors"] == 2
    assert intent.coerce({"max_colors": 0})["max_colors"] is None  # < 1 -> none
    assert intent.coerce({"max_colors": "x"})["max_colors"] is None


def test_heuristic_per_card_price_cap_with_total_budget():
    parsed = intent._heuristic(
        "un deck Commander budget max de 30€ mais chaque carte ne doit pas "
        "coûter plus de 5€"
    )
    assert parsed["budget_eur"] == 30.0
    assert parsed["max_card_price_eur"] == 5.0


def test_heuristic_per_card_price_cap_phrasings():
    assert intent._heuristic(
        "deck aristocrats noir, budget 30 euros, max 5€ par carte"
    )["max_card_price_eur"] == 5.0
    assert intent._heuristic(
        "deck rouge budget 30€, 5€ max par carte"
    )["max_card_price_eur"] == 5.0
    assert intent._heuristic(
        "deck rouge budget 30€, chaque carte à 5€ max"
    )["max_card_price_eur"] == 5.0


def test_heuristic_no_per_card_cap_when_not_mentioned():
    assert intent._heuristic("deck bleu control, budget 50 euros")["max_card_price_eur"] is None


def test_heuristic_wedge_name_requires_all_colors():
    # "un commandant temur" means exactly G/U/R — a 2-colour subset (Simic,
    # Izzet…) must be filtered out downstream, hence min_colors=3.
    parsed = intent._heuristic("je veux un commandant temur")
    assert set(parsed["colors"]) == {"G", "U", "R"}
    assert parsed["min_colors"] == 3
    assert intent._heuristic("deck izzet spellslinger")["min_colors"] == 2


def test_heuristic_conjunctive_color_list_requires_all():
    parsed = intent._heuristic("un deck commander bleu vert et rouge")
    assert set(parsed["colors"]) == {"U", "G", "R"}
    assert parsed["min_colors"] == 3


def test_heuristic_disjunctive_colors_keep_subset_semantics():
    # "noir ou blanc" offers alternatives: mono-black and mono-white are fine.
    parsed = intent._heuristic("un deck lifegain en noir ou en blanc")
    assert set(parsed["colors"]) == {"B", "W"}
    assert parsed["min_colors"] is None


def test_heuristic_mono_wins_over_color_list():
    # "monocouleur noir ou blanc": the explicit ceiling must not be
    # contradicted by an inferred floor.
    parsed = intent._heuristic("un deck monocouleur en noir ou en blanc")
    assert parsed["max_colors"] == 1
    assert parsed["min_colors"] is None


def test_heuristic_exactement_requires_all_colors():
    parsed = intent._heuristic("exactement les couleurs bleu, vert ou rouge")
    assert parsed["min_colors"] == 3


def test_coerce_min_colors():
    assert intent.coerce({"min_colors": 3, "colors": ["U", "G", "R"]})["min_colors"] == 3
    assert intent.coerce({"min_colors": 0})["min_colors"] is None
    assert intent.coerce({"min_colors": "x"})["min_colors"] is None
    # A floor above the listed colours would match nothing: capped to the list.
    assert intent.coerce({"min_colors": 4, "colors": ["U", "G"]})["min_colors"] == 2
    # An explicit ceiling (mono) wins over a conflicting floor.
    assert intent.coerce({"min_colors": 2, "max_colors": 1,
                          "colors": ["B", "W"]})["min_colors"] is None
    assert intent.coerce({})["min_colors"] is None


def test_coerce_max_card_price_eur():
    assert intent.coerce({"max_card_price_eur": "5"})["max_card_price_eur"] == 5.0
    assert intent.coerce({"max_card_price_eur": 0})["max_card_price_eur"] is None
    assert intent.coerce({"max_card_price_eur": "x"})["max_card_price_eur"] is None
    assert intent.coerce({})["max_card_price_eur"] is None
    assert intent.coerce({})["max_colors"] is None
