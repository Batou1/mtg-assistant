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

    Each entry of ``missing_cards`` is either a plain card name (one copy, the
    historical Commander/singleton case) or a ``(name, qty)`` pair for the
    non-singleton formats where several copies of a card may be missing. With a
    budget, copies are added greedily and a line can be partially filled (buy 2
    of the 4 missing copies). Counters (``bought_count``, ``missing_total``,
    ``unpriced``, ``over_cap``) count COPIES, which matches the old per-name
    counts for singleton callers.

    ``max_card_price`` additionally caps the price of any single card added —
    e.g. a 30€ total budget with a 5€ per-card cap never adds a 10€ card even
    though it would otherwise fit the remaining total.
    """
    wanted = []
    for entry in missing_cards:
        if isinstance(entry, (tuple, list)):
            name, qty = entry[0], entry[1]
        else:
            name, qty = entry, 1
        try:
            qty = max(1, int(qty))
        except (TypeError, ValueError):
            qty = 1
        wanted.append((name, qty))

    consider = wanted[:_MAX_PRICED]
    names = [n for n, _ in consider]
    resolved, _not_found = scryfall.resolve_cards(names, client=client) if names else ({}, [])

    items = []
    total = 0.0
    unpriced = 0
    over_cap = 0
    for name, qty in consider:
        card = resolved.get(name.strip().lower())
        if not card:
            unpriced += qty
            continue
        price = scryfall.price_eur(card)
        if price is None:
            unpriced += qty
            continue
        if max_card_price is not None and price > max_card_price:
            over_cap += qty
            continue
        take = qty
        if budget is not None and price > 0:
            # 1e-9 absorbs float noise so an exact-fit copy isn't rejected.
            take = min(qty, int((budget - total + 1e-9) // price))
        if take <= 0:
            continue
        items.append({
            "name": name,
            "image": scryfall.image(card),
            "price_eur": round(price, 2),
            "qty": take,
            "line_eur": round(price * take, 2),
        })
        total += price * take

    return {
        "budget_eur": budget,
        "max_card_price_eur": max_card_price,
        "to_buy": items,  # not "items": Jinja resolves dict.items to the method
        "total_eur": round(total, 2),
        "bought_count": sum(i["qty"] for i in items),
        "considered": len(consider),
        "missing_total": sum(q for _, q in wanted),
        "unpriced": unpriced,
        "over_cap": over_cap,
    }
