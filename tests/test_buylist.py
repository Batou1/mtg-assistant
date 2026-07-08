from app import buylist, scryfall


def card(name, eur):
    return {"name": name, "image_uris": {"normal": f"http://img/{name}"},
            "prices": {"eur": str(eur)}}


CARDS = {
    "cheap card": card("Cheap Card", 1.0),
    "mid card": card("Mid Card", 4.0),
    "pricey card": card("Pricey Card", 20.0),
}


def _fake_resolve(names, client=None):
    res, nf = {}, []
    for n in names:
        k = n.strip().lower()
        (res.__setitem__(k, CARDS[k]) if k in CARDS else nf.append(n))
    return res, nf


def test_max_card_price_excludes_expensive_cards_even_within_budget(monkeypatch):
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    result = buylist.build(
        ["Cheap Card", "Mid Card", "Pricey Card"], budget=30.0, max_card_price=5.0,
    )
    names = {c["name"] for c in result["to_buy"]}
    assert names == {"Cheap Card", "Mid Card"}
    assert "Pricey Card" not in names
    assert result["over_cap"] == 1
    assert result["total_eur"] == 5.0
    assert result["max_card_price_eur"] == 5.0


def test_no_max_card_price_keeps_prior_behaviour(monkeypatch):
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    result = buylist.build(["Cheap Card", "Mid Card", "Pricey Card"], budget=30.0)
    names = {c["name"] for c in result["to_buy"]}
    assert names == {"Cheap Card", "Mid Card", "Pricey Card"}
    assert result["over_cap"] == 0
    assert result["max_card_price_eur"] is None


def test_plain_names_count_as_one_copy(monkeypatch):
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    result = buylist.build(["Cheap Card", "Mid Card"], budget=None)
    assert all(i["qty"] == 1 for i in result["to_buy"])
    assert all(i["line_eur"] == i["price_eur"] for i in result["to_buy"])
    assert result["bought_count"] == 2
    assert result["missing_total"] == 2


def test_quantities_are_priced_per_copy(monkeypatch):
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    result = buylist.build([("Cheap Card", 4), ("Mid Card", 2)], budget=None)
    by_name = {i["name"]: i for i in result["to_buy"]}
    assert by_name["Cheap Card"]["qty"] == 4
    assert by_name["Cheap Card"]["line_eur"] == 4.0
    assert by_name["Mid Card"]["line_eur"] == 8.0
    assert result["total_eur"] == 12.0
    assert result["bought_count"] == 6
    assert result["missing_total"] == 6


def test_budget_fills_a_line_partially(monkeypatch):
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    # 4x Mid Card = 16 € but only 10 € available: buy 2 copies, not 0 or 4.
    result = buylist.build([("Mid Card", 4)], budget=10.0)
    assert result["to_buy"][0]["qty"] == 2
    assert result["total_eur"] == 8.0
    assert result["bought_count"] == 2


def test_max_card_price_counts_skipped_copies(monkeypatch):
    monkeypatch.setattr(scryfall, "resolve_cards", _fake_resolve)
    result = buylist.build([("Pricey Card", 3)], budget=100.0, max_card_price=5.0)
    assert result["to_buy"] == []
    assert result["over_cap"] == 3
