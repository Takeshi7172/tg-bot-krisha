"""
bot/fsm/states.py — aiogram 3 FSMContext state groups for tg-bot-krisha.

State groups:
  SubscribeFlow — the city subscription wizard.

Entry point:   choosing_city (shown after /start or when user picks "Подписаться")
Exit point:    handler calls state.clear() after add_subscription succeeds or user cancels.
"""

from aiogram.fsm.state import State, StatesGroup


class SubscribeFlow(StatesGroup):
    """
    Linear wizard: choose city → optional filters → confirm.

    State transitions:
      choosing_city
          ↓  (city selected via inline button)
      asking_filters          ← "Добавить фильтры? Да / Нет"
          ↓ Yes               ↓ No → add_subscription → clear
      entering_price_min      (skippable)
          ↓
      entering_price_max      (skippable)
          ↓
      entering_rooms          (skippable)
          ↓
      add_subscription → clear
    """

    choosing_city = State()
    asking_filters = State()
    entering_price_min = State()
    entering_price_max = State()
    entering_rooms = State()
