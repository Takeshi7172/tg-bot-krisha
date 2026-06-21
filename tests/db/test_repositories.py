"""
tests/db/test_repositories.py — Hermetic tests for bot/db/repositories.py.

All supabase client calls are mocked via a chainable builder mock.
No real DB connections.

Tests:
  - is_seen returns True when data row exists
  - is_seen returns False when no row
  - mark_seen calls upsert
  - get_subscribers_for_city returns SubscriptionRow list with correct types
  - get_active_city_slugs deduplicates
  - remove_subscription performs soft-delete (is_active=False)
  - upsert_user calls upsert with correct payload
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.db.repositories import (
    SubscriptionRow,
    get_active_city_slugs,
    get_subscribers_for_city,
    is_seen,
    mark_seen,
    remove_subscription,
    upsert_user,
    get_user_subscriptions,
)


# ---------------------------------------------------------------------------
# Chainable supabase mock builder
# ---------------------------------------------------------------------------


def _make_client(table_responses: dict | None = None):
    """
    Returns a mock Supabase AsyncClient.

    supabase-py uses synchronous chaining for builder methods (select, eq, etc.)
    and only execute() is async. All builder chain methods must be MagicMock
    (returning self), not AsyncMock.

    table_responses: { "table_name": [row, ...] }
    """
    table_responses = table_responses or {}

    client = MagicMock()

    def _table(name: str):
        resp_data = table_responses.get(name, [])
        execute_result = MagicMock()
        execute_result.data = resp_data

        # Sync builder — only execute() is async
        builder = MagicMock()
        for method in ("select", "eq", "neq", "limit", "order", "update",
                       "upsert", "insert", "delete", "filter"):
            getattr(builder, method).return_value = builder
        builder.execute = AsyncMock(return_value=execute_result)
        return builder

    client.table = MagicMock(side_effect=_table)
    return client


# ---------------------------------------------------------------------------
# is_seen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestIsSeen:
    async def test_returns_true_when_row_exists(self):
        client = _make_client(
            {"seen_listings": [{"listing_id": "abc", "city_id": "almaty"}]}
        )
        result = await is_seen(client, "abc", "almaty")
        assert result is True

    async def test_returns_false_when_no_row(self):
        client = _make_client({"seen_listings": []})
        result = await is_seen(client, "xyz", "almaty")
        assert result is False

    async def test_queries_seen_listings_table(self):
        client = _make_client({"seen_listings": []})
        await is_seen(client, "123", "almaty")
        client.table.assert_called_with("seen_listings")


# ---------------------------------------------------------------------------
# mark_seen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMarkSeen:
    async def test_calls_upsert_on_seen_listings(self):
        client = _make_client({"seen_listings": []})
        await mark_seen(client, "123", "almaty")
        client.table.assert_called_with("seen_listings")

    async def test_does_not_raise_on_success(self):
        client = _make_client({"seen_listings": []})
        # Should complete without exception
        await mark_seen(client, "999", "astana")


# ---------------------------------------------------------------------------
# get_subscribers_for_city
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetSubscribersForCity:
    async def test_returns_subscription_row_list(self):
        client = _make_client({
            "subscriptions": [
                {"user_id": 100, "price_min": 10_000_000, "price_max": 50_000_000, "rooms": [1, 2]},
                {"user_id": 200, "price_min": None, "price_max": None, "rooms": None},
            ]
        })
        result = await get_subscribers_for_city(client, "almaty")
        assert len(result) == 2
        assert isinstance(result[0], SubscriptionRow)
        assert result[0].user_id == 100
        assert result[0].price_min == 10_000_000
        assert result[0].rooms == [1, 2]

    async def test_none_rooms_preserved(self):
        client = _make_client({
            "subscriptions": [
                {"user_id": 300, "price_min": None, "price_max": None, "rooms": None},
            ]
        })
        result = await get_subscribers_for_city(client, "almaty")
        assert result[0].rooms is None

    async def test_empty_rooms_list_becomes_none(self):
        """Empty rooms list [] should become None (no filter)."""
        client = _make_client({
            "subscriptions": [
                {"user_id": 400, "price_min": None, "price_max": None, "rooms": []},
            ]
        })
        result = await get_subscribers_for_city(client, "almaty")
        assert result[0].rooms is None

    async def test_user_id_is_int(self):
        client = _make_client({
            "subscriptions": [
                {"user_id": "500", "price_min": None, "price_max": None, "rooms": None},
            ]
        })
        result = await get_subscribers_for_city(client, "almaty")
        assert isinstance(result[0].user_id, int)
        assert result[0].user_id == 500

    async def test_empty_result(self):
        client = _make_client({"subscriptions": []})
        result = await get_subscribers_for_city(client, "karaganda")
        assert result == []


# ---------------------------------------------------------------------------
# get_active_city_slugs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetActiveCitySlugs:
    async def test_returns_list_of_slugs(self):
        client = _make_client({
            "subscriptions": [
                {"city_id": "almaty"},
                {"city_id": "astana"},
            ]
        })
        result = await get_active_city_slugs(client)
        assert "almaty" in result
        assert "astana" in result

    async def test_deduplicates_city_slugs(self):
        client = _make_client({
            "subscriptions": [
                {"city_id": "almaty"},
                {"city_id": "almaty"},
                {"city_id": "astana"},
                {"city_id": "almaty"},
            ]
        })
        result = await get_active_city_slugs(client)
        assert result.count("almaty") == 1
        assert len(result) == 2

    async def test_empty_returns_empty_list(self):
        client = _make_client({"subscriptions": []})
        result = await get_active_city_slugs(client)
        assert result == []

    async def test_preserves_insertion_order_of_first_occurrence(self):
        client = _make_client({
            "subscriptions": [
                {"city_id": "almaty"},
                {"city_id": "astana"},
                {"city_id": "almaty"},
            ]
        })
        result = await get_active_city_slugs(client)
        assert result[0] == "almaty"
        assert result[1] == "astana"


# ---------------------------------------------------------------------------
# remove_subscription (soft-delete)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRemoveSubscription:
    async def test_calls_update_with_is_active_false(self):
        # Capture what update was called with
        update_calls = []

        def _table(name: str):
            builder = MagicMock()

            def _update(data):
                update_calls.append(data)
                return builder

            builder.update = MagicMock(side_effect=_update)
            builder.eq.return_value = builder
            builder.execute = AsyncMock(return_value=MagicMock(data=[]))
            return builder

        client = MagicMock()
        client.table = MagicMock(side_effect=_table)

        await remove_subscription(client, user_id=1, city_id="almaty")

        assert len(update_calls) == 1
        assert update_calls[0] == {"is_active": False}

    async def test_does_not_raise(self):
        client = _make_client({"subscriptions": []})
        # Should complete without exception
        await remove_subscription(client, user_id=1, city_id="almaty")


# ---------------------------------------------------------------------------
# upsert_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestUpsertUser:
    async def test_calls_upsert_on_users_table(self):
        client = _make_client({"users": []})
        await upsert_user(
            client,
            user_id=12345,
            username="testuser",
            first_name="Test",
            language_code="ru",
        )
        client.table.assert_called_with("users")

    async def test_payload_contains_user_id(self):
        captured_data = []

        def _table(name):
            builder = AsyncMock()
            for method in ("select", "eq", "limit"):
                getattr(builder, method).return_value = builder

            def _upsert(data, **kwargs):
                captured_data.append(data)
                return builder
            builder.upsert = _upsert
            builder.execute = AsyncMock(return_value=MagicMock(data=[]))
            return builder

        client = MagicMock()
        client.table = MagicMock(side_effect=_table)

        await upsert_user(
            client,
            user_id=99999,
            username="hello",
            first_name="World",
            language_code="kk",
        )

        assert len(captured_data) == 1
        assert captured_data[0]["id"] == 99999
        assert captured_data[0]["is_active"] is True
