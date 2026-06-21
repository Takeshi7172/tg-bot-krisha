"""
tests/bot/test_handlers.py — Hermetic tests for bot handlers.

Strategy: mock Message, CallbackQuery, FSMContext, and DB calls directly.
No aiogram Dispatcher or Router is instantiated — we call handler functions directly.

Tests:
  - cmd_start: upsert called + state set to choosing_city + greeting sent
  - cb_city_selected: state updated + set to asking_filters + message answered
  - cb_filter_answer "no": finalize subscription called (no filters path)
  - cb_skip_rooms: all-None filters → finalize subscription
  - cmd_my_subscriptions: empty → prompts /start
  - cmd_my_subscriptions: with subs → shows list
  - cb_unsubscribe: calls remove_subscription + edits message
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.fsm.states import SubscribeFlow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_message(
    user_id: int = 12345,
    text: str = "/start",
    username: str = "testuser",
    first_name: str = "Test",
    language_code: str = "ru",
) -> AsyncMock:
    """Return a mock aiogram Message."""
    msg = AsyncMock()
    user = MagicMock()
    user.id = user_id
    user.username = username
    user.first_name = first_name
    user.language_code = language_code
    msg.from_user = user
    msg.text = text
    msg.answer = AsyncMock()
    return msg


def _make_callback(
    user_id: int = 12345,
    data: str = "",
) -> AsyncMock:
    """Return a mock aiogram CallbackQuery."""
    cb = AsyncMock()
    user = MagicMock()
    user.id = user_id
    cb.from_user = user
    cb.data = data
    cb.answer = AsyncMock()
    msg = AsyncMock()
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    cb.message = msg
    return cb


def _make_state(data: dict | None = None) -> AsyncMock:
    """Return a mock FSMContext."""
    state = AsyncMock()
    state.get_data = AsyncMock(return_value=data or {})
    state.set_state = AsyncMock()
    state.clear = AsyncMock()
    state.update_data = AsyncMock()
    return state


# ---------------------------------------------------------------------------
# cmd_start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCmdStart:
    async def test_upserts_user_and_sets_choosing_city_state(self):
        from bot.handlers.start import cmd_start

        msg = _make_message()
        state = _make_state()
        mock_client = AsyncMock()
        upsert_mock = AsyncMock()

        with patch("bot.handlers.start.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.handlers.start.upsert_user", upsert_mock):
            await cmd_start(msg, state)

        upsert_mock.assert_awaited_once()
        call_kwargs = upsert_mock.call_args.kwargs
        assert call_kwargs["user_id"] == 12345
        state.set_state.assert_awaited_once_with(SubscribeFlow.choosing_city)

    async def test_clears_previous_state(self):
        from bot.handlers.start import cmd_start

        msg = _make_message()
        state = _make_state()

        with patch("bot.handlers.start.get_supabase_client", AsyncMock(return_value=AsyncMock())), \
             patch("bot.handlers.start.upsert_user", AsyncMock()):
            await cmd_start(msg, state)

        state.clear.assert_awaited_once()

    async def test_sends_greeting_with_keyboard(self):
        from bot.handlers.start import cmd_start

        msg = _make_message()
        state = _make_state()

        with patch("bot.handlers.start.get_supabase_client", AsyncMock(return_value=AsyncMock())), \
             patch("bot.handlers.start.upsert_user", AsyncMock()):
            await cmd_start(msg, state)

        msg.answer.assert_awaited_once()
        call_args = msg.answer.call_args
        # Greeting text should be present
        assert "Привет" in call_args.args[0] or "Привет" in str(call_args)

    async def test_upsert_failure_does_not_crash(self):
        from bot.handlers.start import cmd_start

        msg = _make_message()
        state = _make_state()

        with patch("bot.handlers.start.get_supabase_client", AsyncMock(return_value=AsyncMock())), \
             patch("bot.handlers.start.upsert_user", AsyncMock(side_effect=RuntimeError("db down"))):
            # Should not raise — upsert failure is non-fatal
            await cmd_start(msg, state)

        # State should still be set despite DB error
        state.set_state.assert_awaited_once()

    async def test_no_from_user_returns_early(self):
        from bot.handlers.start import cmd_start

        msg = _make_message()
        msg.from_user = None
        state = _make_state()

        await cmd_start(msg, state)
        msg.answer.assert_not_called()


# ---------------------------------------------------------------------------
# cb_city_selected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCbCitySelected:
    async def test_stores_city_and_sets_asking_filters(self):
        from bot.handlers.subscribe import cb_city_selected
        from bot.keyboards.cities import CityCallbackData

        cb = _make_callback(user_id=12345)
        cb_data = CityCallbackData(slug="almaty")
        state = _make_state()

        await cb_city_selected(cb, cb_data, state)

        cb.answer.assert_awaited_once()
        state.update_data.assert_awaited()
        state.set_state.assert_awaited_with(SubscribeFlow.asking_filters)
        cb.message.answer.assert_awaited_once()

    async def test_city_name_resolved_from_city_map(self):
        from bot.handlers.subscribe import cb_city_selected
        from bot.keyboards.cities import CityCallbackData

        cb = _make_callback()
        cb_data = CityCallbackData(slug="almaty")
        state = _make_state()

        await cb_city_selected(cb, cb_data, state)

        # update_data should have been called with city_name = "Алматы"
        call_kwargs = state.update_data.call_args.kwargs
        assert call_kwargs.get("city_name") == "Алматы"


# ---------------------------------------------------------------------------
# cb_filter_answer "no" path → immediate subscription
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCbFilterAnswerNo:
    async def test_no_answer_creates_subscription_immediately(self):
        from bot.handlers.subscribe import cb_filter_answer
        from bot.keyboards.subscriptions import FilterAnswerCallbackData

        cb = _make_callback(user_id=999)
        cb_data = FilterAnswerCallbackData(answer="no")
        state = _make_state(data={"city_id": "almaty", "city_name": "Алматы"})
        bot = AsyncMock()
        bot.send_message = AsyncMock()

        mock_client = AsyncMock()
        add_sub_mock = AsyncMock(return_value=MagicMock(id="sub-uuid"))

        with patch("bot.handlers.subscribe.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.handlers.subscribe.add_subscription", add_sub_mock), \
             patch("bot.handlers.subscribe.asyncio.create_task"):
            await cb_filter_answer(cb, cb_data, state, bot)

        add_sub_mock.assert_awaited_once()
        cb.answer.assert_awaited_once()

    async def test_yes_answer_sets_entering_price_min(self):
        from bot.handlers.subscribe import cb_filter_answer
        from bot.keyboards.subscriptions import FilterAnswerCallbackData

        cb = _make_callback()
        cb_data = FilterAnswerCallbackData(answer="yes")
        state = _make_state()
        bot = AsyncMock()

        await cb_filter_answer(cb, cb_data, state, bot)

        state.set_state.assert_awaited_with(SubscribeFlow.entering_price_min)
        cb.message.answer.assert_awaited_once()


# ---------------------------------------------------------------------------
# cb_skip_rooms → all-None filters path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCbSkipRooms:
    async def test_skip_rooms_finalizes_with_none_filters(self):
        from bot.handlers.subscribe import cb_skip_rooms
        from bot.keyboards.subscriptions import SkipCallbackData

        cb = _make_callback(user_id=777)
        state = _make_state(data={
            "city_id": "astana",
            "city_name": "Астана",
            "price_min": None,
            "price_max": None,
        })
        bot = AsyncMock()
        bot.send_message = AsyncMock()
        mock_client = AsyncMock()
        add_sub_mock = AsyncMock(return_value=MagicMock(id="uuid-xyz"))

        with patch("bot.handlers.subscribe.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.handlers.subscribe.add_subscription", add_sub_mock), \
             patch("bot.handlers.subscribe.asyncio.create_task"):
            await cb_skip_rooms(cb, state, bot)

        add_sub_mock.assert_awaited_once()
        call_kwargs = add_sub_mock.call_args.kwargs
        assert call_kwargs["rooms"] is None
        assert call_kwargs["price_min"] is None
        assert call_kwargs["price_max"] is None


# ---------------------------------------------------------------------------
# cmd_my_subscriptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCmdMySubscriptions:
    async def test_no_subscriptions_shows_start_prompt(self):
        from bot.handlers.subscriptions import cmd_my_subscriptions

        msg = _make_message()
        mock_client = AsyncMock()

        with patch("bot.handlers.subscriptions.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.handlers.subscriptions.get_user_subscriptions", AsyncMock(return_value=[])):
            await cmd_my_subscriptions(msg)

        msg.answer.assert_awaited_once()
        response_text = msg.answer.call_args.args[0]
        assert "/start" in response_text

    async def test_with_subscriptions_shows_list(self):
        from bot.handlers.subscriptions import cmd_my_subscriptions
        from bot.db.repositories import FullSubscriptionRow

        sub = FullSubscriptionRow(
            id="sub-001",
            user_id=12345,
            city_id="almaty",
            city_name="Алматы",
            price_min=None,
            price_max=None,
            rooms=None,
            is_active=True,
        )
        msg = _make_message()
        mock_client = AsyncMock()

        with patch("bot.handlers.subscriptions.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.handlers.subscriptions.get_user_subscriptions", AsyncMock(return_value=[sub])):
            await cmd_my_subscriptions(msg)

        msg.answer.assert_awaited_once()
        response_text = msg.answer.call_args.args[0]
        assert "Алматы" in response_text

    async def test_no_from_user_returns_early(self):
        from bot.handlers.subscriptions import cmd_my_subscriptions

        msg = _make_message()
        msg.from_user = None

        await cmd_my_subscriptions(msg)
        msg.answer.assert_not_called()


# ---------------------------------------------------------------------------
# cb_unsubscribe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCbUnsubscribe:
    async def test_unsubscribe_calls_remove_subscription(self):
        from bot.handlers.subscriptions import cb_unsubscribe
        from bot.keyboards.subscriptions import UnsubscribeCallbackData

        cb = _make_callback(user_id=12345)
        cb_data = UnsubscribeCallbackData(city_id="almaty")
        mock_client = AsyncMock()
        remove_mock = AsyncMock()

        with patch("bot.handlers.subscriptions.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.handlers.subscriptions.remove_subscription", remove_mock), \
             patch("bot.handlers.subscriptions.get_user_subscriptions", AsyncMock(return_value=[])):
            await cb_unsubscribe(cb, cb_data)

        remove_mock.assert_awaited_once_with(mock_client, user_id=12345, city_id="almaty")

    async def test_unsubscribe_edits_message_after_last_sub(self):
        from bot.handlers.subscriptions import cb_unsubscribe
        from bot.keyboards.subscriptions import UnsubscribeCallbackData

        cb = _make_callback(user_id=12345)
        cb_data = UnsubscribeCallbackData(city_id="almaty")
        mock_client = AsyncMock()

        with patch("bot.handlers.subscriptions.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.handlers.subscriptions.remove_subscription", AsyncMock()), \
             patch("bot.handlers.subscriptions.get_user_subscriptions", AsyncMock(return_value=[])):
            await cb_unsubscribe(cb, cb_data)

        cb.message.edit_text.assert_awaited()
        call_args = cb.message.edit_text.call_args
        # Message should mention cancellation
        assert "Подписка отменена" in call_args.args[0]

    async def test_unsubscribe_callback_answered(self):
        from bot.handlers.subscriptions import cb_unsubscribe
        from bot.keyboards.subscriptions import UnsubscribeCallbackData

        cb = _make_callback(user_id=12345)
        cb_data = UnsubscribeCallbackData(city_id="almaty")
        mock_client = AsyncMock()

        with patch("bot.handlers.subscriptions.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.handlers.subscriptions.remove_subscription", AsyncMock()), \
             patch("bot.handlers.subscriptions.get_user_subscriptions", AsyncMock(return_value=[])):
            await cb_unsubscribe(cb, cb_data)

        cb.answer.assert_awaited()


# ---------------------------------------------------------------------------
# _instant_push — owner-gate regression test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestInstantPushOwnerGate:
    async def test_mark_seen_for_both_send_only_for_owner(self):
        """
        _instant_push must call mark_seen for EVERY listing (owner AND non-owner)
        but call send_listing_to_user ONLY for is_owner=True listings.

        Two listings are returned by the scraper:
          - listing_owner   : is_owner=True  → mark_seen + send
          - listing_complex : is_owner=False → mark_seen only, NOT sent
        """
        from bot.handlers.subscribe import _instant_push
        from bot.scraper.models import Listing

        listing_owner = Listing(
            id="111",
            title="1-комн. кв.",
            price=20_000_000,
            rooms=1,
            area=40.0,
            address="Алматы, ул. Тестовая 1",
            url="https://krisha.kz/a/show/111",
            city_id="almaty",
            is_owner=True,
        )
        listing_complex = Listing(
            id="222",
            title="2-комн. кв. (ЖК)",
            price=35_000_000,
            rooms=2,
            area=65.0,
            address="Алматы, ул. Тестовая 2",
            url="https://krisha.kz/a/show/222",
            city_id="almaty",
            is_owner=False,
        )

        mock_client = AsyncMock()
        mock_bot = AsyncMock()

        # Scraper mock: async context manager wrapping a scraper instance.
        mock_scraper_instance = AsyncMock()
        mock_scraper_instance.fetch_listing_ids = AsyncMock(
            return_value=[
                ("111", "https://krisha.kz/a/show/111"),
                ("222", "https://krisha.kz/a/show/222"),
            ]
        )
        mock_scraper_instance.fetch_listing_detail = AsyncMock(
            side_effect=lambda lid, cid: (
                listing_owner if lid == "111" else listing_complex
            )
        )
        mock_scraper_instance.__aenter__ = AsyncMock(return_value=mock_scraper_instance)
        mock_scraper_instance.__aexit__ = AsyncMock(return_value=False)

        mark_seen_mock = AsyncMock()
        is_seen_mock = AsyncMock(return_value=False)  # neither listing seen yet
        send_mock = AsyncMock()

        # _instant_push does a local import: `from bot.push.sender import send_listing_to_user`
        # Patching bot.push.sender.send_listing_to_user intercepts that import.
        with patch("bot.handlers.subscribe.get_supabase_client", AsyncMock(return_value=mock_client)), \
             patch("bot.handlers.subscribe.KrishaScraper", return_value=mock_scraper_instance), \
             patch("bot.handlers.subscribe.is_seen", is_seen_mock), \
             patch("bot.handlers.subscribe.mark_seen", mark_seen_mock), \
             patch("bot.push.sender.send_listing_to_user", send_mock), \
             patch("bot.handlers.subscribe.asyncio.sleep", new_callable=AsyncMock):
            await _instant_push(
                mock_bot,
                user_id=12345,
                city_id="almaty",
                price_min=None,
                price_max=None,
                rooms=None,
            )

        # mark_seen is called as: mark_seen(client, listing_id, city_id) — positional
        # args[1] is listing_id.
        marked_ids = {c.args[1] for c in mark_seen_mock.call_args_list}
        assert "111" in marked_ids, "mark_seen must be called for owner listing"
        assert "222" in marked_ids, "mark_seen must be called for non-owner listing"

        # send_listing_to_user is called as: send_listing_to_user(bot, user_id, listing)
        # args[2] is the Listing object.
        sent_listings = [c.args[2] for c in send_mock.call_args_list]
        assert any(lst.id == "111" for lst in sent_listings), "owner listing must be pushed"
        assert not any(lst.id == "222" for lst in sent_listings), "non-owner listing must NOT be pushed"
