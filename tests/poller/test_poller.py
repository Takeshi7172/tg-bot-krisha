"""
tests/poller/test_poller.py — Hermetic tests for KrishaPoller.

Tests (100% dedup + owner-skip path coverage):
  1. Seen listing id → skipped (no detail fetch, no push)
  2. New owner listing → mark_seen called BEFORE push, push called
  3. New complex listing → mark_seen called, push NOT called
  4. One city raises → other cities still processed
  5. _matches_filters: price/rooms including None values
  6. Empty subscriber list → mark_seen still called, no push
  7. push_callback raises → loop continues for next subscriber
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from bot.db.repositories import SubscriptionRow
from bot.poller.poller import KrishaPoller
from bot.scraper.models import Listing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sub(
    user_id: int = 1,
    price_min: int | None = None,
    price_max: int | None = None,
    rooms: list[int] | None = None,
) -> SubscriptionRow:
    return SubscriptionRow(
        user_id=user_id,
        price_min=price_min,
        price_max=price_max,
        rooms=rooms,
    )


def _listing(
    lid: str = "123",
    city: str = "almaty",
    is_owner: bool = True,
    price: int | None = 20_000_000,
    rooms: int | None = 2,
) -> Listing:
    return Listing(
        id=lid,
        url=f"https://krisha.kz/a/show/{lid}",
        city_id=city,
        price=price,
        rooms=rooms,
        is_owner=is_owner,
    )


# ---------------------------------------------------------------------------
# _matches_filters
# ---------------------------------------------------------------------------


class TestMatchesFilters:
    poller = KrishaPoller()

    def test_no_filters_always_matches(self):
        listing = _listing(price=25_000_000, rooms=3)
        assert self.poller._matches_filters(listing, _sub()) is True

    def test_price_min_passes_when_gte(self):
        listing = _listing(price=25_000_000)
        assert self.poller._matches_filters(listing, _sub(price_min=20_000_000)) is True

    def test_price_min_fails_when_lt(self):
        listing = _listing(price=15_000_000)
        assert self.poller._matches_filters(listing, _sub(price_min=20_000_000)) is False

    def test_price_max_passes_when_lte(self):
        listing = _listing(price=30_000_000)
        assert self.poller._matches_filters(listing, _sub(price_max=30_000_000)) is True

    def test_price_max_fails_when_gt(self):
        listing = _listing(price=35_000_000)
        assert self.poller._matches_filters(listing, _sub(price_max=30_000_000)) is False

    def test_rooms_passes_when_in_list(self):
        listing = _listing(rooms=2)
        assert self.poller._matches_filters(listing, _sub(rooms=[1, 2, 3])) is True

    def test_rooms_fails_when_not_in_list(self):
        listing = _listing(rooms=4)
        assert self.poller._matches_filters(listing, _sub(rooms=[1, 2])) is False

    def test_none_listing_price_skips_price_filter(self):
        """If listing.price is None, price filters are ignored."""
        listing = _listing(price=None)
        assert self.poller._matches_filters(
            listing, _sub(price_min=10_000_000, price_max=50_000_000)
        ) is True

    def test_none_sub_rooms_skips_rooms_filter(self):
        """If sub.rooms is None, any listing.rooms passes."""
        listing = _listing(rooms=5)
        assert self.poller._matches_filters(listing, _sub(rooms=None)) is True

    def test_none_listing_rooms_with_sub_rooms_passes(self):
        """If listing.rooms is None, rooms filter is skipped."""
        listing = _listing(rooms=None)
        assert self.poller._matches_filters(listing, _sub(rooms=[1, 2])) is True

    def test_combined_filters_pass(self):
        listing = _listing(price=25_000_000, rooms=2)
        assert self.poller._matches_filters(
            listing, _sub(price_min=20_000_000, price_max=30_000_000, rooms=[1, 2])
        ) is True

    def test_combined_filters_fail_on_rooms(self):
        listing = _listing(price=25_000_000, rooms=3)
        assert self.poller._matches_filters(
            listing, _sub(price_min=20_000_000, price_max=30_000_000, rooms=[1, 2])
        ) is False


# ---------------------------------------------------------------------------
# _poll_city — core dedup + owner-skip tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPollCity:
    """
    Test _poll_city by injecting mocked scraper, repos and a push_callback.
    All asyncio.sleep / random_delay calls are mocked out.
    """

    async def _run_poll_city(
        self,
        listing_id_pairs: list,
        detail_listings: list,
        seen_results: list,
        subscribers: list,
        push_callback: AsyncMock,
    ):
        """
        Run _poll_city with full mocks.

        listing_id_pairs : list of (id, url) tuples returned by fetch_listing_ids
        detail_listings  : Listing objects returned by fetch_listing_detail, in order
        seen_results     : bool per listing_id returned by is_seen, in order
        subscribers      : list of SubscriptionRow
        push_callback    : AsyncMock
        """
        poller = KrishaPoller()

        mock_scraper = AsyncMock()
        mock_scraper.fetch_listing_ids = AsyncMock(return_value=listing_id_pairs)
        mock_scraper.fetch_listing_detail = AsyncMock(side_effect=detail_listings)

        mock_client = MagicMock()

        is_seen_iter = iter(seen_results)
        mark_seen_mock = AsyncMock()
        get_subs_mock = AsyncMock(return_value=subscribers)

        async def _is_seen(client, listing_id, city_id):
            return next(is_seen_iter)

        with patch("bot.poller.poller.is_seen", side_effect=_is_seen), \
             patch("bot.poller.poller.mark_seen", mark_seen_mock), \
             patch("bot.poller.poller.get_subscribers_for_city", get_subs_mock), \
             patch("bot.poller.poller.random_delay", new_callable=AsyncMock):
            await poller._poll_city(
                mock_scraper, mock_client, "almaty", push_callback
            )

        return mark_seen_mock, get_subs_mock

    async def test_seen_id_is_skipped_no_detail_fetch_no_push(self):
        push_cb = AsyncMock()
        mark_seen_mock, _ = await self._run_poll_city(
            listing_id_pairs=[("111", "https://krisha.kz/a/show/111")],
            detail_listings=[],
            seen_results=[True],  # already seen
            subscribers=[_sub(user_id=1)],
            push_callback=push_cb,
        )
        push_cb.assert_not_called()
        # mark_seen should NOT be called for already-seen listings
        mark_seen_mock.assert_not_called()

    async def test_new_owner_listing_push_called_and_mark_seen_called(self):
        listing = _listing(lid="222", is_owner=True)
        push_cb = AsyncMock()
        mark_seen_mock, _ = await self._run_poll_city(
            listing_id_pairs=[("222", "https://krisha.kz/a/show/222")],
            detail_listings=[listing],
            seen_results=[False],
            subscribers=[_sub(user_id=42)],
            push_callback=push_cb,
        )
        # mark_seen called
        mark_seen_mock.assert_awaited_once()
        # push called
        push_cb.assert_awaited_once_with(42, listing)

    async def test_new_owner_listing_mark_seen_before_push(self):
        """mark_seen must be called BEFORE push_callback."""
        call_order = []

        async def _mark_seen(client, listing_id, city_id):
            call_order.append("mark_seen")

        async def _push(user_id, listing):
            call_order.append("push")

        poller = KrishaPoller()
        listing = _listing(lid="333", is_owner=True)
        mock_scraper = AsyncMock()
        mock_scraper.fetch_listing_ids = AsyncMock(
            return_value=[("333", "https://krisha.kz/a/show/333")]
        )
        mock_scraper.fetch_listing_detail = AsyncMock(return_value=listing)

        with patch("bot.poller.poller.is_seen", AsyncMock(return_value=False)), \
             patch("bot.poller.poller.mark_seen", side_effect=_mark_seen), \
             patch(
                 "bot.poller.poller.get_subscribers_for_city",
                 AsyncMock(return_value=[_sub(user_id=1)]),
             ), \
             patch("bot.poller.poller.random_delay", new_callable=AsyncMock):
            await poller._poll_city(mock_scraper, MagicMock(), "almaty", _push)

        assert call_order == ["mark_seen", "push"], (
            f"Expected mark_seen before push, got: {call_order}"
        )

    async def test_complex_listing_mark_seen_called_push_not_called(self):
        """Complex (developer) listing: mark_seen YES, push NO."""
        listing = _listing(lid="444", is_owner=False)
        push_cb = AsyncMock()
        mark_seen_mock, _ = await self._run_poll_city(
            listing_id_pairs=[("444", "https://krisha.kz/a/show/444")],
            detail_listings=[listing],
            seen_results=[False],
            subscribers=[_sub(user_id=1)],
            push_callback=push_cb,
        )
        mark_seen_mock.assert_awaited_once()
        push_cb.assert_not_called()

    async def test_empty_listings_returns_without_error(self):
        push_cb = AsyncMock()
        mark_seen_mock, _ = await self._run_poll_city(
            listing_id_pairs=[],
            detail_listings=[],
            seen_results=[],
            subscribers=[],
            push_callback=push_cb,
        )
        push_cb.assert_not_called()

    async def test_no_subscribers_mark_seen_still_called(self):
        """No subscribers for city → still mark listings as seen."""
        listing = _listing(lid="555", is_owner=True)
        push_cb = AsyncMock()
        mark_seen_mock, _ = await self._run_poll_city(
            listing_id_pairs=[("555", "https://krisha.kz/a/show/555")],
            detail_listings=[listing],
            seen_results=[False],
            subscribers=[],  # no subscribers
            push_callback=push_cb,
        )
        mark_seen_mock.assert_awaited_once()
        push_cb.assert_not_called()

    async def test_push_callback_raises_loop_continues(self):
        """push_callback raising must not crash the poller."""
        listing1 = _listing(lid="601", is_owner=True)
        listing2 = _listing(lid="602", is_owner=True)

        push_calls = []

        async def _push(user_id, listing):
            push_calls.append(listing.id)
            if listing.id == "601":
                raise RuntimeError("push failed!")

        poller = KrishaPoller()
        mock_scraper = AsyncMock()
        mock_scraper.fetch_listing_ids = AsyncMock(
            return_value=[
                ("601", "https://krisha.kz/a/show/601"),
                ("602", "https://krisha.kz/a/show/602"),
            ]
        )
        mock_scraper.fetch_listing_detail = AsyncMock(
            side_effect=[listing1, listing2]
        )

        seen_vals = iter([False, False])

        with patch("bot.poller.poller.is_seen", AsyncMock(side_effect=lambda *a: next(seen_vals))), \
             patch("bot.poller.poller.mark_seen", AsyncMock()), \
             patch(
                 "bot.poller.poller.get_subscribers_for_city",
                 AsyncMock(return_value=[_sub(user_id=1)]),
             ), \
             patch("bot.poller.poller.random_delay", new_callable=AsyncMock):
            await poller._poll_city(mock_scraper, MagicMock(), "almaty", _push)

        # Both listings should have been attempted
        assert "601" in push_calls
        assert "602" in push_calls


# ---------------------------------------------------------------------------
# _poll_cycle — city isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPollCycleIsolation:
    async def test_one_city_raises_other_cities_still_processed(self):
        """Exception in city A must not prevent city B from running."""
        cities_processed = []

        async def _poll_city(scraper, client, city_slug, push_callback):
            cities_processed.append(city_slug)
            if city_slug == "almaty":
                raise RuntimeError("almaty exploded")

        poller = KrishaPoller()

        mock_client = MagicMock()

        with patch("bot.poller.poller.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.poller.poller.get_active_city_slugs", AsyncMock(return_value=["almaty", "astana"])), \
             patch.object(poller, "_poll_city", side_effect=_poll_city), \
             patch("bot.poller.poller.random_delay", new_callable=AsyncMock):
            await poller._poll_cycle(AsyncMock())

        assert "almaty" in cities_processed
        assert "astana" in cities_processed

    async def test_no_active_cities_skips_cycle(self):
        poller = KrishaPoller()
        mock_client = MagicMock()

        with patch("bot.poller.poller.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.poller.poller.get_active_city_slugs", AsyncMock(return_value=[])), \
             patch.object(poller, "_poll_city", AsyncMock()) as mock_poll_city:
            await poller._poll_cycle(AsyncMock())

        mock_poll_city.assert_not_called()
