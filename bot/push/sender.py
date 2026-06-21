"""
bot/push/sender.py — Delivers a Listing notification to one Telegram user.

send_listing_to_user(bot, user_id, listing)
  - Sends a photo if listing.image_url is set, otherwise a plain message.
  - Rate-limiting: global ≤30 msg/s via asyncio.Semaphore + 0.033s sleep.
    Per-chat: tracked via _last_sent dict; enforces ≥1s between messages to
    the same user.
  - TelegramForbiddenError (bot blocked) → set_user_inactive, no retry.
  - TelegramRetryAfter → sleep requested seconds then retry once.
  - All other Telegram errors are logged and suppressed — never raises.

This function is designed to be partially applied with `bot` bound via
functools.partial so it matches the PushCallback signature:
  async def push_callback(user_id: int, listing: Listing) -> None
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot.db.repositories import set_user_inactive
from bot.db.supabase_client import get_supabase_client
from bot.push.formatter import format_listing
from bot.scraper.models import Listing

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiting state (module-level, shared across all calls)
# ---------------------------------------------------------------------------

# Global: max 30 messages per second across all chats.
# Semaphore controls concurrency; sleep after each acquire enforces pacing.
_GLOBAL_RATE_SEMAPHORE = asyncio.Semaphore(30)
_GLOBAL_RATE_SLEEP = 0.033  # seconds — 1000ms / 30 ≈ 33ms between slots

# Per-chat: minimum gap between messages to the same chat (seconds)
_PER_CHAT_MIN_GAP = 1.0
# Dict of user_id → last send timestamp (monotonic clock)
_last_sent: dict[int, float] = {}


async def _enforce_per_chat_rate(user_id: int) -> None:
    """Sleep if needed to maintain ≥1s gap between messages to the same chat."""
    now = time.monotonic()
    last = _last_sent.get(user_id, 0.0)
    gap = now - last
    if gap < _PER_CHAT_MIN_GAP:
        await asyncio.sleep(_PER_CHAT_MIN_GAP - gap)
    _last_sent[user_id] = time.monotonic()


# ---------------------------------------------------------------------------
# Public send function
# ---------------------------------------------------------------------------


async def send_listing_to_user(
    bot: Bot,
    user_id: int,
    listing: Listing,
) -> None:
    """
    Send a single listing notification to a Telegram user.

    This function never raises — all errors are caught and logged so the
    calling poller loop can continue to the next subscriber.

    Parameters
    ----------
    bot      : aiogram Bot instance (bound at startup)
    user_id  : Telegram user id (bigint)
    listing  : Listing to send
    """
    caption, image_url = format_listing(listing)

    async with _GLOBAL_RATE_SEMAPHORE:
        await _enforce_per_chat_rate(user_id)
        await asyncio.sleep(_GLOBAL_RATE_SLEEP)  # global pacing

        try:
            await _send_once(bot, user_id, caption, image_url)
        except TelegramRetryAfter as exc:
            retry_after = exc.retry_after
            logger.warning(
                "sender: TelegramRetryAfter for user_id=%d listing_id=%s — "
                "sleeping %ds then retrying once",
                user_id,
                listing.id,
                retry_after,
            )
            await asyncio.sleep(retry_after)
            try:
                await _send_once(bot, user_id, caption, image_url)
            except Exception as retry_exc:  # noqa: BLE001
                logger.error(
                    "sender: retry failed for user_id=%d listing_id=%s: %s",
                    user_id,
                    listing.id,
                    retry_exc,
                )
        except TelegramForbiddenError:
            # Bot was blocked by the user — mark them inactive so the poller
            # stops sending them notifications
            logger.info("sender: bot blocked by user_id=%d — setting inactive", user_id)
            try:
                client = await get_supabase_client()
                await set_user_inactive(client, user_id)
            except Exception as db_exc:  # noqa: BLE001
                logger.error(
                    "sender: could not set_user_inactive for user_id=%d: %s",
                    user_id,
                    db_exc,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "sender: unexpected error for user_id=%d listing_id=%s: %s",
                user_id,
                listing.id,
                exc,
            )


async def _send_once(
    bot: Bot,
    user_id: int,
    caption: str,
    image_url: str | None,
) -> None:
    """
    Single Telegram API call — photo if image_url available, else text message.

    Raises on Telegram errors — caller handles retry / forbidden logic.
    """
    if image_url:
        await bot.send_photo(
            chat_id=user_id,
            photo=image_url,
            caption=caption,
        )
    else:
        await bot.send_message(
            chat_id=user_id,
            text=caption,
            disable_web_page_preview=True,
        )
    logger.debug(
        "sender: delivered to user_id=%d image=%s",
        user_id,
        bool(image_url),
    )
