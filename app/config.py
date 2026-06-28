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

    # --- External services -------------------------------------------------
    scryfall_api: str = os.environ.get("MTG_SCRYFALL_API", "https://api.scryfall.com")
    edhrec_json: str = os.environ.get(
        "MTG_EDHREC_JSON", "https://json.edhrec.com/pages/commanders"
    )
    request_delay: float = float(os.environ.get("MTG_REQUEST_DELAY", "0.1"))
    error_retry_cooldown: float = float(os.environ.get("MTG_ERROR_RETRY_COOLDOWN", "6"))
    error_retry_passes: int = int(os.environ.get("MTG_ERROR_RETRY_PASSES", "2"))
    user_agent: str = os.environ.get(
        "MTG_USER_AGENT", "mtg-assistant/0.1 (local personal tool)"
    )

    # --- Local LLM (Ollama) ------------------------------------------------
    ollama_url: str = os.environ.get("MTG_OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.environ.get("MTG_OLLAMA_MODEL", "qwen2.5:7b-instruct")
    ollama_timeout: float = float(os.environ.get("MTG_OLLAMA_TIMEOUT", "60"))

    # --- Deck generation ---------------------------------------------------
    deck_size: int = int(os.environ.get("MTG_DECK_SIZE", "100"))
    deck_lands: int = int(os.environ.get("MTG_DECK_LANDS", "36"))

    # --- Web research (Phase 2b; Brave Search complements curated sources) --
    brave_api_key: str = os.environ.get("MTG_BRAVE_API_KEY", "")

    # --- Pricing / budget --------------------------------------------------
    # Cardmarket (EUR) is the relevant market for buying from France.
    currency: str = os.environ.get("MTG_CURRENCY", "EUR")


settings = Settings()
