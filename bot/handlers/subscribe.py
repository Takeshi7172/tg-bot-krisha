"""
bot/handlers/subscribe.py — City subscription wizard.

Flow:
  1. User selects a city via inline keyboard (CityCallbackData).
  2. Bot asks "Добавить фильтры?" with Yes/No buttons.
  3a. No → add_subscription → confirm → instant scrape push.
  3b. Yes → entering_price_min (skippable) → entering_price_max (skippable)
          → entering_rooms (skippable) → add_subscription → confirm → instant scrape push.

After subscribing: the bot does ONE immediate scrape of the chosen city via
KrishaScraper and pushes any unseen listings to this user (instant value delivery).

All DB operations go through bot/db/repositories.py.
No raw supabase calls in this file.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import CITY_MAP
from bot.db.repositories import (
    add_subscription,
    is_seen,
    mark_seen,
)
from bot.db.supabase_client import get_supabase_client
from bot.fsm.states import SubscribeFlow
from bot.keyboards.cities import CityCallbackData, build_city_keyboard
from bot.keyboards.subscriptions import (
    FilterAnswerCallbackData,
    SkipCallbackData,
    build_skip_keyboard,
    build_yes_no_keyboard,
)
from bot.scraper.krisha_scraper import KrishaScraper

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)
router = Router(name="subscribe")


# ---------------------------------------------------------------------------
# Entry: city selected
# ---------------------------------------------------------------------------


@router.callback_query(SubscribeFlow.choosing_city, CityCallbackData.filter())
async def cb_city_selected(
    callback: CallbackQuery,
    callback_data: CityCallbackData,
    state: FSMContext,
) -> None:
    """
    User tapped a city button. Store the choice and ask about filters.
    """
    await callback.answer()

    city_slug = callback_data.slug
    city_name = CITY_MAP.get(city_slug, city_slug)

    await state.update_data(city_id=city_slug, city_name=city_name)
    await state.set_state(SubscribeFlow.asking_filters)

    await callback.message.answer(
        f"Выбран город: {city_name}\n\n"
        "Хотите добавить фильтры по цене и количеству комнат?",
        reply_markup=build_yes_no_keyboard(),
    )


# ---------------------------------------------------------------------------
# Filter answer: Yes / No
# ---------------------------------------------------------------------------


@router.callback_query(SubscribeFlow.asking_filters, FilterAnswerCallbackData.filter())
async def cb_filter_answer(
    callback: CallbackQuery,
    callback_data: FilterAnswerCallbackData,
    state: FSMContext,
    bot: "Bot",
) -> None:
    """
    User answered Yes or No to "Добавить фильтры?"
    """
    await callback.answer()

    if callback_data.answer == "no":
        # No filters — subscribe immediately
        await _finalize_subscription(
            callback.message, state, bot, callback.from_user.id
        )
        return

    # Yes — start collecting filters
    await state.set_state(SubscribeFlow.entering_price_min)
    await callback.message.answer(
        "Введите минимальную цену в тенге (например: 15000000).\n"
        "Или нажмите кнопку ниже, чтобы пропустить.",
        reply_markup=build_skip_keyboard("price_min"),
    )


# ---------------------------------------------------------------------------
# Price min
# ---------------------------------------------------------------------------


@router.callback_query(
    SubscribeFlow.entering_price_min, SkipCallbackData.filter(F.step == "price_min")
)
async def cb_skip_price_min(
    callback: CallbackQuery,
    state: FSMContext,
    bot: "Bot",
) -> None:
    await callback.answer()
    await state.update_data(price_min=None)
    await state.set_state(SubscribeFlow.entering_price_max)
    await callback.message.answer(
        "Введите максимальную цену в тенге (например: 50000000).\n"
        "Или нажмите кнопку ниже, чтобы пропустить.",
        reply_markup=build_skip_keyboard("price_max"),
    )


@router.message(SubscribeFlow.entering_price_min)
async def msg_price_min(message: Message, state: FSMContext, bot: "Bot") -> None:
    """Parse price_min from user input."""
    text = (message.text or "").strip()
    price_min = _parse_price(text)
    if price_min is None:
        await message.answer(
            "Не могу распознать число. Введите цену цифрами (например: 15000000) "
            "или нажмите «Пропустить».",
            reply_markup=build_skip_keyboard("price_min"),
        )
        return

    await state.update_data(price_min=price_min)
    await state.set_state(SubscribeFlow.entering_price_max)
    await message.answer(
        f"Минимальная цена: {price_min:,} ₸".replace(",", " ") + "\n\n"
        "Введите максимальную цену в тенге (например: 50000000).\n"
        "Или нажмите кнопку ниже, чтобы пропустить.",
        reply_markup=build_skip_keyboard("price_max"),
    )


# ---------------------------------------------------------------------------
# Price max
# ---------------------------------------------------------------------------


@router.callback_query(
    SubscribeFlow.entering_price_max, SkipCallbackData.filter(F.step == "price_max")
)
async def cb_skip_price_max(
    callback: CallbackQuery,
    state: FSMContext,
    bot: "Bot",
) -> None:
    await callback.answer()
    await state.update_data(price_max=None)
    await state.set_state(SubscribeFlow.entering_rooms)
    await callback.message.answer(
        "Введите количество комнат через запятую (например: 1,2 или 3).\n"
        "Или нажмите кнопку ниже, чтобы пропустить.",
        reply_markup=build_skip_keyboard("rooms"),
    )


@router.message(SubscribeFlow.entering_price_max)
async def msg_price_max(message: Message, state: FSMContext, bot: "Bot") -> None:
    """Parse price_max from user input."""
    text = (message.text or "").strip()
    price_max = _parse_price(text)
    if price_max is None:
        await message.answer(
            "Не могу распознать число. Введите цену цифрами (например: 50000000) "
            "или нажмите «Пропустить».",
            reply_markup=build_skip_keyboard("price_max"),
        )
        return

    await state.update_data(price_max=price_max)
    await state.set_state(SubscribeFlow.entering_rooms)
    await message.answer(
        f"Максимальная цена: {price_max:,} ₸".replace(",", " ") + "\n\n"
        "Введите количество комнат через запятую (например: 1,2 или 3).\n"
        "Или нажмите кнопку ниже, чтобы пропустить.",
        reply_markup=build_skip_keyboard("rooms"),
    )


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------


@router.callback_query(
    SubscribeFlow.entering_rooms, SkipCallbackData.filter(F.step == "rooms")
)
async def cb_skip_rooms(
    callback: CallbackQuery,
    state: FSMContext,
    bot: "Bot",
) -> None:
    await callback.answer()
    await state.update_data(rooms=None)
    await _finalize_subscription(callback.message, state, bot, callback.from_user.id)


@router.message(SubscribeFlow.entering_rooms)
async def msg_rooms(message: Message, state: FSMContext, bot: "Bot") -> None:
    """Parse rooms list from user input."""
    text = (message.text or "").strip()
    rooms = _parse_rooms(text)
    if rooms is None:
        await message.answer(
            "Не могу распознать комнаты. Введите числа через запятую (например: 1,2,3) "
            "или нажмите «Пропустить».",
            reply_markup=build_skip_keyboard("rooms"),
        )
        return

    await state.update_data(rooms=rooms)
    await _finalize_subscription(message, state, bot, message.from_user.id)


# ---------------------------------------------------------------------------
# Finalize subscription
# ---------------------------------------------------------------------------


async def _finalize_subscription(
    message: "Message",
    state: FSMContext,
    bot: "Bot",
    user_id: int,
) -> None:
    """
    Read collected FSM data, call add_subscription, clear state,
    confirm to user, then run instant scrape push.
    """
    data = await state.get_data()
    city_id: str = data.get("city_id", "")
    city_name: str = data.get("city_name", city_id)
    price_min: int | None = data.get("price_min")
    price_max: int | None = data.get("price_max")
    rooms: list[int] | None = data.get("rooms")

    if not city_id:
        await state.clear()
        await message.answer("Что-то пошло не так. Начните заново: /start")
        return

    # Save subscription
    try:
        client = await get_supabase_client()
        await add_subscription(
            client,
            user_id=user_id,
            city_id=city_id,
            city_name=city_name,
            price_min=price_min,
            price_max=price_max,
            rooms=rooms,
        )
    except Exception as exc:
        logger.error(
            "subscribe: add_subscription failed user_id=%d city=%s: %s",
            user_id,
            city_id,
            exc,
        )
        await state.clear()
        await message.answer("Не удалось сохранить подписку. Попробуйте позже.")
        return

    # Clear FSM state
    await state.clear()

    # Build confirmation text
    filter_lines: list[str] = []
    if price_min is not None:
        filter_lines.append(f"  от {price_min:,} ₸".replace(",", " "))
    if price_max is not None:
        filter_lines.append(f"  до {price_max:,} ₸".replace(",", " "))
    if rooms:
        rooms_str = ", ".join(str(r) for r in rooms)
        filter_lines.append(f"  комнат: {rooms_str}")

    confirm_text = f"Подписка на {city_name} оформлена!"
    if filter_lines:
        confirm_text += "\n\nФильтры:\n" + "\n".join(filter_lines)
    confirm_text += "\n\nЯ пришлю уведомления о новых квартирах от хозяев."

    await message.answer(confirm_text)

    # Instant scrape: push any recent unseen listings from this city to this user
    # Run in the background so the confirmation arrives immediately
    asyncio.create_task(
        _instant_push(bot, user_id, city_id, price_min, price_max, rooms),
        name=f"instant_push_{user_id}_{city_id}",
    )


async def _instant_push(
    bot: "Bot",
    user_id: int,
    city_id: str,
    price_min: int | None,
    price_max: int | None,
    rooms: list[int] | None,
) -> None:
    """
    Do one immediate scrape of `city_id` and push unseen listings to `user_id`.
    Applies the same filters as the poller would.

    Imports send_listing_to_user here to avoid circular imports at module level.
    """
    # Local import to avoid circular at module level
    from bot.push.sender import send_listing_to_user  # noqa: PLC0415
    from bot.scraper.models import Listing  # noqa: PLC0415, F401 (used for type hints in _matches)

    logger.info(
        "subscribe: instant_push starting user_id=%d city=%s",
        user_id,
        city_id,
    )

    try:
        client = await get_supabase_client()
        async with KrishaScraper() as scraper:
            listing_id_pairs = await scraper.fetch_listing_ids(city_id, page=1)
            pushed = 0
            for listing_id, _url in listing_id_pairs:
                try:
                    already_seen = await is_seen(client, listing_id, city_id)
                except Exception as exc:
                    logger.warning(
                        "subscribe: instant_push is_seen failed listing_id=%s: %s — skipping",
                        listing_id,
                        exc,
                    )
                    continue

                if already_seen:
                    continue

                # Fetch detail
                listing = await scraper.fetch_listing_detail(listing_id, city_id)

                # Mark seen before sending (even for non-owners, so they are
                # not re-fetched on the next cycle — mirrors poller.py:251).
                try:
                    await mark_seen(client, listing_id, city_id)
                except Exception as exc:
                    logger.warning(
                        "subscribe: instant_push mark_seen failed listing_id=%s: %s",
                        listing_id,
                        exc,
                    )

                # Skip non-owners (developers/agencies).
                # das[who]=1 includes userType="complex" (ЖК/developers) — only
                # "owner" (private person) qualifies as "от хозяев".
                if not listing.is_owner:
                    continue

                # Apply filters
                if not _matches_filters(listing, price_min, price_max, rooms):
                    continue

                await send_listing_to_user(bot, user_id, listing)
                pushed += 1

                # Small delay between instant-push items to avoid flooding
                await asyncio.sleep(1.0)

        logger.info(
            "subscribe: instant_push done user_id=%d city=%s pushed=%d",
            user_id,
            city_id,
            pushed,
        )
        if pushed == 0:
            # Let user know there are no fresh listings right now
            try:
                await bot.send_message(
                    user_id,
                    f"Пока новых объявлений в городе {CITY_MAP.get(city_id, city_id)} нет. "
                    "Пришлю, как только появятся!",
                )
            except Exception:  # noqa: BLE001
                pass

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "subscribe: instant_push failed user_id=%d city=%s: %s",
            user_id,
            city_id,
            exc,
        )


def _matches_filters(
    listing: "object",
    price_min: int | None,
    price_max: int | None,
    rooms: list[int] | None,
) -> bool:
    """Same filter logic as KrishaPoller._matches_filters, applied inline."""
    price = getattr(listing, "price", None)
    rooms_count = getattr(listing, "rooms", None)

    if price_min is not None and price is not None:
        if price < price_min:
            return False
    if price_max is not None and price is not None:
        if price > price_max:
            return False
    if rooms is not None and rooms_count is not None:
        if rooms_count not in rooms:
            return False
    return True


# ---------------------------------------------------------------------------
# Cancel / re-entry guard
# ---------------------------------------------------------------------------


@router.message(Command("start"), SubscribeFlow.choosing_city)
@router.message(Command("start"), SubscribeFlow.asking_filters)
@router.message(Command("start"), SubscribeFlow.entering_price_min)
@router.message(Command("start"), SubscribeFlow.entering_price_max)
@router.message(Command("start"), SubscribeFlow.entering_rooms)
async def cmd_start_during_flow(message: Message, state: FSMContext) -> None:
    """
    User typed /start mid-flow. Clear state and re-show city keyboard.
    """
    await state.clear()
    await state.set_state(SubscribeFlow.choosing_city)
    await message.answer(
        "Выберите город для подписки:",
        reply_markup=build_city_keyboard(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_price(text: str) -> int | None:
    """
    Parse a price integer from user text. Strips spaces, commas, dots used as
    thousand separators. Returns None on failure.
    """
    clean = text.replace(" ", "").replace(",", "").replace(".", "").replace("₸", "")
    try:
        val = int(clean)
        return val if val > 0 else None
    except (ValueError, OverflowError):
        return None


def _parse_rooms(text: str) -> list[int] | None:
    """
    Parse a list of room counts from user text.
    Accepts "1,2,3" or "1 2 3" or just "2".
    Returns None on parse failure (so the caller can re-prompt).
    """
    # Normalise separators
    normalised = text.replace(" ", ",").replace(";", ",")
    parts = [p.strip() for p in normalised.split(",") if p.strip()]
    if not parts:
        return None
    try:
        result = [int(p) for p in parts]
        return result if result else None
    except ValueError:
        return None
