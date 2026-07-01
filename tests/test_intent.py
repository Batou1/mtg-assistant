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
    out = intent._coerce(
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
    assert intent._coerce({"format": "premodern"})["format"] == "premodern"


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
    assert intent._coerce({"max_colors": 2})["max_colors"] == 2
    assert intent._coerce({"max_colors": 0})["max_colors"] is None  # < 1 -> none
    assert intent._coerce({"max_colors": "x"})["max_colors"] is None


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


def test_coerce_max_card_price_eur():
    assert intent._coerce({"max_card_price_eur": "5"})["max_card_price_eur"] == 5.0
    assert intent._coerce({"max_card_price_eur": 0})["max_card_price_eur"] is None
    assert intent._coerce({"max_card_price_eur": "x"})["max_card_price_eur"] is None
    assert intent._coerce({})["max_card_price_eur"] is None
    assert intent._coerce({})["max_colors"] is None
