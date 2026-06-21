"""
bot/keyboards/cities.py — Inline keyboard for city selection.

Displays all cities from CITY_MAP in a 2-column grid.
Callback data is a typed CityCallbackData object carrying the city SLUG
(e.g. "almaty") — matches CITY_MAP keys and DB subscriptions.city_id.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import CITY_MAP


class CityCallbackData(CallbackData, prefix="city"):
    """Callback data for city selection.

    Attributes
    ----------
    slug : str
        Krisha city slug (e.g. 'almaty'). Matches CITY_MAP keys.
    """

    slug: str


def build_city_keyboard() -> InlineKeyboardMarkup:
    """
    Build a 2-column inline keyboard with all cities from CITY_MAP.

    Each button label is the human-readable city name (Russian/Kazakh).
    Callback data carries the slug for DB operations.

    Returns
    -------
    InlineKeyboardMarkup
        Ready-to-send keyboard.
    """
    builder = InlineKeyboardBuilder()
    for slug, display_name in CITY_MAP.items():
        builder.button(
            text=display_name,
            callback_data=CityCallbackData(slug=slug),
        )
    # 2 columns
    builder.adjust(2)
    return builder.as_markup()
