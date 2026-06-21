"""
bot/handlers/__init__.py — Aggregates all handler routers into a single Router
that main.py includes in the Dispatcher.

Import order matters: more specific filters must be registered before general ones.
"""

from __future__ import annotations

from aiogram import Router

from bot.handlers.start import router as start_router
from bot.handlers.subscribe import router as subscribe_router
from bot.handlers.subscriptions import router as subscriptions_router

# Master router — include this in main.py's Dispatcher
main_router = Router(name="main")
main_router.include_router(start_router)
main_router.include_router(subscribe_router)
main_router.include_router(subscriptions_router)

__all__ = ["main_router"]
