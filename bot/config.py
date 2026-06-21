"""
bot/config.py — Application settings via pydantic-settings.

All settings are read from environment variables (or .env file).
The CITY_MAP is the single source of truth for krisha.kz city slugs.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# City slug map — authoritative (live-verified HTTP 200, June 2026)
# Keys are the PATH SEGMENT used in krisha.kz URLs:
#   https://krisha.kz/prodazha/kvartiry/{city-slug}/
# Values are human-readable Russian/Kazakh city names for bot display.
# ---------------------------------------------------------------------------
CITY_MAP: dict[str, str] = {
    "almaty": "Алматы",
    "astana": "Астана",
    "shymkent": "Шымкент",
    "karaganda": "Қарағанды",  # NOT karagandy
    "aktobe": "Актобе",
    "atyrau": "Атырау",
    "pavlodar": "Павлодар",
    "ust-kamenogorsk": "Усть-Каменогорск",
    "semej": "Семей",  # NOT semey
    "taraz": "Тараз",
    "kostanay": "Қостанай",
}


class Settings(BaseSettings):
    """
    Central settings object. Loaded once at import time via get_settings().

    Required environment variables (must be set in .env or Railway env):
      BOT_TOKEN             — Telegram bot token from @BotFather
      SUPABASE_URL          — Supabase project URL (https://<ref>.supabase.co)
      SUPABASE_SERVICE_KEY  — service_role key (server-side only, NEVER expose client-side)

    Optional (have defaults):
      POLL_INTERVAL_SECONDS  — how often the poller loops (default 300 = 5 min)
      REQUEST_TIMEOUT        — httpx request timeout in seconds (default 20)
      KRISHA_BASE_URL        — base URL for krisha.kz (default https://krisha.kz)
      SEEN_LISTINGS_TTL_DAYS — days before seen_listings rows are eligible for cleanup (default 30)
      REDIS_URL              — if set, used for optional FSM/cache storage
      WEBHOOK_URL            — if set, bot runs in webhook mode; else polling
      PORT                   — aiohttp webhook server port (default 8080)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Required ---
    BOT_TOKEN: str
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # --- Optional with defaults ---
    POLL_INTERVAL_SECONDS: int = 300
    REQUEST_TIMEOUT: int = 20
    KRISHA_BASE_URL: str = "https://krisha.kz"
    SEEN_LISTINGS_TTL_DAYS: int = 30
    REDIS_URL: str | None = None
    WEBHOOK_URL: str | None = None
    PORT: int = 8080


_settings: Settings | None = None


def get_settings() -> Settings:
    """
    Return the singleton Settings instance. Lazily initialised on first call.
    Thread-safe enough for asyncio — called once at startup before the event
    loop is running.
    """
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
