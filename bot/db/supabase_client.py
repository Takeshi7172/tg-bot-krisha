"""
bot/db/supabase_client.py — Async supabase-py v2 client factory.

The client is created once (singleton) and reused across the bot lifetime.
All DB operations use the service_role key — the bot never uses the anon key.

Connection note (from docs/schema.md):
  - Use the TRANSACTION-MODE POOLER connection string (port 6543).
  - Direct Supabase connection is IPv6-only — unreachable from Railway.
  - SUPABASE_URL stays as the project REST URL (https://<ref>.supabase.co);
    the pooler is only relevant for PostgreSQL-level connections (asyncpg).
    supabase-py uses the REST/PostgREST API over HTTPS — no raw pgbouncer needed.

supabase-py v2 async client:
  from supabase._async.client import AsyncClient, create_client as create_async_client
  client = await create_async_client(url, key)
"""

from __future__ import annotations

import logging

from supabase._async.client import AsyncClient
from supabase._async.client import create_client as _create_async_client

from bot.config import get_settings

logger = logging.getLogger(__name__)

_client: AsyncClient | None = None


async def get_supabase_client() -> AsyncClient:
    """
    Return the singleton async Supabase client. Created on first call.

    The client authenticates with the service_role key and has full access
    to all tables (RLS bypass). This key must NEVER be exposed client-side.

    Usage
    -----
    client = await get_supabase_client()
    result = await client.table("users").select("*").execute()
    """
    global _client
    if _client is None:
        settings = get_settings()
        logger.info(
            "supabase_client: creating async client for %s", settings.SUPABASE_URL
        )
        _client = await _create_async_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY,
        )
    return _client


async def close_supabase_client() -> None:
    """
    Close the client and reset the singleton. Call during application shutdown.
    """
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("supabase_client: error closing client: %s", exc)
        finally:
            _client = None
