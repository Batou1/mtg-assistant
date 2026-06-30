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
    min_decks: int = int(os.environ.get("MTG_MIN_DECKS", "300"))

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

    # --- External services -------------------------------------------------
    scryfall_api: str = os.environ.get("MTG_SCRYFALL_API", "https://api.scryfall.com")
    edhrec_json: str = os.environ.get(
        "MTG_EDHREC_JSON", "https://json.edhrec.com/pages/commanders"
    )
    # EDHREC per-card pages list the commanders that most play a card — the
    # source for discovering commanders you don't own but that fit your cards.
    edhrec_cards_json: str = os.environ.get(
        "MTG_EDHREC_CARDS_JSON", "https://json.edhrec.com/pages/cards"
    )
    request_delay: float = float(os.environ.get("MTG_REQUEST_DELAY", "0.1"))
    error_retry_cooldown: float = float(os.environ.get("MTG_ERROR_RETRY_COOLDOWN", "6"))
    error_retry_passes: int = int(os.environ.get("MTG_ERROR_RETRY_PASSES", "2"))
    user_agent: str = os.environ.get(
        "MTG_USER_AGENT", "mtg-assistant/0.1 (local personal tool)"
    )

    # --- LLM (Anthropic / Claude) ------------------------------------------
    # The API key is read by the SDK from ANTHROPIC_API_KEY (kept in .env).
    anthropic_model: str = os.environ.get("MTG_ANTHROPIC_MODEL", "claude-sonnet-5")
    # Sonnet 5's tokenizer emits ~30% more tokens per unit of text than 4.6, so
    # the output budget is bumped ~30% to preserve the same text headroom.
    anthropic_max_tokens: int = int(os.environ.get("MTG_ANTHROPIC_MAX_TOKENS", "2700"))

    # --- Iterative chat (Phase 3) ------------------------------------------
    # How many tool-call rounds a single chat turn may take before stopping.
    chat_max_tool_iterations: int = int(os.environ.get("MTG_CHAT_MAX_TOOL_ITERS", "6"))
    # How many past messages to replay to the API (bounds tokens/cost).
    chat_history_limit: int = int(os.environ.get("MTG_CHAT_HISTORY", "16"))

    # --- Deck generation ---------------------------------------------------
    deck_size: int = int(os.environ.get("MTG_DECK_SIZE", "100"))
    deck_lands: int = int(os.environ.get("MTG_DECK_LANDS", "36"))
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
