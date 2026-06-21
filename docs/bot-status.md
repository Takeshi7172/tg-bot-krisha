# bot-status.md — Wave 3 Implementation Status

STATUS: DONE

## Files Created

### FSM
- `bot/fsm/__init__.py` — package init
- `bot/fsm/states.py` — SubscribeFlow StatesGroup: choosing_city, asking_filters, entering_price_min, entering_price_max, entering_rooms
- `bot/fsm/storage.py` — SupabaseFSMStorage(BaseStorage) backed by fsm_state table. Key format: fsm:{chat_id}:{user_id}. set_state/get_state/set_data/get_data/update_data/close implemented. Read-then-write pattern to preserve data column on set_state (supabase-py upsert replaces whole row).

### Keyboards
- `bot/keyboards/__init__.py` — package init
- `bot/keyboards/cities.py` — build_city_keyboard() 2-column InlineKeyboardMarkup from CITY_MAP. CityCallbackData(prefix="city", slug=str).
- `bot/keyboards/subscriptions.py` — FilterAnswerCallbackData(answer), SkipCallbackData(step), UnsubscribeCallbackData(city_id). build_yes_no_keyboard(), build_skip_keyboard(step), build_unsubscribe_keyboard(subs).

### Push layer
- `bot/push/__init__.py` — package init
- `bot/push/formatter.py` — format_listing(listing) -> (caption, image_url). 5-line Russian caption with emoji, thousands-separator price, 1024-char cap.
- `bot/push/sender.py` — send_listing_to_user(bot, user_id, listing). Global 30 msg/s Semaphore + asyncio.sleep(0.033). Per-chat 1s gap via _last_sent dict. TelegramForbiddenError → set_user_inactive. TelegramRetryAfter → sleep + retry once. Never raises.

### Handlers
- `bot/handlers/__init__.py` — main_router aggregating start_router + subscribe_router + subscriptions_router
- `bot/handlers/start.py` — /start (upsert_user, clear state, set choosing_city, city keyboard), /help
- `bot/handlers/subscribe.py` — full wizard: city selection → filter prompt → price_min → price_max → rooms → add_subscription + confirmation + asyncio.create_task(_instant_push). Each numeric step skippable via SkipCallbackData inline button. /start mid-flow handlers clear and restart.
- `bot/handlers/subscriptions.py` — /my_subscriptions (list active subs with filters), UnsubscribeCallbackData callback → remove_subscription → edit message with refreshed list.

### Entry point
- `bot/main.py` — Hybrid mode: WEBHOOK_URL set → aiohttp on PORT with /webhook (SimpleRequestHandler) + /healthz; else dp.start_polling(bot). FSM: REDIS_URL set → RedisStorage.from_url, else SupabaseFSMStorage. Poller as asyncio.create_task. push_callback = functools.partial(send_listing_to_user, bot). Graceful shutdown: poller.stop() + task.cancel() + close_supabase_client() + bot.session.close(). SIGTERM handler on non-Windows.

### Config
- `.env` — local dev secrets (BOT_TOKEN=<provided>, SUPABASE_URL/KEY empty for user to fill)

## Key decisions

- FSM key: `fsm:{chat_id}:{user_id}` (chat_id first per docs/schema.md)
- SupabaseFSMStorage uses read-then-write (not raw upsert) to preserve `data` when `set_state` is called and vice versa — supabase-py v2 upsert replaces the full row.
- Instant push after subscribe: `asyncio.create_task` so confirmation message is delivered immediately; push runs in background. Applied same price/rooms filters as poller.
- City keyboard: imports `InlineKeyboardMarkup` at module level (not inside function) — avoids F401 from ruff.
- Bot injection in handlers: aiogram 3 DI — `bot: Bot` in handler signature works automatically.
- Unsubscribe is soft-delete (is_active=False) per repositories.py — seen_listings dedup remains effective on resubscribe.

## Ruff verification
- ruff check: All checks passed
- ruff format --check: 14 files already formatted

## Verification checklist
- [x] All 14 new .py files AST-parse clean (py -c ast.parse)
- [x] ruff check passes (0 errors)
- [x] ruff format --check passes (14 files clean)
- [x] No MemoryStorage anywhere
- [x] All callback handlers call await callback.answer()
- [x] No blocking I/O in handlers (all await)
- [x] push_callback bound with functools.partial(send_listing_to_user, bot)
- [x] TelegramForbiddenError → set_user_inactive
- [x] WEBHOOK_URL → aiohttp + /healthz; else polling
- [x] /start clears FSM state before setting choosing_city
- [ ] End-to-end test: requires live BOT_TOKEN + Supabase (user fills SUPABASE_URL/KEY)
