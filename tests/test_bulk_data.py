import importlib
import time

import pytest


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """app.bulk_data + app.db pointed at a throwaway SQLite file."""
    monkeypatch.setenv("MTG_DB_PATH", str(tmp_path / "t.db"))
    import app.config as config
    importlib.reload(config)
    import app.db as db
    importlib.reload(db)
    import app.bulk_data as bulk_data
    importlib.reload(bulk_data)
    db.init_db()
    return bulk_data, db


SOL_RING = {"id": "abc-1", "name": "Sol Ring", "oracle_id": "o-1"}
DFC = {
    "id": "abc-2",
    "name": "Delver of Secrets // Insectile Aberration",
    "oracle_id": "o-2",
}


def test_import_oracle_cards_registers_name_key(fresh):
    bulk_data, db = fresh
    bulk_data._import_oracle_cards([SOL_RING])
    assert db.get_card("sol ring")["id"] == "abc-1"


def test_import_oracle_cards_registers_dfc_front_face(fresh):
    bulk_data, db = fresh
    bulk_data._import_oracle_cards([DFC])
    assert db.get_card("delver of secrets // insectile aberration")["id"] == "abc-2"
    assert db.get_card("delver of secrets")["id"] == "abc-2"


def test_import_oracle_cards_skips_non_game_layouts(fresh):
    """Regression: an Art Series / token / emblem object can reuse a real
    card's display name (e.g. an Art Series "Sol Ring // Sol Ring") without
    being that card, and must not clobber the real card's name_key entry."""
    bulk_data, db = fresh
    art_series_sol_ring = {
        "id": "art-1",
        "name": "Sol Ring // Sol Ring",
        "layout": "art_series",
    }
    bulk_data._import_oracle_cards([SOL_RING, art_series_sol_ring])
    assert db.get_card("sol ring")["id"] == "abc-1"


def test_import_oracle_cards_skips_digital_only_printings(fresh):
    """Regression: Scryfall's canonical pick for a card is sometimes its
    MTGO-only printing (Vintage Masters, Masters Edition…), which carries no
    Cardmarket price. Caching that under the card's name made expensive
    staples look free. Leaving the name unregistered sends the lookup to the
    live API, where scryfall._ensure_paper finds a real paper printing."""
    bulk_data, db = fresh
    mtgo_workshop = {
        "id": "mtgo-1", "name": "Mishra's Workshop", "games": ["mtgo"],
        "digital": True,
    }
    bulk_data._import_oracle_cards([SOL_RING, mtgo_workshop])
    assert db.get_card("sol ring")["id"] == "abc-1"
    assert db.get_card("mishra's workshop") is None


def test_import_all_cards_registers_id_only(fresh):
    bulk_data, db = fresh
    bulk_data._import_all_cards([SOL_RING])
    assert db.get_card("id:abc-1")["name"] == "Sol Ring"
    # Unlike the oracle_cards import, this must not touch the name_key cache —
    # all_cards has many printings per name and isn't the canonical pick.
    assert db.get_card("sol ring") is None


def test_updated_at_from_filename_parses_scryfall_convention(fresh):
    bulk_data, _db = fresh
    stamp = bulk_data._updated_at_from_filename("all-cards-20260701092749.jsonl")
    assert stamp == "2026-07-01T09:27:49+00:00"


def test_updated_at_from_filename_rejects_unrecognized_name(fresh):
    bulk_data, _db = fresh
    assert bulk_data._updated_at_from_filename("cards.jsonl") is None


def test_needs_refresh_when_never_synced(fresh):
    bulk_data, _db = fresh
    assert bulk_data.needs_refresh() is True


def test_needs_refresh_false_right_after_sync(fresh):
    bulk_data, db = fresh
    now = str(time.time())
    for bulk_type in bulk_data.BULK_TYPES:
        db.set_meta(f"bulk_{bulk_type}_synced_at", now)
    assert bulk_data.needs_refresh() is False


def test_needs_refresh_true_once_stale(fresh):
    bulk_data, db = fresh
    # Default bulk_refresh_hours is 24; 25h old is stale regardless of env.
    stale = str(time.time() - 25 * 3600)
    for bulk_type in bulk_data.BULK_TYPES:
        db.set_meta(f"bulk_{bulk_type}_synced_at", stale)
    assert bulk_data.needs_refresh() is True


def test_same_version_ignores_millisecond_precision(fresh):
    """A local-file bootstrap timestamp (no milliseconds) must be recognized
    as matching the API's ``updated_at`` (which has milliseconds) — otherwise
    every refresh would think the version differs and redownload the ~2.5 GB
    all_cards export for no reason."""
    bulk_data, _db = fresh
    assert bulk_data._same_version(
        "2026-07-01T09:27:49+00:00", "2026-07-01T09:27:49.936+00:00"
    )
    assert not bulk_data._same_version(
        "2026-07-01T09:27:49+00:00", "2026-07-02T09:27:49.936+00:00"
    )


def test_import_local_all_cards_file_sets_meta(fresh, tmp_path):
    bulk_data, db = fresh
    path = tmp_path / "all-cards-20260701092749.jsonl"
    path.write_text('{"id": "abc-1", "name": "Sol Ring", "oracle_id": "o-1"}\n')
    count = bulk_data.import_local_all_cards_file(str(path))
    assert count == 1
    assert db.get_card("id:abc-1") is not None
    assert db.get_meta("bulk_all_cards_updated_at") == "2026-07-01T09:27:49+00:00"
    assert db.get_meta("bulk_all_cards_synced_at") is not None
