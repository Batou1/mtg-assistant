"""Parse a ManaBox CSV export into normalized collection rows.

ManaBox exports one row per printing/finish with a header like:

    Binder Name,Binder Type,Name,Set code,Set name,Collector number,Foil,
    Rarity,Quantity,ManaBox ID,Scryfall ID,Purchase price,Misprint,Altered,
    Condition,Language,Purchase price currency

We read it tolerantly: column lookups are case-insensitive with fallbacks, so
small header changes between ManaBox versions don't break the import. The
Scryfall ID, when present, lets us resolve the exact card later.

"Binder Type" tells us where the copies live in ManaBox: "binder" (and "list")
means spare cards, "deck" means the copies are already sleeved in one of the
user's decks — deck generation should avoid leaning on those.
"""
import csv
import io


def _norm_header(name: str) -> str:
    return (name or "").strip().lower()


def _pick(row: dict, *candidates: str) -> str:
    """Return the first non-empty value among ``candidates`` (case-insensitive)."""
    for cand in candidates:
        key = cand.lower()
        for col, val in row.items():
            if _norm_header(col) == key and (val or "").strip():
                return val.strip()
    return ""


def parse_manabox_csv(text: str):
    """Return (rows, errors).

    rows: list of dicts ready for db.replace_collection — keys scryfall_id,
    name_key, raw_name, set_code, foil, condition, quantity, binder_type,
    binder_name (the deck/binder the copies live in — for "deck" rows this is
    the deck's name, which feeds the player-style memory).
    errors: list of raw lines that had no usable card name.
    """
    rows = []
    errors = []

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return rows, errors

    for raw in reader:
        name = _pick(raw, "Name", "Card", "Card Name")
        if not name:
            joined = ",".join(v for v in raw.values() if v)
            if joined.strip():
                errors.append(joined)
            continue

        qty_str = _pick(raw, "Quantity", "Count", "Qty") or "1"
        try:
            quantity = int(float(qty_str))
        except ValueError:
            quantity = 1
        if quantity <= 0:
            continue

        foil_str = _pick(raw, "Foil", "Finish", "Printing").lower()
        foil = 1 if foil_str in ("foil", "etched", "true", "yes", "1") else 0

        rows.append(
            {
                "scryfall_id": _pick(raw, "Scryfall ID", "Scryfall Id", "ScryfallID"),
                "name_key": name.split("//")[0].strip().lower(),
                "raw_name": name,
                "set_code": _pick(raw, "Set code", "Set Code", "Set", "Edition").upper(),
                "foil": foil,
                "condition": _pick(raw, "Condition") or "near_mint",
                "quantity": quantity,
                "binder_type": _pick(raw, "Binder Type", "Binder type").lower(),
                "binder_name": _pick(raw, "Binder Name", "Binder name"),
            }
        )

    return rows, errors
