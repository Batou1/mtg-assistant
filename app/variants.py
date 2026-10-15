"""Which card-recommendation source to use for each Commander variant.

EDHREC only aggregates regular (4-player, casual) Commander. Reusing its page
for the variants — filtered by Scryfall legality — produced decks that were
"Commander minus the illegal cards" rather than real Duel or Pauper decks:

- **Pauper Commander** → PDHREC (``app.pdhrec``), the EDHREC equivalent built
  from PDH decklists only (it also has pages for the non-legendary uncommon
  commanders EDHREC knows nothing about);
- **Duel Commander** → MTGTop8 tournament results (``app.mtgtop8``),
  aggregated from the newest top-8 decklists of the commander — 1-vs-1 at
  20 life plays a very different game from multiplayer EDH;
- **Commander** → EDHREC, as before.

Every source returns the EDHREC payload shape, so ``deckgen`` and ``analysis``
consume them unchanged. When the variant source has nothing (unknown
commander, too few tournament results, site down), the EDHREC page is used as
a fallback — still filtered by the variant's legality downstream — and the
payload says so (``_fallback``), so the UI and the chat never present a
casual-EDH pool as tournament data. EDHREC's sentinel contract
(``_not_found`` / ``_error``) holds for the merged result.
"""
from functools import partial

import httpx

from . import edhrec, mtgtop8, pdhrec, scryfall
from .config import settings

_LABELS = {"edhrec": "EDHREC", pdhrec.SOURCE: pdhrec.LABEL, mtgtop8.SOURCE: mtgtop8.LABEL}

#: The source a variant is supposed to use (anything else is a fallback).
NATIVE_SOURCE = {
    "commander": "edhrec",
    "paupercommander": pdhrec.SOURCE,
    "duelcommander": mtgtop8.SOURCE,
}

_FALLBACK_REASONS = {
    "paupercommander": "commandant absent de PDHREC",
    "duelcommander": "pas assez de résultats de tournoi Duel Commander sur MTGTop8",
}


def _bad(data: dict) -> bool:
    return bool(data.get("_not_found") or data.get("_error"))


def _duel_metagame_keys(client) -> set[str]:
    return {scryfall.norm_name(r["name"]) for r in mtgtop8.fetch_metagame(client=client)}


def _with_complement(data: dict, extra: dict) -> dict:
    """``data`` plus ``extra``'s cards it lacks, ranked strictly below them.

    A PDHREC page lists ~85 cards and a handful of tournament lists may not
    cover 99 slots once the legality filter has run: deckgen would leave
    slots empty. The EDHREC page fills the gap — still filtered by the
    variant's legality downstream — but every complement card gets an
    inclusion below the least-played native card, so it is only ever used
    once the variant's own data is exhausted.
    """
    if _bad(extra):
        return data
    lists = edhrec._json_dict(data).get("cardlists") or []
    present = {scryfall.norm_name(v.get("name") or "")
               for sec in lists for v in sec.get("cardviews") or []}
    floor = min((v.get("inclusion") or 0 for sec in lists
                 for v in sec.get("cardviews") or []), default=1)
    extra_lists = edhrec._json_dict(extra).get("cardlists") or []
    top = max((v.get("inclusion") or v.get("num_decks") or 0 for sec in extra_lists
               for v in sec.get("cardviews") or []), default=0) or 1
    added = []
    for sec in extra_lists:
        views = []
        for v in sec.get("cardviews") or []:
            key = scryfall.norm_name(v.get("name") or "")
            if not key or key in present:
                continue
            present.add(key)
            share = (v.get("inclusion") or v.get("num_decks") or 0) / top
            views.append({"name": v["name"], "inclusion": floor * 0.99 * share,
                          "complement": True})
        if views:
            added.append({**sec, "cardviews": views})
    if not added:
        return data
    container = {"json_dict": {**edhrec._json_dict(data), "cardlists": lists + added}}
    return {**data, "container": container, "_complemented": True}


def _tag_for(card: dict | None) -> str:
    return mtgtop8._tag_for(card)


