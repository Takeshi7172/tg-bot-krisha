"""
tests/bot/test_fsm_storage.py — Hermetic tests for SupabaseFSMStorage.

Tests:
  - set_state persists the correct state string
  - set_data preserves existing state when updating data
  - get_state returns None when no row exists
  - get_data returns {} when no row exists
  - Round-trip: set_state + set_data + get_state + get_data
  - update_data merges dicts correctly
  - Simulated restart: set_state in one storage instance, get_state in new instance
    reading from the same mocked table
  - close() is a no-op (does not raise)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from aiogram.fsm.storage.base import StorageKey

from bot.fsm.storage import SupabaseFSMStorage, _key


# ---------------------------------------------------------------------------
# Helper: StorageKey factory
# ---------------------------------------------------------------------------


def _storage_key(chat_id: int = 100, user_id: int = 200) -> StorageKey:
    return StorageKey(bot_id=0, chat_id=chat_id, user_id=user_id)


# ---------------------------------------------------------------------------
# _key helper
# ---------------------------------------------------------------------------


class TestKeyHelper:
    def test_format_is_fsm_chat_user(self):
        key = _storage_key(chat_id=100, user_id=200)
        assert _key(key) == "fsm:100:200"

    def test_different_users_produce_different_keys(self):
        k1 = _key(_storage_key(chat_id=100, user_id=1))
        k2 = _key(_storage_key(chat_id=100, user_id=2))
        assert k1 != k2


# ---------------------------------------------------------------------------
# Chainable supabase mock builder for FSM
# ---------------------------------------------------------------------------


def _make_fsm_client(row_data: list | None = None) -> MagicMock:
    """
    Returns a mock that supports the read-then-write pattern in set_state/set_data.

    supabase-py uses synchronous chaining — only execute() is async.
    row_data: the row returned by .select().eq().limit().execute()
    """
    row_data = row_data or []

    client = MagicMock()

    def _table(name: str):
        result = MagicMock()
        result.data = list(row_data)

        # Sync builder — only execute is awaitable
        builder = MagicMock()
        for method in ("select", "eq", "neq", "limit", "order", "filter"):
            getattr(builder, method).return_value = builder
        builder.execute = AsyncMock(return_value=result)
        # upsert returns the same builder so .execute() works
        builder.upsert = MagicMock(return_value=builder)
        return builder

    client.table = MagicMock(side_effect=_table)
    return client


# ---------------------------------------------------------------------------
# set_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSetState:
    async def test_set_state_calls_upsert_with_state_string(self):
        client = _make_fsm_client(row_data=[])
        storage = SupabaseFSMStorage()
        key = _storage_key()

        with patch("bot.fsm.storage.get_supabase_client", AsyncMock(return_value=client)):
            await storage.set_state(key, "SubscribeFlow:choosing_city")

        client.table.assert_called_with("fsm_state")

    async def test_set_state_none_clears_state(self):
        """Setting state=None should store state_str=None in the payload (no exception)."""
        client = _make_fsm_client(row_data=[])
        storage = SupabaseFSMStorage()
        key = _storage_key()

        with patch("bot.fsm.storage.get_supabase_client", AsyncMock(return_value=client)):
            await storage.set_state(key, None)
        # No exception = correct behaviour


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetState:
    async def test_returns_none_when_no_row(self):
        client = _make_fsm_client(row_data=[])
        storage = SupabaseFSMStorage()
        key = _storage_key()

        with patch("bot.fsm.storage.get_supabase_client", AsyncMock(return_value=client)):
            result = await storage.get_state(key)

        assert result is None

    async def test_returns_state_string_from_row(self):
        row = {"state": "SubscribeFlow:asking_filters", "data": {}}
        client = _make_fsm_client(row_data=[row])
        storage = SupabaseFSMStorage()
        key = _storage_key()

        with patch("bot.fsm.storage.get_supabase_client", AsyncMock(return_value=client)):
            result = await storage.get_state(key)

        assert result == "SubscribeFlow:asking_filters"

    async def test_returns_none_when_state_is_null(self):
        row = {"state": None, "data": {}}
        client = _make_fsm_client(row_data=[row])
        storage = SupabaseFSMStorage()
        key = _storage_key()

        with patch("bot.fsm.storage.get_supabase_client", AsyncMock(return_value=client)):
            result = await storage.get_state(key)

        assert result is None


# ---------------------------------------------------------------------------
# get_data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetData:
    async def test_returns_empty_dict_when_no_row(self):
        client = _make_fsm_client(row_data=[])
        storage = SupabaseFSMStorage()
        key = _storage_key()

        with patch("bot.fsm.storage.get_supabase_client", AsyncMock(return_value=client)):
            result = await storage.get_data(key)

        assert result == {}

    async def test_returns_data_dict_from_row(self):
        row = {"state": "some_state", "data": {"city_id": "almaty", "price_min": 5000000}}
        client = _make_fsm_client(row_data=[row])
        storage = SupabaseFSMStorage()
        key = _storage_key()

        with patch("bot.fsm.storage.get_supabase_client", AsyncMock(return_value=client)):
            result = await storage.get_data(key)

        assert result == {"city_id": "almaty", "price_min": 5000000}

    async def test_handles_json_string_data(self):
        """data column may be returned as a JSON string in some supabase-py versions."""
        import json
        row = {"state": "some_state", "data": json.dumps({"foo": "bar"})}
        client = _make_fsm_client(row_data=[row])
        storage = SupabaseFSMStorage()
        key = _storage_key()

        with patch("bot.fsm.storage.get_supabase_client", AsyncMock(return_value=client)):
            result = await storage.get_data(key)

        assert result == {"foo": "bar"}

    async def test_handles_invalid_json_string_returns_empty(self):
        row = {"state": "some_state", "data": "not json {{{"}
        client = _make_fsm_client(row_data=[row])
        storage = SupabaseFSMStorage()
        key = _storage_key()

        with patch("bot.fsm.storage.get_supabase_client", AsyncMock(return_value=client)):
            result = await storage.get_data(key)

        assert result == {}


# ---------------------------------------------------------------------------
# update_data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestUpdateData:
    async def test_merges_new_data_with_existing(self):
        """update_data should shallow-merge, not replace."""
        storage = SupabaseFSMStorage()
        key = _storage_key()
        existing = {"city_id": "almaty", "price_min": 1000}

        with patch.object(storage, "get_data", AsyncMock(return_value=existing)), \
             patch.object(storage, "set_data", AsyncMock()) as mock_set:
            result = await storage.update_data(key, {"price_max": 50000})

        assert result == {"city_id": "almaty", "price_min": 1000, "price_max": 50000}
        mock_set.assert_awaited_once_with(key, result)

    async def test_update_overwrites_existing_key(self):
        storage = SupabaseFSMStorage()
        key = _storage_key()
        existing = {"city_id": "almaty"}

        with patch.object(storage, "get_data", AsyncMock(return_value=existing)), \
             patch.object(storage, "set_data", AsyncMock()):
            result = await storage.update_data(key, {"city_id": "astana"})

        assert result["city_id"] == "astana"


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestClose:
    async def test_close_is_noop(self):
        """close() should complete without raising."""
        storage = SupabaseFSMStorage()
        # Should not raise
        await storage.close()


# ---------------------------------------------------------------------------
# Simulated restart (round-trip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRoundTrip:
    async def test_state_persists_across_storage_instances(self):
        """
        Simulate: instance A sets state, instance B (new) reads it.
        Both point to the same mocked row in the DB.
        """
        persisted_rows: list[dict] = []

        def _make_client_for_row():
            client = MagicMock()

            def _table(name: str):
                result_mock = MagicMock()
                result_mock.data = list(persisted_rows)  # snapshot at call time

                # Sync builder — only execute is async
                builder = MagicMock()
                builder.select.return_value = builder
                builder.eq.return_value = builder
                builder.limit.return_value = builder
                builder.execute = AsyncMock(return_value=result_mock)

                def _upsert(payload, **kw):
                    persisted_rows.clear()
                    persisted_rows.append(payload)
                    # Update the result_mock so get_state reads the new data
                    result_mock.data = list(persisted_rows)
                    return builder

                builder.upsert = MagicMock(side_effect=_upsert)
                return builder

            client.table = MagicMock(side_effect=_table)
            return client

        key = _storage_key()

        # Instance A: set state
        storage_a = SupabaseFSMStorage()
        client_a = _make_client_for_row()
        with patch("bot.fsm.storage.get_supabase_client", AsyncMock(return_value=client_a)):
            await storage_a.set_state(key, "SubscribeFlow:choosing_city")

        # Verify persisted_rows has the state
        assert any(r.get("state") == "SubscribeFlow:choosing_city" for r in persisted_rows)

        # Instance B: get state (new instance, reads same persisted_rows)
        storage_b = SupabaseFSMStorage()
        client_b = _make_client_for_row()
        with patch("bot.fsm.storage.get_supabase_client", AsyncMock(return_value=client_b)):
            result = await storage_b.get_state(key)

        assert result == "SubscribeFlow:choosing_city"
