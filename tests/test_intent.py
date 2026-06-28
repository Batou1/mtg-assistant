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
