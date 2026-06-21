"""
bot/handlers/subscriptions.py — View and manage active subscriptions.

/my_subscriptions:
  Lists all active subscriptions for the user with inline [Отписаться] buttons.
  If no active subscriptions exist, prompts to /start.

Unsubscribe callback:
  UnsubscribeCallbackData → remove_subscription → edit message to confirm.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.db.repositories import get_user_subscriptions, remove_subscription
from bot.db.supabase_client import get_supabase_client
from bot.keyboards.subscriptions import (
    UnsubscribeCallbackData,
    build_unsubscribe_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="subscriptions")


@router.message(Command("my_subscriptions"))
async def cmd_my_subscriptions(message: Message) -> None:
    """
    Show all active subscriptions with [Отписаться] buttons.
    """
    if message.from_user is None:
        return

    user_id = message.from_user.id

    try:
        client = await get_supabase_client()
        all_subs = await get_user_subscriptions(client, user_id)
    except Exception as exc:
        logger.error(
            "subscriptions: get_user_subscriptions failed user_id=%d: %s", user_id, exc
        )
        await message.answer("Не удалось загрузить подписки. Попробуйте позже.")
        return

    active_subs = [s for s in all_subs if s.is_active]

    if not active_subs:
        await message.answer(
            "У вас нет активных подписок.\n\nЧтобы подписаться на город, введите /start"
        )
        return

    # Build subscription summary text
    lines: list[str] = ["Ваши активные подписки:\n"]
    for sub in active_subs:
        line = f"• {sub.city_name}"
        filters: list[str] = []
        if sub.price_min is not None:
            filters.append(f"от {sub.price_min:,} ₸".replace(",", " "))
        if sub.price_max is not None:
            filters.append(f"до {sub.price_max:,} ₸".replace(",", " "))
        if sub.rooms:
            rooms_str = ", ".join(str(r) for r in sub.rooms)
            filters.append(f"комнат: {rooms_str}")
        if filters:
            line += " (" + ", ".join(filters) + ")"
        lines.append(line)

    text = "\n".join(lines)
    keyboard = build_unsubscribe_keyboard(active_subs)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(UnsubscribeCallbackData.filter())
async def cb_unsubscribe(
    callback: CallbackQuery,
    callback_data: UnsubscribeCallbackData,
) -> None:
    """
    Handle [Отписаться] button. Deactivates the subscription and updates the message.
    """
    await callback.answer()

    if callback.from_user is None:
        return

    user_id = callback.from_user.id
    city_id = callback_data.city_id

    try:
        client = await get_supabase_client()
        await remove_subscription(client, user_id=user_id, city_id=city_id)
    except Exception as exc:
        logger.error(
            "subscriptions: remove_subscription failed user_id=%d city=%s: %s",
            user_id,
            city_id,
            exc,
        )
        await callback.answer(
            "Не удалось отписаться. Попробуйте позже.", show_alert=True
        )
        return

    # Refresh the subscription list after unsubscribe
    try:
        client = await get_supabase_client()
        all_subs = await get_user_subscriptions(client, user_id)
        active_subs = [s for s in all_subs if s.is_active]
    except Exception as exc:
        logger.error(
            "subscriptions: get_user_subscriptions after unsub failed user_id=%d: %s",
            user_id,
            exc,
        )
        # Best-effort: just edit the message to a success notice
        try:
            await callback.message.edit_text("Подписка отменена.")
        except Exception:  # noqa: BLE001
            pass
        return

    if not active_subs:
        try:
            await callback.message.edit_text(
                "Подписка отменена.\n\nУ вас больше нет активных подписок.\n"
                "Чтобы подписаться снова, введите /start",
                reply_markup=None,
            )
        except Exception:  # noqa: BLE001
            pass
        return

    # Rebuild the list with remaining subscriptions
    lines: list[str] = ["Ваши активные подписки:\n"]
    for sub in active_subs:
        line = f"• {sub.city_name}"
        filters: list[str] = []
        if sub.price_min is not None:
            filters.append(f"от {sub.price_min:,} ₸".replace(",", " "))
        if sub.price_max is not None:
            filters.append(f"до {sub.price_max:,} ₸".replace(",", " "))
        if sub.rooms:
            rooms_str = ", ".join(str(r) for r in sub.rooms)
            filters.append(f"комнат: {rooms_str}")
        if filters:
            line += " (" + ", ".join(filters) + ")"
        lines.append(line)

    text = "\n".join(lines)
    keyboard = build_unsubscribe_keyboard(active_subs)

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:  # noqa: BLE001
        # Message too old to edit — send new
        await callback.message.answer(text, reply_markup=keyboard)