def _pdh_staples_payload(name: str, client) -> dict:
    """PDHREC's format-wide staples within the commander's colour identity, as
    an EDHREC-shaped payload (typed via the local Scryfall cache — the staples
    page has no type line, and a land must never be counted as a spell)."""
    resolved, _nf = scryfall.resolve_cards([name], client=client)
    commander = resolved.get(scryfall.norm_name(name))
    if commander is None:
        return {"_not_found": True}
    identity = set(scryfall.color_identity(commander))
    staples = [s for s in pdhrec.fetch_staples(client=client)
               if set(s["color_identity"]) <= identity]
    if not staples:
        return {"_not_found": True}
    typed, _nf = scryfall.resolve_cards([s["name"] for s in staples], client=client)
    sections: dict[str, list] = {}
    for s in staples:
        tag = _tag_for(typed.get(scryfall.norm_name(s["name"])))
        sections.setdefault(tag, []).append({"name": s["name"], "inclusion": s["pct"]})
    return {"container": {"json_dict": {"cardlists": [
        {"tag": tag, "cardviews": views} for tag, views in sections.items()]}}}


def fetch_commander(name: str, fmt: str = "commander",
                    client: httpx.Client | None = None, *, scoring: bool = False,
                    complete: bool = False) -> dict:
    """Recommendation payload for ``name`` in ``fmt`` (EDHREC shape + ``_source``).

    ``scoring`` is the cheap mode for ranking many candidates at once
    (``analysis``): a Duel Commander aggregate costs ~20 requests the first
    time, so only commanders present in the current tournament metagame are
    aggregated there; the others go straight to the EDHREC fallback. Building
    one deck (``deckgen``) always tries the variant source, with ``complete``
    to top a short native pool up from EDHREC (:func:`_with_complement`) —
    never for scoring, where completion % must reflect the variant's data.
    """
    if fmt == "paupercommander":
        primary = pdhrec.fetch_commander
    elif fmt == "duelcommander":
        primary = mtgtop8.fetch_commander
        if scoring:
            if scryfall.norm_name(name) in _duel_metagame_keys(client):
                primary = partial(mtgtop8.fetch_commander,
                                  sample=settings.duel_scoring_sample)
            else:
                primary = None
    else:
        data = edhrec.fetch_commander(name, client=client)
        return data if _bad(data) else {**data, "_source": "edhrec"}

    data = primary(name, client=client) if primary else {"_not_found": True}
    if not _bad(data):
        if complete:
            # Commander-specific EDHREC page first (when the commander has
            # one — non-legendary PDH commanders don't), then PDH staples.
            data = _with_complement(data, edhrec.fetch_commander(name, client=client))
            if fmt == "paupercommander":
                data = _with_complement(data, _pdh_staples_payload(name, client))
        return data

    fallback = edhrec.fetch_commander(name, client=client)
    if _bad(fallback):
        # Prefer reporting the variant source's error (retryable) over EDHREC's
        # "not found", so a transient outage is never cached as "no data".
        return data if data.get("_error") else fallback
    reason = _FALLBACK_REASONS.get(fmt, "")
    if data.get("_error"):
        reason = f"{_LABELS[NATIVE_SOURCE[fmt]]} injoignable"
    return {**fallback, "_source": "edhrec", "_fallback": True,
            "_fallback_reason": reason}


def source_info(data: dict, fmt: str = "commander") -> dict:
    """Display facts about where a payload came from (for the UI and the chat)."""
    source = data.get("_source") or "edhrec"
    url = data.get("_url")
    return {
        "source": source,
        "label": _LABELS.get(source, source),
        "url": url,
        "num_decks": edhrec.extract_num_decks(data),
        "sample_size": data.get("sample_size"),
        "native": source == NATIVE_SOURCE.get(fmt, "edhrec"),
        "fallback": bool(data.get("_fallback")),
        "fallback_reason": data.get("_fallback_reason") or "",
    }


def describe(info: dict) -> str:
    """One French sentence describing the source, for the chat transcript."""
    if info.get("fallback"):
        return (f"Source : EDHREC (Commander multijoueur) filtré par légalité, faute "
                f"de mieux — {info.get('fallback_reason') or 'source dédiée indisponible'}.")
    if info.get("source") == mtgtop8.SOURCE:
        return (f"Source : {info['label']}, {info.get('sample_size') or 0} decklists de "
                f"tournoi agrégées sur {info.get('num_decks') or 0} résultats récents.")
    if info.get("source") == pdhrec.SOURCE:
        return f"Source : {info['label']}, {info.get('num_decks') or 0} decks Pauper Commander."
    return f"Source : EDHREC, {info.get('num_decks') or 0} decks."
