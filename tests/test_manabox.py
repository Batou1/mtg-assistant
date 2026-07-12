from app import manabox

SAMPLE = (
    "Name,Set code,Set name,Collector number,Foil,Rarity,Quantity,"
    "ManaBox ID,Scryfall ID,Purchase price,Misprint,Altered,Condition,"
    "Language,Purchase price currency\n"
    "Sol Ring,LTC,Lord of the Rings Commander,280,normal,uncommon,2,"
    "123,abc-123,1.50,false,false,near_mint,en,EUR\n"
    "Lightning Bolt,2X2,Double Masters 2022,117,foil,common,1,"
    "456,def-456,3.20,false,false,lightly_played,en,EUR\n"
    "Atraxa, Praetors' Voice,CMR,Commander Legends,28,normal,mythic,1,"
    "789,ghi-789,8.00,false,false,near_mint,en,EUR\n"
)


def test_parses_rows_with_all_fields():
    rows, errors = manabox.parse_manabox_csv(SAMPLE)
    assert errors == []
    assert len(rows) == 3

    sol = next(r for r in rows if r["name_key"] == "sol ring")
    assert sol["quantity"] == 2
    assert sol["foil"] == 0
    assert sol["scryfall_id"] == "abc-123"
    assert sol["set_code"] == "LTC"

    bolt = next(r for r in rows if r["name_key"] == "lightning bolt")
    assert bolt["foil"] == 1
    assert bolt["condition"] == "lightly_played"


def test_name_with_comma_is_preserved():
    rows, _ = manabox.parse_manabox_csv(SAMPLE)
    atraxa = next(r for r in rows if r["name_key"].startswith("atraxa"))
    assert atraxa["raw_name"].startswith("Atraxa")


def test_binder_type_defaults_to_empty_without_column():
    rows, _ = manabox.parse_manabox_csv(SAMPLE)
    assert all(r["binder_type"] == "" for r in rows)


def test_binder_type_is_parsed_and_lowercased():
    csv_text = (
        "Binder Name,Binder Type,Name,Set code,Quantity,Scryfall ID\n"
        "Mon classeur,binder,Sol Ring,LTC,2,abc-123\n"
        "Deck Krenko,Deck,Goblin Matron,DMR,1,def-456\n"
        "Wishlist,list,Lightning Bolt,2X2,1,ghi-789\n"
    )
    rows, errors = manabox.parse_manabox_csv(csv_text)
    assert errors == []
    by_key = {r["name_key"]: r for r in rows}
    assert by_key["sol ring"]["binder_type"] == "binder"
    assert by_key["goblin matron"]["binder_type"] == "deck"
    assert by_key["lightning bolt"]["binder_type"] == "list"


def test_empty_and_header_only():
    assert manabox.parse_manabox_csv("") == ([], [])
    header_only = SAMPLE.splitlines()[0] + "\n"
    rows, errors = manabox.parse_manabox_csv(header_only)
    assert rows == [] and errors == []
