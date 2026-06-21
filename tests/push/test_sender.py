"""
tests/push/test_sender.py — Hermetic tests for send_listing_to_user().

Tests:
  - TelegramForbiddenError → set_user_inactive called, no exception raised
  - TelegramRetryAfter → sleep(retry_after) then retry once
  - With image_url → bot.send_photo called
  - Without image_url → bot.send_message called
  - Generic exception → logged and suppressed (no raise)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from bot.push.sender import send_listing_to_user
from bot.scraper.models import Listing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _listing(image_url: str | None = "https://example.com/img.jpg") -> Listing:
    return Listing(
        id="1011171116",
        url="https://krisha.kz/a/show/1011171116",
        city_id="almaty",
        price=29_000_000,
        rooms=1,
        area=46.0,
        address="Алматы тест",
        image_url=image_url,
        is_owner=True,
    )


def _make_bot(send_photo_side_effect=None, send_message_side_effect=None) -> AsyncMock:
    bot = AsyncMock()
    if send_photo_side_effect is not None:
        bot.send_photo = AsyncMock(side_effect=send_photo_side_effect)
    else:
        bot.send_photo = AsyncMock()
    if send_message_side_effect is not None:
        bot.send_message = AsyncMock(side_effect=send_message_side_effect)
    else:
        bot.send_message = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# Photo vs message branching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSendBranching:
    async def test_with_image_url_calls_send_photo(self):
        bot = _make_bot()
        listing = _listing(image_url="https://example.com/photo.jpg")

        with patch("bot.push.sender.asyncio.sleep", new_callable=AsyncMock), \
             patch("bot.push.sender.time.monotonic", return_value=9999.0), \
             patch("bot.push.sender._last_sent", {}):
            await send_listing_to_user(bot, 12345, listing)

        bot.send_photo.assert_awaited_once()
        call_kwargs = bot.send_photo.call_args
        assert call_kwargs.kwargs["chat_id"] == 12345
        bot.send_message.assert_not_called()

    async def test_without_image_url_calls_send_message(self):
        bot = _make_bot()
        listing = _listing(image_url=None)

        with patch("bot.push.sender.asyncio.sleep", new_callable=AsyncMock), \
             patch("bot.push.sender.time.monotonic", return_value=9999.0), \
             patch("bot.push.sender._last_sent", {}):
            await send_listing_to_user(bot, 12345, listing)

        bot.send_message.assert_awaited_once()
        call_kwargs = bot.send_message.call_args
        assert call_kwargs.kwargs["chat_id"] == 12345
        bot.send_photo.assert_not_called()


# ---------------------------------------------------------------------------
# TelegramForbiddenError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestForbiddenError:
    async def test_forbidden_calls_set_user_inactive(self):
        # TelegramForbiddenError requires a Message object; we simulate it
        error = TelegramForbiddenError(
            method=MagicMock(), message="Forbidden: bot was blocked by the user"
        )
        bot = _make_bot(send_photo_side_effect=error)
        listing = _listing()

        mock_client = AsyncMock()
        set_inactive_mock = AsyncMock()

        with patch("bot.push.sender.asyncio.sleep", new_callable=AsyncMock), \
             patch("bot.push.sender.time.monotonic", return_value=9999.0), \
             patch("bot.push.sender._last_sent", {}), \
             patch("bot.push.sender.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.push.sender.set_user_inactive", set_inactive_mock):
            # Must NOT raise
            await send_listing_to_user(bot, 99, listing)

        set_inactive_mock.assert_awaited_once_with(mock_client, 99)

    async def test_forbidden_does_not_raise(self):
        error = TelegramForbiddenError(
            method=MagicMock(), message="Forbidden"
        )
        bot = _make_bot(send_photo_side_effect=error)
        listing = _listing()

        with patch("bot.push.sender.asyncio.sleep", new_callable=AsyncMock), \
             patch("bot.push.sender.time.monotonic", return_value=9999.0), \
             patch("bot.push.sender._last_sent", {}), \
             patch("bot.push.sender.get_supabase_client", AsyncMock(return_value=AsyncMock())), \
             patch("bot.push.sender.set_user_inactive", AsyncMock()):
            # Should not raise
            await send_listing_to_user(bot, 99, listing)


# ---------------------------------------------------------------------------
# TelegramRetryAfter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRetryAfter:
    async def test_retry_after_sleeps_and_retries_once(self):
        retry_error = TelegramRetryAfter(
            method=MagicMock(), message="Too Many Requests", retry_after=5
        )
        send_calls = []

        async def _send_photo(**kwargs):
            send_calls.append("send")
            if len(send_calls) == 1:
                raise retry_error

        bot = _make_bot(send_photo_side_effect=_send_photo)
        listing = _listing()

        sleep_calls = []

        async def _fake_sleep(s):
            sleep_calls.append(s)

        with patch("bot.push.sender.asyncio.sleep", side_effect=_fake_sleep), \
             patch("bot.push.sender.time.monotonic", return_value=9999.0), \
             patch("bot.push.sender._last_sent", {}):
            await send_listing_to_user(bot, 77, listing)

        # Should have been called twice (initial + retry)
        assert len(send_calls) == 2
        # Sleep should have been called with retry_after value
        assert 5 in sleep_calls

    async def test_retry_after_retry_fails_no_raise(self):
        retry_error = TelegramRetryAfter(
            method=MagicMock(), message="Too Many Requests", retry_after=3
        )

        async def _always_fail(**kwargs):
            raise retry_error

        bot = _make_bot(send_photo_side_effect=_always_fail)
        listing = _listing()

        with patch("bot.push.sender.asyncio.sleep", new_callable=AsyncMock), \
             patch("bot.push.sender.time.monotonic", return_value=9999.0), \
             patch("bot.push.sender._last_sent", {}):
            # Should not raise even when retry also fails
            await send_listing_to_user(bot, 88, listing)


# ---------------------------------------------------------------------------
# Generic exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGenericException:
    async def test_generic_exception_suppressed(self):
        """Unexpected exception must be caught and not propagate."""

        async def _explode(**kwargs):
            raise RuntimeError("unexpected telegram error")

        bot = _make_bot(send_photo_side_effect=_explode)
        listing = _listing()

        with patch("bot.push.sender.asyncio.sleep", new_callable=AsyncMock), \
             patch("bot.push.sender.time.monotonic", return_value=9999.0), \
             patch("bot.push.sender._last_sent", {}):
            await send_listing_to_user(bot, 55, listing)
        # No assertion needed — test passes if no exception propagates
