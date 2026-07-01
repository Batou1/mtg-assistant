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
