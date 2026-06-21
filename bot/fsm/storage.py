"""
bot/fsm/storage.py — Persistent FSM storage backed by the `fsm_state` Supabase table.

Used when REDIS_URL is NOT configured. Implements aiogram 3's BaseStorage interface
so aiogram's Dispatcher can use it transparently.

Table schema (from docs/schema.md):
  fsm_state:
    key        text   PK  — format "fsm:{chat_id}:{user_id}"
    state      text   NULL
    data       jsonb  NOT NULL DEFAULT '{}'
    updated_at timestamptz

All operations are idempotent upserts — no separate INSERT vs UPDATE logic.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from bot.db.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)


def _key(storage_key: StorageKey) -> str:
    """
    Serialise an aiogram StorageKey to the DB primary key string.

    Format: "fsm:{chat_id}:{user_id}"

    aiogram StorageKey fields:
      - bot_id       — not included (single-bot deployment)
      - chat_id      — Telegram chat_id (bigint)
      - user_id      — Telegram user_id (bigint)
      - destiny      — not included (default destiny only)
    """
    return f"fsm:{storage_key.chat_id}:{storage_key.user_id}"


class SupabaseFSMStorage(BaseStorage):
    """
    aiogram 3 BaseStorage implementation backed by the `fsm_state` Supabase table.

    Thread/task safety: supabase-py v2 async client is safe to share across
    concurrent coroutines. Upsert operations are atomic at the DB level.

    Usage
    -----
    storage = SupabaseFSMStorage()
    dp = Dispatcher(storage=storage)
    """

    # ------------------------------------------------------------------
    # State operations
    # ------------------------------------------------------------------

    async def set_state(
        self,
        key: StorageKey,
        state: StateType = None,
    ) -> None:
        """
        Persist the FSM state for a user. Upserts the row so that existing
        data (FSM context fields) is preserved.

        Parameters
        ----------
        key   : StorageKey — identifies chat + user
        state : State | str | None — None clears the state (user is idle)
        """
        db_key = _key(key)
        state_str: str | None = None
        if state is not None:
            # aiogram passes either a State object or a string like "GroupName:state_name"
            state_str = state if isinstance(state, str) else state.state

        client = await get_supabase_client()
        try:
            # Upsert: insert if not exists, update state only if exists.
            # We use a raw upsert so existing `data` column is NOT wiped.
            # PostgREST upsert with on_conflict updates ONLY the specified columns
            # when we pass ignoreDuplicates=False (default) — but supabase-py upsert
            # replaces the whole row. To preserve `data`, we must read-merge or use
            # a two-step approach. Simplest safe approach: upsert with all columns
            # (data defaults to existing value via ON CONFLICT DO UPDATE SET only state).
            #
            # supabase-py v2 doesn't expose partial ON CONFLICT DO UPDATE SET, so we
            # use a read-then-write pattern. State changes are rare; this is acceptable.
            existing_resp = await (
                client.table("fsm_state")
                .select("data")
                .eq("key", db_key)
                .limit(1)
                .execute()
            )
            existing_rows = existing_resp.data or []
            existing_data: dict = {}
            if existing_rows:
                raw = existing_rows[0].get("data")
                if isinstance(raw, dict):
                    existing_data = raw
                elif isinstance(raw, str):
                    try:
                        existing_data = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        existing_data = {}

            payload = {
                "key": db_key,
                "state": state_str,
                "data": existing_data,
            }
            await client.table("fsm_state").upsert(payload, on_conflict="key").execute()
            logger.debug("fsm_storage: set_state key=%s state=%s", db_key, state_str)
        except Exception as exc:
            logger.error(
                "fsm_storage: set_state failed key=%s state=%s: %s",
                db_key,
                state_str,
                exc,
            )
            raise

    async def get_state(self, key: StorageKey) -> str | None:
        """
        Return the current FSM state string, or None if no active state.
        """
        db_key = _key(key)
        client = await get_supabase_client()
        try:
            resp = await (
                client.table("fsm_state")
                .select("state")
                .eq("key", db_key)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            if not rows:
                return None
            return rows[0].get("state")
        except Exception as exc:
            logger.error("fsm_storage: get_state failed key=%s: %s", db_key, exc)
            raise

    # ------------------------------------------------------------------
    # Data operations
    # ------------------------------------------------------------------

    async def set_data(
        self,
        key: StorageKey,
        data: dict[str, Any],
    ) -> None:
        """
        Replace the entire FSM data dict for a user. Preserves existing state.
        """
        db_key = _key(key)
        client = await get_supabase_client()
        try:
            # Read current state to preserve it
            existing_resp = await (
                client.table("fsm_state")
                .select("state")
                .eq("key", db_key)
                .limit(1)
                .execute()
            )
            existing_rows = existing_resp.data or []
            existing_state: str | None = None
            if existing_rows:
                existing_state = existing_rows[0].get("state")

            payload = {
                "key": db_key,
                "state": existing_state,
                "data": data,
            }
            await client.table("fsm_state").upsert(payload, on_conflict="key").execute()
            logger.debug(
                "fsm_storage: set_data key=%s data_keys=%s", db_key, list(data.keys())
            )
        except Exception as exc:
            logger.error("fsm_storage: set_data failed key=%s: %s", db_key, exc)
            raise

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        """
        Return the FSM data dict. Returns {} if no row exists.
        """
        db_key = _key(key)
        client = await get_supabase_client()
        try:
            resp = await (
                client.table("fsm_state")
                .select("data")
                .eq("key", db_key)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            if not rows:
                return {}
            raw = rows[0].get("data")
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return {}
            return {}
        except Exception as exc:
            logger.error("fsm_storage: get_data failed key=%s: %s", db_key, exc)
            raise

    async def update_data(
        self,
        key: StorageKey,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Merge `data` into the existing FSM data dict (shallow merge).
        Returns the merged result.
        """
        current = await self.get_data(key)
        merged = {**current, **data}
        await self.set_data(key, merged)
        return merged

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """
        No-op — the Supabase client is managed by supabase_client.py and
        closed via close_supabase_client() in main.py shutdown.
        """
        logger.debug("fsm_storage: close() called (no-op)")
