"""Tests for EDHREC payload extraction (pure parsing, no network)."""
from app import edhrec


def _page(sections):
    return {"container": {"json_dict": {"cardlists": sections}}}


def test_extract_top_commanders_matches_by_tag():
    data = _page([
        {"tag": "topcommanders", "cardviews": [
            {"name": "Krenko, Mob Boss"}, {"name": "Krenko, Tin Street Kingpin"}]},
        {"tag": "creatures", "cardviews": [{"name": "Goblin Chieftain"}]},
    ])
    assert edhrec.extract_top_commanders(data) == [
        "Krenko, Mob Boss", "Krenko, Tin Street Kingpin"]


def test_extract_top_commanders_matches_by_header():
    data = _page([
        {"header": "Top Commanders", "cardviews": [{"name": "Prosper, Tome-Bound"}]},
    ])
    assert edhrec.extract_top_commanders(data) == ["Prosper, Tome-Bound"]


def test_extract_top_commanders_dedupes_and_ignores_other_sections():
    data = _page([
        {"tag": "commanders", "cardviews": [{"name": "A"}, {"name": "A"}, {"name": "B"}]},
        {"tag": "instants", "cardviews": [{"name": "Lightning Bolt"}]},
    ])
    assert edhrec.extract_top_commanders(data) == ["A", "B"]


def test_extract_top_commanders_empty_when_no_commander_section():
    data = _page([{"tag": "creatures", "cardviews": [{"name": "Goblin Chieftain"}]}])
    assert edhrec.extract_top_commanders(data) == []
