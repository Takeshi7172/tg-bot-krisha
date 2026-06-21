"""
bot/keyboards/subscriptions.py — Keyboards for subscription management flows.

Keyboards exported:
  build_yes_no_keyboard()          — "Да" / "Нет" inline buttons
  build_skip_keyboard()            — Single "Пропустить" inline button
  build_unsubscribe_keyboard(subs) — Per-subscription [Отписаться] buttons
  UnsubscribeCallbackData          — typed callback for unsubscribe actions
  FilterAnswerCallbackData         — typed callback for Yes/No answers
  SkipCallbackData                 — typed callback for skipping a filter step
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup
    from bot.db.repositories import FullSubscriptionRow


# ---------------------------------------------------------------------------
# Callback data types
# ---------------------------------------------------------------------------


class FilterAnswerCallbackData(CallbackData, prefix="filter_ans"):
    """User answered Yes/No to "Добавить фильтры?"."""

    answer: str  # "yes" | "no"


class SkipCallbackData(CallbackData, prefix="skip_step"):
    """User pressed "Пропустить" on a filter input step."""

    step: str  # "price_min" | "price_max" | "rooms"


class UnsubscribeCallbackData(CallbackData, prefix="unsub"):
    """User pressed [Отписаться] for a specific city."""

    city_id: str  # krisha city slug


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------


def build_yes_no_keyboard() -> "InlineKeyboardMarkup":
    """
    "Добавить фильтры по цене и комнатам?" — Yes / No inline keyboard.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Да",
        callback_data=FilterAnswerCallbackData(answer="yes"),
    )
    builder.button(
        text="Нет",
        callback_data=FilterAnswerCallbackData(answer="no"),
    )
    builder.adjust(2)
    return builder.as_markup()


def build_skip_keyboard(step: str) -> "InlineKeyboardMarkup":
    """
    Single "Пропустить" button for a filter step.

    Parameters
    ----------
    step : str
        Which step this skip button applies to: "price_min", "price_max", or "rooms".
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Пропустить",
        callback_data=SkipCallbackData(step=step),
    )
    return builder.as_markup()


def build_unsubscribe_keyboard(
    subs: "list[FullSubscriptionRow]",
) -> "InlineKeyboardMarkup":
    """
    Build one [Отписаться] button per active subscription.

    Each button shows the city name and carries the city_id (slug) in
    its callback data so the handler knows which subscription to deactivate.

    Parameters
    ----------
    subs : list[FullSubscriptionRow]
        Subscriptions to display. Only active ones are expected here, but
        the function renders all provided regardless.
    """
    builder = InlineKeyboardBuilder()
    for sub in subs:
        builder.button(
            text=f"Отписаться от {sub.city_name}",
            callback_data=UnsubscribeCallbackData(city_id=sub.city_id),
        )
    # One button per row for clarity
    builder.adjust(1)
    return builder.as_markup()
