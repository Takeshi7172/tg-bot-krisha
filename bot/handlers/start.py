"""
bot/handlers/start.py — /start and /help command handlers.

/start:
  1. Upsert user in DB (stores username, first_name, language_code).
  2. Send greeting with city keyboard — user can immediately pick a city to subscribe.

/help:
  Show available commands.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.db.repositories import upsert_user
from bot.db.supabase_client import get_supabase_client
from bot.keyboards.cities import build_city_keyboard
from bot.fsm.states import SubscribeFlow

logger = logging.getLogger(__name__)
router = Router(name="start")

_GREETING = (
    "Привет! Я слежу за новыми квартирами от хозяев на krisha.kz.\n\n"
    "Выбери город, чтобы начать получать уведомления:"
)

_HELP_TEXT = (
    "Доступные команды:\n\n"
    "/start — начало работы, выбор города\n"
    "/my_subscriptions — список активных подписок\n"
    "/help — эта справка\n\n"
    "Я отправляю уведомления только о квартирах от хозяев (без агентств)."
)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    """
    Handle /start. Upsert user, clear any lingering FSM state, show city picker.
    """
    if message.from_user is None:
        return

    # Clear any previous FSM state so user starts fresh
    await state.clear()

    # Upsert user
    try:
        client = await get_supabase_client()
        await upsert_user(
            client,
            user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            language_code=message.from_user.language_code,
        )
    except Exception as exc:
        logger.error(
            "start: upsert_user failed user_id=%d: %s", message.from_user.id, exc
        )
        # Non-fatal — continue to show the greeting

    await state.set_state(SubscribeFlow.choosing_city)
    await message.answer(_GREETING, reply_markup=build_city_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help — show command list."""
    await message.answer(_HELP_TEXT)
