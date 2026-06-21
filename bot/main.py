"""
bot/main.py — Bot entry point for tg-bot-krisha.

Startup sequence:
  1. Load settings.
  2. Pick FSM storage: RedisStorage if REDIS_URL set, else SupabaseFSMStorage.
  3. Create Bot + Dispatcher, include main_router.
  4. Launch KrishaPoller as a background asyncio task.
  5. Mode:
     - WEBHOOK_URL set → aiohttp server on PORT (production/Railway)
     - else            → dp.start_polling(bot) (development)

Graceful shutdown:
  - asyncio.CancelledError / KeyboardInterrupt → stop poller task + close Supabase client.

Rate limiter state (_last_sent) lives in push/sender.py at module level — no cleanup needed.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import signal
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher
    from bot.config import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def main() -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    from bot.config import get_settings
    from bot.db.supabase_client import close_supabase_client
    from bot.handlers import main_router
    from bot.poller.poller import KrishaPoller
    from bot.push.sender import send_listing_to_user

    settings = get_settings()

    # ------------------------------------------------------------------
    # FSM storage
    # ------------------------------------------------------------------
    if settings.REDIS_URL:
        from aiogram.fsm.storage.redis import RedisStorage  # type: ignore[import]

        storage = RedisStorage.from_url(settings.REDIS_URL)
        logger.info(
            "main: using RedisStorage for FSM (url=%s)", settings.REDIS_URL[:30]
        )
    else:
        from bot.fsm.storage import SupabaseFSMStorage

        storage = SupabaseFSMStorage()
        logger.info("main: using SupabaseFSMStorage for FSM")

    # ------------------------------------------------------------------
    # Bot + Dispatcher
    # ------------------------------------------------------------------
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)
    dp.include_router(main_router)

    # ------------------------------------------------------------------
    # Poller
    # ------------------------------------------------------------------
    poller = KrishaPoller()
    # Bind the bot instance into the push_callback so it matches PushCallback signature:
    #   async def push_callback(user_id: int, listing: Listing) -> None
    push_callback = functools.partial(send_listing_to_user, bot)
    poller_task = asyncio.create_task(
        poller.start(push_callback=push_callback),
        name="krisha_poller",
    )

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    try:
        if settings.WEBHOOK_URL:
            await _run_webhook(bot, dp, settings, poller_task)
        else:
            await _run_polling(bot, dp, poller_task)
    finally:
        logger.info("main: shutting down")
        poller.stop()
        poller_task.cancel()
        try:
            await poller_task
        except (asyncio.CancelledError, Exception):
            pass
        await close_supabase_client()
        await bot.session.close()
        logger.info("main: shutdown complete")


async def _run_polling(bot: "Bot", dp: "Dispatcher", poller_task: asyncio.Task) -> None:
    """Development mode: long-polling."""
    logger.info("main: starting in POLLING mode")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except asyncio.CancelledError:
        pass
    finally:
        poller_task.cancel()


async def _run_webhook(
    bot: "Bot",
    dp: "Dispatcher",
    settings: "Settings",  # type: ignore[name-defined]
    poller_task: asyncio.Task,
) -> None:
    """Production mode: aiohttp webhook server on settings.PORT."""
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    logger.info(
        "main: starting in WEBHOOK mode url=%s port=%d",
        settings.WEBHOOK_URL,
        settings.PORT,
    )

    # Register webhook with Telegram
    await bot.set_webhook(
        url=settings.WEBHOOK_URL,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=True,
    )

    app = web.Application()

    # /healthz — Railway health probe (no auth)
    async def healthz(_request: web.Request) -> web.Response:
        return web.Response(text="ok", status=200)

    app.router.add_get("/healthz", healthz)

    # Webhook handler at /webhook
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.PORT)
    await site.start()
    logger.info("main: aiohttp webhook server listening on 0.0.0.0:%d", settings.PORT)

    # Keep running until cancelled
    try:
        # Wait forever (or until poller_task fails, which would mean the bot should restart)
        await asyncio.gather(poller_task)
    except asyncio.CancelledError:
        pass
    finally:
        await bot.delete_webhook()
        await runner.cleanup()


def _setup_signal_handlers() -> None:
    """
    Install SIGTERM handler so Railway graceful shutdown works.
    SIGTERM → cancel the main task → finally block runs shutdown.
    """
    loop = asyncio.get_event_loop()

    def _handle_sigterm() -> None:
        logger.info("main: SIGTERM received — cancelling main task")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("main: interrupted by user")
