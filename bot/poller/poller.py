"""
bot/poller/poller.py — Background listing poller for tg-bot-krisha.

KrishaPoller loops every POLL_INTERVAL_SECONDS and:
  1. Reads active city slugs from DB (only cities with at least one active sub).
  2. For each city: fetches search page → gets (listing_id, url) list.
  3. For each UNSEEN id: fetches detail page → builds Listing.
  4. Marks listing as seen in DB.
  5. For each subscriber of that city whose filters match: calls push_callback.

Isolation contract:
  - NO aiogram/telegram imports in this file.
  - push_callback is injected by Wave 3 (the bot layer). Type signature:
      Callable[[int, Listing], Awaitable[None]]
    where the int is the Telegram user_id.

Failure isolation:
  - One city's exception MUST NOT crash the entire poller loop.
  - One listing's detail-fetch failure falls back to a partial Listing.
  - push_callback errors are caught per-subscriber and logged — the loop continues.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable

from bot.config import get_settings
from bot.db.repositories import (
    SubscriptionRow,
    get_active_city_slugs,
    get_subscribers_for_city,
    is_seen,
    mark_seen,
)
from bot.db.supabase_client import get_supabase_client
from bot.scraper.anti_bot import random_delay
from bot.scraper.krisha_scraper import KrishaScraper
from bot.scraper.models import Listing

logger = logging.getLogger(__name__)

# Number of consecutive cycles a city can return 0 cards before a WARNING is logged
_ZERO_CARD_WARNING_THRESHOLD = 3

PushCallback = Callable[[int, Listing], Awaitable[None]]


class KrishaPoller:
    """
    Background poller that scrapes krisha.kz and dispatches Listing notifications.

    Usage (from Wave 3 bot startup)
    --------------------------------
    poller = KrishaPoller()
    asyncio.create_task(poller.start(push_callback=my_bot_send_listing))

    The poller runs until the event loop is cancelled or stop() is called.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._running = False
        # Per-city consecutive zero-card counter for WARNING threshold
        self._zero_card_counts: dict[str, int] = defaultdict(int)

    async def start(self, push_callback: PushCallback) -> None:
        """
        Main polling loop. Runs indefinitely until stop() is called.

        Parameters
        ----------
        push_callback : PushCallback
            Async callable injected by the bot layer. Signature:
              async def push_callback(user_id: int, listing: Listing) -> None
            Must never raise — errors are the bot layer's responsibility. The
            poller wraps each call in a try/except for safety.
        """
        self._running = True
        logger.info(
            "poller: starting — interval=%ds", self._settings.POLL_INTERVAL_SECONDS
        )

        while self._running:
            try:
                await self._poll_cycle(push_callback)
            except asyncio.CancelledError:
                logger.info("poller: cancelled — stopping")
                self._running = False
                return
            except Exception as exc:  # noqa: BLE001
                # Unexpected error in the cycle orchestrator (not in per-city logic)
                logger.error("poller: unexpected error in poll cycle: %s", exc)

            if not self._running:
                break

            logger.debug(
                "poller: sleeping %ds until next cycle",
                self._settings.POLL_INTERVAL_SECONDS,
            )
            try:
                await asyncio.sleep(self._settings.POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                logger.info("poller: sleep cancelled — stopping")
                self._running = False
                return

    def stop(self) -> None:
        """Signal the poller to stop after the current sleep completes."""
        logger.info("poller: stop() called")
        self._running = False

    # ------------------------------------------------------------------
    # Poll cycle
    # ------------------------------------------------------------------

    async def _poll_cycle(self, push_callback: PushCallback) -> None:
        """
        One complete poll cycle: all active cities.
        """
        client = await get_supabase_client()
        city_slugs = await get_active_city_slugs(client)

        if not city_slugs:
            logger.info("poller: no active city subscriptions — skipping cycle")
            return

        logger.info(
            "poller: poll cycle starting for %d cities: %s", len(city_slugs), city_slugs
        )

        async with KrishaScraper() as scraper:
            for city_slug in city_slugs:
                try:
                    await self._poll_city(scraper, client, city_slug, push_callback)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "poller: unhandled error for city=%s: %s — continuing to next city",
                        city_slug,
                        exc,
                    )

                # Delay between cities to avoid hammering the server
                await random_delay(min_s=2.0, max_s=3.0)

        logger.info("poller: poll cycle complete")

    async def _poll_city(
        self,
        scraper: KrishaScraper,
        client,
        city_slug: str,
        push_callback: PushCallback,
    ) -> None:
        """
        Poll one city: fetch search page → filter unseen → fetch details → notify.
        """
        # Phase 1: get listing ids from search page
        listing_id_pairs = await scraper.fetch_listing_ids(city_slug, page=1)
        card_count = len(listing_id_pairs)

        # Zero-card tracking
        if card_count == 0:
            self._zero_card_counts[city_slug] += 1
            if self._zero_card_counts[city_slug] >= _ZERO_CARD_WARNING_THRESHOLD:
                logger.warning(
                    "poller: city=%s returned 0 cards for %d consecutive cycles",
                    city_slug,
                    self._zero_card_counts[city_slug],
                )
        else:
            if self._zero_card_counts[city_slug] > 0:
                logger.info(
                    "poller: city=%s recovered — returned %d cards (was 0 for %d cycles)",
                    city_slug,
                    card_count,
                    self._zero_card_counts[city_slug],
                )
            self._zero_card_counts[city_slug] = 0

        if card_count == 0:
            logger.info(
                "poller: city=%s status=ok cards=0 new=0 owner=0 (empty search page)",
                city_slug,
            )
            return

        # Phase 2: filter out already-seen ids (pre-check before detail fetch)
        new_pairs: list[tuple[str, str]] = []
        for listing_id, url in listing_id_pairs:
            try:
                seen = await is_seen(client, listing_id, city_slug)
            except Exception as exc:
                logger.warning(
                    "poller: is_seen check failed listing_id=%s city=%s: %s — assuming unseen",
                    listing_id,
                    city_slug,
                    exc,
                )
                seen = False

            if not seen:
                new_pairs.append((listing_id, url))

        new_count = len(new_pairs)
        logger.info(
            "poller: city=%s cards=%d new=%d",
            city_slug,
            card_count,
            new_count,
        )

        if new_count == 0:
            return

        # Fetch subscribers once for this city (avoid N+1 per listing)
        try:
            subscribers: list[SubscriptionRow] = await get_subscribers_for_city(
                client, city_slug
            )
        except Exception as exc:
            logger.error(
                "poller: get_subscribers_for_city failed city=%s: %s — skipping notifications",
                city_slug,
                exc,
            )
            return

        if not subscribers:
            logger.info(
                "poller: city=%s has new listings but no active subscribers — skipping",
                city_slug,
            )
            # Still mark seen so we don't reprocess on next cycle
            for listing_id, _ in new_pairs:
                await self._safe_mark_seen(client, listing_id, city_slug)
            return

        # Phase 3: fetch detail + mark seen + notify subscribers
        owner_count = 0
        for listing_id, _url in new_pairs:
            listing = await scraper.fetch_listing_detail(listing_id, city_slug)

            # ALWAYS mark seen — prevents re-fetching on the next cycle regardless
            # of whether the listing is pushed (non-owners must not reappear).
            await self._safe_mark_seen(client, listing_id, city_slug)

            if not listing.is_owner:
                # das[who]=1 includes developers (userType="complex"); skip them.
                # Only private owners (userType="owner") are pushed to subscribers.
                logger.info(
                    "poller: listing_id=%s city=%s skipped — not a private owner "
                    "(userType not 'owner'); marked seen",
                    listing_id,
                    city_slug,
                )
                await random_delay(min_s=2.0, max_s=3.0)
                continue

            owner_count += 1

            # Notify matching subscribers (owner listing only)
            for sub in subscribers:
                if self._matches_filters(listing, sub):
                    await self._safe_push(push_callback, sub.user_id, listing)

            # Delay between detail fetches
            await random_delay(min_s=2.0, max_s=3.0)

        logger.info(
            "poller: city=%s status=ok cards=%d new=%d owner=%d",
            city_slug,
            card_count,
            new_count,
            owner_count,
        )

    # ------------------------------------------------------------------
    # Filter matching
    # ------------------------------------------------------------------

    @staticmethod
    def _matches_filters(listing: Listing, sub: SubscriptionRow) -> bool:
        """
        Return True if the listing satisfies ALL filters in the subscription.

        Rules (from brief):
          - price_min: listing.price >= price_min (skip if either is None)
          - price_max: listing.price <= price_max (skip if either is None)
          - rooms: listing.rooms in sub.rooms list (skip if sub.rooms is None
                   or listing.rooms is None)
        A None filter means "no restriction" — the listing passes that filter.
        """
        # Price lower bound
        if sub.price_min is not None and listing.price is not None:
            if listing.price < sub.price_min:
                return False

        # Price upper bound
        if sub.price_max is not None and listing.price is not None:
            if listing.price > sub.price_max:
                return False

        # Room filter
        if sub.rooms is not None and listing.rooms is not None:
            if listing.rooms not in sub.rooms:
                return False

        return True

    # ------------------------------------------------------------------
    # Safe wrappers (log but do not raise)
    # ------------------------------------------------------------------

    async def _safe_mark_seen(
        self,
        client,
        listing_id: str,
        city_id: str,
    ) -> None:
        try:
            await mark_seen(client, listing_id, city_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "poller: mark_seen failed listing_id=%s city=%s: %s",
                listing_id,
                city_id,
                exc,
            )

    async def _safe_push(
        self,
        push_callback: PushCallback,
        user_id: int,
        listing: Listing,
    ) -> None:
        try:
            await push_callback(user_id, listing)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "poller: push_callback raised for user_id=%d listing_id=%s: %s",
                user_id,
                listing.id,
                exc,
            )
