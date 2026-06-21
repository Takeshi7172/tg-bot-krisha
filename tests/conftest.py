"""
tests/conftest.py — Shared pytest fixtures for tg-bot-krisha test suite.

Key fixtures
------------
- mock_settings      : patches get_settings() so no real .env is needed
- mock_supabase      : full mock of get_supabase_client() + chained builder
- owner_listing      : a private-owner Listing (is_owner=True)
- complex_listing    : a developer Listing (is_owner=False)
- minimal_listing    : Listing with only required fields (no optional fields)
- mock_bot           : a bare AsyncMock aiogram Bot
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Environment stubs — must be set BEFORE any bot.* imports so pydantic-settings
# does not raise a ValidationError for missing BOT_TOKEN / SUPABASE_URL / etc.
# ---------------------------------------------------------------------------
os.environ.setdefault("BOT_TOKEN", "123456:AAFake_test_token_for_pytest")
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake_service_key")

# ---------------------------------------------------------------------------
# Reset the Settings singleton between test modules to pick up env changes
# ---------------------------------------------------------------------------
import bot.config as _config_module  # noqa: E402


@pytest.fixture(autouse=True)
def reset_settings_singleton():
    """Reset the cached Settings singleton before each test."""
    original = _config_module._settings
    _config_module._settings = None
    yield
    _config_module._settings = original


# ---------------------------------------------------------------------------
# Listing fixtures
# ---------------------------------------------------------------------------

from bot.scraper.models import Listing  # noqa: E402


@pytest.fixture
def owner_listing() -> Listing:
    """A fully populated private-owner Listing."""
    return Listing(
        id="1011171116",
        title="1-комнатная квартира · 46 м² · 8/9 этаж",
        price=29_000_000,
        rooms=1,
        area=46.0,
        address="Алматы, Турксибский р-н, мкр Кайрат 153/59",
        url="https://krisha.kz/a/show/1011171116",
        image_url="https://krisha-photos.kcdn.online/webp/4c/4c1d5fd7/71-full.jpg",
        published_at=datetime(2026, 4, 9, tzinfo=timezone.utc),
        city_id="almaty",
        is_owner=True,
    )


@pytest.fixture
def complex_listing() -> Listing:
    """A developer/ЖК Listing (is_owner=False)."""
    return Listing(
        id="1009994655",
        title="2-комнатная квартира · 65 м² · 5/12 этаж",
        price=45_000_000,
        rooms=2,
        area=65.0,
        address="Алматы, Бостандыкский р-н, ЖК Arena City",
        url="https://krisha.kz/a/show/1009994655",
        image_url="https://krisha-photos.kcdn.online/webp/ab/abcd1234/55-full.jpg",
        published_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
        city_id="almaty",
        is_owner=False,
    )


@pytest.fixture
def minimal_listing() -> Listing:
    """A Listing with only required fields — simulates partial scrape fallback."""
    return Listing(
        id="9999999999",
        url="https://krisha.kz/a/show/9999999999",
        city_id="astana",
    )


# ---------------------------------------------------------------------------
# Supabase mock — returns a chainable builder mock so repository code can do:
#   client.table("x").select("y").eq("k","v").limit(1).execute()
# ---------------------------------------------------------------------------


def _make_supabase_mock(table_responses: dict | None = None) -> MagicMock:
    """
    Build a mock Supabase AsyncClient.

    The supabase-py builder pattern uses synchronous chaining:
      client.table("x").select("y").eq("k","v").limit(1).execute()
    Only .execute() is async (awaitable). All other methods return self (sync).

    Parameters
    ----------
    table_responses : dict
        Maps table_name → list of row dicts returned by .execute().
        If not provided, execute() returns data=[].
    """
    table_responses = table_responses or {}

    client = MagicMock()

    def _table(name: str):
        resp_data = table_responses.get(name, [])
        execute_result = MagicMock()
        execute_result.data = resp_data

        # Use MagicMock for the builder (sync chaining)
        builder = MagicMock()
        # All chaining methods return the same builder instance
        for method in ("select", "eq", "neq", "limit", "order", "update",
                       "upsert", "insert", "delete", "filter"):
            getattr(builder, method).return_value = builder
        # Only execute is async
        builder.execute = AsyncMock(return_value=execute_result)
        return builder

    client.table = MagicMock(side_effect=_table)
    return client


@pytest.fixture
def mock_supabase() -> AsyncMock:
    """Mock supabase client with empty responses by default."""
    return _make_supabase_mock()


@pytest.fixture
def mock_bot() -> AsyncMock:
    """A bare AsyncMock that stands in for aiogram.Bot."""
    bot = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock()
    return bot
