"""SQLite persistence: collection storage plus Scryfall/EDHREC response caches.

The collection model is richer than a plain decklist: ManaBox exports carry a
Scryfall id, set, foil flag and condition per row, all of which we keep so the
app can resolve cards precisely and (later) price specific printings.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from .config import settings


def _ensure_dir() -> None:
    directory = os.path.dirname(settings.db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)


@contextmanager
def get_conn():
    _ensure_dir()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cards (
                name_key TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                fetched_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edhrec (
                slug TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                fetched_at REAL NOT NULL
            );
            -- One row per distinct printing/finish owned. quantity is summed on
            -- import. name_key is the normalized front-face name used to match
            -- against EDHREC recommendations and decklists. The key includes the
            -- name because ManaBox rows often have no Scryfall id, so keying on
            -- id alone would collapse every such card into one row.
            CREATE TABLE IF NOT EXISTS collection (
                scryfall_id TEXT,
                name_key    TEXT NOT NULL,
                raw_name    TEXT NOT NULL,
                set_code    TEXT,
                foil        INTEGER NOT NULL DEFAULT 0,
                condition   TEXT,
                quantity    INTEGER NOT NULL,
                PRIMARY KEY (name_key, set_code, foil, condition)
            );
            CREATE INDEX IF NOT EXISTS idx_collection_name ON collection(name_key);
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )


def _fresh(fetched_at: float, ttl_days: float) -> bool:
    return (time.time() - fetched_at) < ttl_days * 86400


# --- Scryfall card cache -------------------------------------------------

def get_card(name_key: str, ttl_days: float | None = None):
    ttl = settings.cache_ttl_days if ttl_days is None else ttl_days
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data, fetched_at FROM cards WHERE name_key=?", (name_key,)
        ).fetchone()
    if row and _fresh(row["fetched_at"], ttl):
        return json.loads(row["data"])
    return None


def set_card(name_key: str, data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cards (name_key, data, fetched_at) VALUES (?, ?, ?)",
            (name_key, json.dumps(data), time.time()),
        )


# --- EDHREC cache --------------------------------------------------------

def get_edhrec(slug: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data, fetched_at FROM edhrec WHERE slug=?", (slug,)
        ).fetchone()
    if row and _fresh(row["fetched_at"], settings.cache_ttl_days):
        return json.loads(row["data"])
    return None


def set_edhrec(slug: str, data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO edhrec (slug, data, fetched_at) VALUES (?, ?, ?)",
            (slug, json.dumps(data), time.time()),
        )


# --- Collection ----------------------------------------------------------

def replace_collection(rows, source: str | None = None) -> None:
    """Replace the stored collection with ``rows``.

    Each row is a dict with keys: scryfall_id, name_key, raw_name, set_code,
    foil (bool/int), condition, quantity. Quantities for identical
    (scryfall_id, foil, condition) keys are summed.
    """
    merged: dict[tuple, dict] = {}
    for r in rows:
        key = (
            r["name_key"],
            r.get("set_code") or "",
            int(bool(r.get("foil"))),
            r.get("condition") or "",
        )
        if key in merged:
            merged[key]["quantity"] += int(r["quantity"])
            # Keep the first Scryfall id we saw for this printing.
            if not merged[key]["scryfall_id"] and r.get("scryfall_id"):
                merged[key]["scryfall_id"] = r["scryfall_id"]
        else:
            merged[key] = {
                "scryfall_id": r.get("scryfall_id") or "",
                "name_key": r["name_key"],
                "raw_name": r["raw_name"],
                "set_code": r.get("set_code") or "",
                "foil": int(bool(r.get("foil"))),
                "condition": r.get("condition") or "",
                "quantity": int(r["quantity"]),
            }
    with get_conn() as conn:
        conn.execute("DELETE FROM collection")
        for r in merged.values():
            conn.execute(
                """INSERT OR REPLACE INTO collection
                   (scryfall_id, name_key, raw_name, set_code, foil, condition, quantity)
                   VALUES (:scryfall_id, :name_key, :raw_name, :set_code, :foil, :condition, :quantity)""",
                r,
            )
        if source is not None:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('collection_source', ?)",
                (source,),
            )


def collection_names():
    """Distinct owned card names with total quantity, ordered by name."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT raw_name, name_key, SUM(quantity) AS qty
               FROM collection GROUP BY name_key ORDER BY raw_name"""
        ).fetchall()
    return [(r["raw_name"], r["name_key"], r["qty"]) for r in rows]


def collection_scryfall_ids():
    """Distinct Scryfall ids present in the collection (non-empty)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT scryfall_id FROM collection WHERE scryfall_id != ''"
        ).fetchall()
    return [r["scryfall_id"] for r in rows]


def collection_count():
    """(distinct_cards, total_cards)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT name_key) AS distinct_cards, "
            "COALESCE(SUM(quantity),0) AS total FROM collection"
        ).fetchone()
    return row["distinct_cards"], row["total"]


def owned_name_keys() -> set[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT name_key FROM collection").fetchall()
    return {r["name_key"] for r in rows}


def get_meta(key: str):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None
