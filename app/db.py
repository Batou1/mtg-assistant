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
    # Several threads write concurrently (web requests, chat turns, the bulk-data
    # refresh and its huge import transactions). WAL lets readers proceed during
    # a write, and the busy timeout waits out a competing writer instead of
    # failing immediately with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
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
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                artifacts_json TEXT,
                created_at REAL NOT NULL
            );
            """
        )
        _migrate_collection(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_collection_name ON collection(name_key)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_collection_profile ON collection(profile_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_profile ON conversations(profile_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id)"
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


def get_cards(name_keys, ttl_days: float | None = None) -> dict[str, dict]:
    """Fetch many cached cards in one connection: {name_key: card_dict}.

    Stale or missing keys are simply absent from the result. Chunked to stay
    under SQLite's bound-parameter limit. One page render used to call
    ``get_card`` once per card — thousands of connections; this replaces them
    with a handful of IN queries.
    """
    ttl = settings.cache_ttl_days if ttl_days is None else ttl_days
    keys = list(name_keys)
    out: dict[str, dict] = {}
    if not keys:
        return out
    with get_conn() as conn:
        for i in range(0, len(keys), 900):
            chunk = keys[i : i + 900]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT name_key, data, fetched_at FROM cards WHERE name_key IN ({placeholders})",
                chunk,
            ).fetchall()
            for r in rows:
                if _fresh(r["fetched_at"], ttl):
                    out[r["name_key"]] = json.loads(r["data"])
    return out


def set_card(name_key: str, data: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cards (name_key, data, fetched_at) VALUES (?, ?, ?)",
            (name_key, json.dumps(data), time.time()),
        )


def bulk_set_cards(items) -> int:
    """Upsert many (name_key, card_dict) pairs in one transaction.

    Used by the Scryfall bulk-data import, which writes hundreds of thousands
    of rows — one connection/commit per row (like ``set_card``) would take far
    too long.
    """
    now = time.time()
    rows = [(name_key, json.dumps(data), now) for name_key, data in items]
    if not rows:
        return 0
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO cards (name_key, data, fetched_at) VALUES (?, ?, ?)",
            rows,
        )
    return len(rows)


def all_oracle_cards() -> list[dict]:
    """Every by-name cached card (the oracle_cards bulk import) — excludes the
    by-id printing entries (``id:<uuid>``). Used by ``app.cardsearch`` to scan
    the whole card pool for type/keyword/oracle-text criteria.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT data FROM cards WHERE name_key NOT LIKE 'id:%'"
        ).fetchall()
    return [json.loads(r["data"]) for r in rows]


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
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM conversations WHERE profile_id=?", (pid,)
            ).fetchall()
        ]
        for cid in ids:
            conn.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        conn.execute("DELETE FROM conversations WHERE profile_id=?", (pid,))
        conn.execute("DELETE FROM meta WHERE key=?", (f"collection_stats:{pid}",))
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
        # The home page caches per-profile stats (value, colors) in meta.
        conn.execute("DELETE FROM meta WHERE key=?", (f"collection_stats:{pid}",))
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


def set_meta(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def delete_meta_prefix(prefix: str) -> None:
    """Drop every meta row whose key starts with ``prefix`` (cache invalidation)."""
    with get_conn() as conn:
        escaped = prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        conn.execute("DELETE FROM meta WHERE key LIKE ? ESCAPE '\\'", (escaped + "%",))


# --- Chat: conversations + messages (per profile) ------------------------

def create_conversation(profile_id: int, title: str = "") -> int:
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (profile_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (int(profile_id), (title or "").strip()[:80], now, now),
        )
        return cur.lastrowid


def list_conversations(profile_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE profile_id=? ORDER BY updated_at DESC",
            (int(profile_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conversation_id) -> dict | None:
    try:
        cid = int(conversation_id)
    except (TypeError, ValueError):
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, profile_id, title, created_at, updated_at "
            "FROM conversations WHERE id=?",
            (cid,),
        ).fetchone()
    return dict(row) if row else None


def delete_conversation(conversation_id: int) -> None:
    cid = int(conversation_id)
    with get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        conn.execute("DELETE FROM conversations WHERE id=?", (cid,))


def add_message(conversation_id: int, role: str, content: str, artifacts=None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, artifacts_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                int(conversation_id),
                role,
                content or "",
                json.dumps(artifacts) if artifacts else None,
                time.time(),
            ),
        )
        return cur.lastrowid


def _backfill_artifact(obj):
    """Old persisted artifacts (from before a field existed) can be missing
    keys the current templates expect. `budget_eur` and `max_card_price_eur`
    are always set together by intent/buylist/deck builders, so any dict
    that has one but not the other predates the cap feature; fill it with
    None so templates' `is not none` guards work as intended.
    """
    if isinstance(obj, dict):
        if "budget_eur" in obj and "max_card_price_eur" not in obj:
            obj["max_card_price_eur"] = None
        for v in obj.values():
            _backfill_artifact(v)
    elif isinstance(obj, list):
        for v in obj:
            _backfill_artifact(v)
    return obj


def get_messages(conversation_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content, artifacts_json, created_at FROM messages "
            "WHERE conversation_id=? ORDER BY id",
            (int(conversation_id),),
        ).fetchall()
    out = []
    for r in rows:
        artifacts = json.loads(r["artifacts_json"]) if r["artifacts_json"] else []
        for art in artifacts:
            _backfill_artifact(art)
        out.append(
            {
                "role": r["role"],
                "content": r["content"],
                "artifacts": artifacts,
                "created_at": r["created_at"],
            }
        )
    return out


def touch_conversation(conversation_id: int, title: str | None = None) -> None:
    """Bump updated_at; set the title only if it's still empty."""
    cid = int(conversation_id)
    with get_conn() as conn:
        if title:
            conn.execute(
                "UPDATE conversations SET updated_at=?, "
                "title=CASE WHEN title='' THEN ? ELSE title END WHERE id=?",
                (time.time(), title.strip()[:80], cid),
            )
        else:
            conn.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (time.time(), cid)
            )
