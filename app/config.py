"""Runtime configuration, overridable via environment variables."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # --- Storage -----------------------------------------------------------
    db_path: str = os.environ.get("MTG_DB_PATH", "data/app.db")
    cache_ttl_days: int = int(os.environ.get("MTG_CACHE_TTL_DAYS", "7"))
    # Prices move faster than card text, so they get a shorter freshness window.
    price_ttl_days: float = float(os.environ.get("MTG_PRICE_TTL_DAYS", "1"))

    # --- Commander analysis ------------------------------------------------
    # Minimum number of EDHREC decks a commander needs to be considered.
    min_decks: int = int(os.environ.get("MTG_MIN_DECKS", "1"))

    # --- Commander discovery (propose commanders you don't own) ------------
    # Also surface commanders absent from the collection but linked to cards you
    # own (via EDHREC card pages), matching the requested theme/colours/budget.
    discovery_enabled: bool = os.environ.get("MTG_DISCOVERY", "1").lower() not in (
        "0", "false", "no", "off", ""
    )
    # How many owned cards to query for "which commanders play this" (bounds I/O).
    discovery_card_limit: int = int(os.environ.get("MTG_DISCOVERY_CARD_LIMIT", "40"))
    # How many discovered candidate commanders to resolve + colour-filter.
    discovery_pool: int = int(os.environ.get("MTG_DISCOVERY_POOL", "20"))
    # How many proposed (unowned) commanders to fully score + display.
    discovery_limit: int = int(os.environ.get("MTG_DISCOVERY_LIMIT", "4"))

    # --- Commander finder (theme-first, collection-independent) -------------
    # Search commanders for a requested theme on EDHREC's theme/typal pages and
    # in the local Scryfall pool, without deriving candidates from the
    # collection (see analysis.find_commanders).
    # How many theme-page slugs to probe per request (bounds I/O).
    finder_theme_limit: int = int(os.environ.get("MTG_FINDER_THEME_LIMIT", "4"))
    # How many candidate commanders to resolve + colour/legality-filter.
    finder_pool: int = int(os.environ.get("MTG_FINDER_POOL", "30"))
    # How many found commanders to fully score + display.
    finder_limit: int = int(os.environ.get("MTG_FINDER_LIMIT", "8"))
    # How many viable candidates to score before ranking + cutting to
    # finder_limit. Scanning deeper than the display limit is what lets an
    # older, better-fitting commander outrank the newest hyped ones that top
    # EDHREC's lists.
    finder_scan: int = int(os.environ.get("MTG_FINDER_SCAN", "16"))
    # A non-reprint card younger than this is "recent" (new-set hype):
    # suggestion rankings demote it below equally-fitting older commanders.
    recent_set_months: float = float(os.environ.get("MTG_RECENT_SET_MONTHS", "18"))

    # --- External services -------------------------------------------------
    scryfall_api: str = os.environ.get("MTG_SCRYFALL_API", "https://api.scryfall.com")
    # Card data comes from Scryfall's bulk-data exports (see app/bulk_data.py),
    # refreshed automatically; the live API is only used for cache misses.
    bulk_auto_refresh: bool = os.environ.get("MTG_BULK_AUTO_REFRESH", "1").lower() not in (
        "0", "false", "no", "off", ""
    )
    bulk_refresh_hours: float = float(os.environ.get("MTG_BULK_REFRESH_HOURS", "24"))
    edhrec_json: str = os.environ.get(
        "MTG_EDHREC_JSON", "https://json.edhrec.com/pages/commanders"
    )
    # EDHREC per-card pages list the commanders that most play a card — the
    # source for discovering commanders you don't own but that fit your cards.
    edhrec_cards_json: str = os.environ.get(
        "MTG_EDHREC_CARDS_JSON", "https://json.edhrec.com/pages/cards"
    )
    # EDHREC tag pages list the commanders most played for a theme — both
    # strategy themes ("aristocrats", "group-hug"…) and creature types
    # ("zombies", "dragons"…) live under pages/tags. The source for the
    # collection-independent commander finder.
    edhrec_tags_json: str = os.environ.get(
        "MTG_EDHREC_TAGS_JSON", "https://json.edhrec.com/pages/tags"
    )
    request_delay: float = float(os.environ.get("MTG_REQUEST_DELAY", "0.1"))
    # Parallel workers for the per-candidate EDHREC/Scryfall fetches during a
    # Commander analysis. Kept small on purpose: each worker still honours
    # request_delay after every call, and EDHREC's backoff handles 429s.
    fetch_workers: int = int(os.environ.get("MTG_FETCH_WORKERS", "4"))
    error_retry_cooldown: float = float(os.environ.get("MTG_ERROR_RETRY_COOLDOWN", "6"))
    error_retry_passes: int = int(os.environ.get("MTG_ERROR_RETRY_PASSES", "2"))
    user_agent: str = os.environ.get(
        "MTG_USER_AGENT", "mtg-assistant/0.1 (local personal tool)"
    )

    # --- LLM (Anthropic / Claude) ------------------------------------------
    # The API key is read by the SDK from ANTHROPIC_API_KEY (kept in .env).
    anthropic_model: str = os.environ.get("MTG_ANTHROPIC_MODEL", "claude-sonnet-5")
    # Sonnet 5 runs adaptive thinking by default and thinking tokens count
    # against max_tokens, so the budget must leave room for BOTH the (invisible)
    # reasoning and the actual text. 2700 proved too tight: on hard prompts the
    # whole budget went to thinking and the response was truncated before any
    # text (stop_reason=max_tokens, empty content), which callers see as a
    # failed/None LLM call.
    anthropic_max_tokens: int = int(os.environ.get("MTG_ANTHROPIC_MAX_TOKENS", "8000"))
    # Deck-proposal calls (pool_deck for a 100-card singleton list, 60-card
    # archetypes) must emit up to ~90 JSON entries on top of the thinking spend.
    anthropic_deck_max_tokens: int = int(os.environ.get("MTG_ANTHROPIC_DECK_MAX_TOKENS", "16000"))

    # --- Iterative chat (Phase 3) ------------------------------------------
    # How many tool-call rounds a single chat turn may take before stopping.
    chat_max_tool_iterations: int = int(os.environ.get("MTG_CHAT_MAX_TOOL_ITERS", "6"))
    # How many past messages to replay to the API (bounds tokens/cost).
    chat_history_limit: int = int(os.environ.get("MTG_CHAT_HISTORY", "16"))

    # --- Deck generation ---------------------------------------------------
    deck_size: int = int(os.environ.get("MTG_DECK_SIZE", "100"))
    deck_lands: int = int(os.environ.get("MTG_DECK_LANDS", "36"))
    # Minimum basic lands in a generated Commander manabase. EDHREC land
    # sections list far more nonbasics than a casual deck should run, so
    # without a floor the generator fills every land slot with nonbasics.
    deck_min_basics: int = int(os.environ.get("MTG_DECK_MIN_BASICS", "18"))
    # Relevant alternatives proposed alongside the deck (next-best EDHREC cards
    # not in the main list). Target 15-20.
    deck_sideboard: int = int(os.environ.get("MTG_DECK_SIDEBOARD", "18"))

    # --- Limited / pool deckbuilding (draft & sealed) ----------------------
    # A Limited deck is 40 cards with ~17 lands; basic lands are added freely
    # (they are not part of the opened pool).
    limited_deck_size: int = int(os.environ.get("MTG_LIMITED_DECK_SIZE", "40"))
    limited_lands: int = int(os.environ.get("MTG_LIMITED_LANDS", "17"))

    # --- Pool-deck collection bonus (synergy add-ons beyond the deck) -------
    # When building a constructed/Commander deck from a provided list, also
    # recommend extra cards: synergistic cards you already own + a few to buy.
    bonus_owned_scan: int = int(os.environ.get("MTG_BONUS_OWNED_SCAN", "200"))
    bonus_owned_max: int = int(os.environ.get("MTG_BONUS_OWNED_MAX", "12"))
    bonus_buy_max: int = int(os.environ.get("MTG_BONUS_BUY_MAX", "10"))

    # --- Web research (Phase 2b; Brave Search complements curated sources) --
    brave_api_key: str = os.environ.get("MTG_BRAVE_API_KEY", "")

    # --- Pricing / budget --------------------------------------------------
    # Cardmarket (EUR) is the relevant market for buying from France.
    currency: str = os.environ.get("MTG_CURRENCY", "EUR")


settings = Settings()
