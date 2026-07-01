"""Build a budget-constrained shopping list from a commander's missing cards.

Cards are considered in EDHREC order (most synergistic first), priced in EUR
from Scryfall's Cardmarket data, and greedily added while the running total
stays within budget. With no budget, we simply price the most relevant missing
cards so you can see what completing the deck would cost.
"""
import httpx

from . import scryfall

# Cap how many missing cards we price per commander to bound Scryfall calls.
_MAX_PRICED = 60


def build(missing_cards, budget, max_card_price: float | None = None,
         client: httpx.Client | None = None) -> dict:
    """Return a buylist dict for ``missing_cards`` within ``budget`` (EUR or None).

    ``max_card_price`` additionally caps the price of any single card added —
    e.g. a 30€ total budget with a 5€ per-card cap never adds a 10€ card even
    though it would otherwise fit the remaining total.
    """
    consider = missing_cards[:_MAX_PRICED]
    resolved, _not_found = scryfall.resolve_cards(consider, client=client) if consider else ({}, [])

    items = []
    total = 0.0
    unpriced = 0
    over_cap = 0
    for name in consider:
        card = resolved.get(name.strip().lower())
        if not card:
            unpriced += 1
            continue
        price = scryfall.price_eur(card)
        if price is None:
            unpriced += 1
            continue
        if max_card_price is not None and price > max_card_price:
            over_cap += 1
            continue
        if budget is not None and total + price > budget:
            continue
        items.append(
            {"name": name, "image": scryfall.image(card), "price_eur": round(price, 2)}
        )
        total += price

    return {
        "budget_eur": budget,
        "max_card_price_eur": max_card_price,
        "to_buy": items,  # not "items": Jinja resolves dict.items to the method
        "total_eur": round(total, 2),
        "bought_count": len(items),
        "considered": len(consider),
        "missing_total": len(missing_cards),
        "unpriced": unpriced,
        "over_cap": over_cap,
    }
