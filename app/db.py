"""SQLite persistence: profiles + per-profile collections, plus shared
Scryfall/EDHREC response caches.

Each profile owns its own collection (one row per printing/finish: quantity,
set, foil, condition, Scryfall id). The card and EDHREC caches are global —
they're just upstream-API responses and are shared across profiles.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from .config import settings

DEFAULT_PROFILE_NAME = "Par défaut"

_CREATE_COLLECTION_SQL = """
CREATE TABLE collection (
    profile_id  INTEGER NOT NULL,
    scryfall_id TEXT,
    name_key    TEXT NOT NULL,
    raw_name    TEXT NOT NULL,
    set_code    TEXT,
    foil        INTEGER NOT NULL DEFAULT 0,
    condition   TEXT,
    quantity    INTEGER NOT NULL,
    PRIMARY KEY (profile_id, name_key, set_code, foil, condition)
)
"""


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


def _table_columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


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
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                collection_source TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        _migrate_collection(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_collection_name ON collection(name_key)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_collection_profile ON collection(profile_id)"
        )


def _migrate_collection(conn) -> None:
    """Create the collection table, migrating a pre-profiles schema if present."""
    cols = _table_columns(conn, "collection")
    if not cols:
        conn.execute(_CREATE_COLLECTION_SQL)
        return
    if "profile_id" in cols:
        return

    # Old single-collection schema: move its rows under a default profile.
    default_id = _ensure_default_profile(conn)
    conn.execute("ALTER TABLE collection RENAME TO collection_old")
    conn.execute(_CREATE_COLLECTION_SQL)
    conn.execute(
        """INSERT INTO collection
           (profile_id, scryfall_id, name_key, raw_name, set_code, foil, condition, quantity)
           SELECT ?, scryfall_id, name_key, raw_name, set_code, foil, condition, quantity
           FROM collection_old""",
        (default_id,),
    )
    src = conn.execute("SELECT value FROM meta WHERE key='collection_source'").fetchone()
    if src and src["value"]:
        conn.execute(
            "UPDATE profiles SET collection_source=? WHERE id=?", (src["value"], default_id)
        )
    conn.execute("DROP TABLE collection_old")


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


# --- Profiles ------------------------------------------------------------

def _ensure_default_profile(conn) -> int:
    """Return some profile id, creating the default one if none exist."""
    row = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO profiles (name, created_at) VALUES (?, ?)",
        (DEFAULT_PROFILE_NAME, time.time()),
    )
    return cur.lastrowid


def ensure_default_profile() -> int:
    with get_conn() as conn:
        return _ensure_default_profile(conn)


def list_profiles() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, collection_source FROM profiles ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


def get_profile(profile_id) -> dict | None:
    try:
        pid = int(profile_id)
    except (TypeError, ValueError):
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name, collection_source FROM profiles WHERE id=?", (pid,)
        ).fetchone()
    return dict(row) if row else None


def create_profile(name: str) -> int:
    """Create a profile, returning its id. If the name exists, return that id."""
    name = (name or "").strip() or "Sans nom"
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM profiles WHERE name=?", (name,)).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO profiles (name, created_at) VALUES (?, ?)", (name, time.time())
        )
        return cur.lastrowid


def rename_profile(profile_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        return
    with get_conn() as conn:
        conn.execute("UPDATE profiles SET name=? WHERE id=?", (name, int(profile_id)))


def delete_profile(profile_id: int) -> None:
    """Delete a profile and its collection. Recreates a default if none remain."""
    pid = int(profile_id)
    with get_conn() as conn:
        conn.execute("DELETE FROM collection WHERE profile_id=?", (pid,))
        conn.execute("DELETE FROM profiles WHERE id=?", (pid,))
        _ensure_default_profile(conn)


# --- Collection (per profile) -------------------------------------------

def replace_collection(profile_id: int, rows, source: str | None = None) -> None:
    """Replace ``profile_id``'s collection with ``rows``.

    Each row: scryfall_id, name_key, raw_name, set_code, foil, condition,
    quantity. Identical (name_key, set_code, foil, condition) rows are summed.
    """
    pid = int(profile_id)
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
            if not merged[key]["scryfall_id"] and r.get("scryfall_id"):
                merged[key]["scryfall_id"] = r["scryfall_id"]
        else:
            merged[key] = {
                "profile_id": pid,
                "scryfall_id": r.get("scryfall_id") or "",
                "name_key": r["name_key"],
                "raw_name": r["raw_name"],
                "set_code": r.get("set_code") or "",
                "foil": int(bool(r.get("foil"))),
                "condition": r.get("condition") or "",
                "quantity": int(r["quantity"]),
            }
    with get_conn() as conn:
        conn.execute("DELETE FROM collection WHERE profile_id=?", (pid,))
        for r in merged.values():
            conn.execute(
                """INSERT OR REPLACE INTO collection
                   (profile_id, scryfall_id, name_key, raw_name, set_code, foil, condition, quantity)
                   VALUES (:profile_id, :scryfall_id, :name_key, :raw_name, :set_code, :foil, :condition, :quantity)""",
                r,
            )
        if source is not None:
            conn.execute(
                "UPDATE profiles SET collection_source=? WHERE id=?", (source, pid)
            )


def collection_names(profile_id: int):
    """Distinct owned card names with total quantity for a profile."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT raw_name, name_key, SUM(quantity) AS qty
               FROM collection WHERE profile_id=?
               GROUP BY name_key ORDER BY raw_name""",
            (int(profile_id),),
        ).fetchall()
    return [(r["raw_name"], r["name_key"], r["qty"]) for r in rows]


def collection_scryfall_ids(profile_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT scryfall_id FROM collection WHERE profile_id=? AND scryfall_id != ''",
            (int(profile_id),),
        ).fetchall()
    return [r["scryfall_id"] for r in rows]


def collection_count(profile_id: int):
    """(distinct_cards, total_cards) for a profile."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(DISTINCT name_key) AS distinct_cards,
                      COALESCE(SUM(quantity),0) AS total
               FROM collection WHERE profile_id=?""",
            (int(profile_id),),
        ).fetchone()
    return row["distinct_cards"], row["total"]


def owned_name_keys(profile_id: int) -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT name_key FROM collection WHERE profile_id=?",
            (int(profile_id),),
        ).fetchall()
    return {r["name_key"] for r in rows}


def get_meta(key: str):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None
