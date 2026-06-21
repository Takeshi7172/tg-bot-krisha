# patterns.md — supabase-dba / tg-bot-krisha

- [2026-06-21] Telegram user IDs must be bigint PK — int4 max (2,147,483,647) exceeded by modern IDs. Never uuid or int4 for users.id in any Telegram bot schema.
- [2026-06-21] aiogram FSM on Railway: use Postgres-backed storage (fsm_state table); no FK from fsm_state.key to users.id — FSM key is written before users row exists (upsert race).
- [2026-06-21] seen_listings grows ~10k–17k rows/day at 12 cities, 5-min polling. Schedule pg_cron DELETE older than 30 days; do NOT implement in schema migration — document only and let backend lead choose cleanup strategy.
- [2026-06-21] city_id stores Krisha.kz city path slug (e.g. 'almaty', 'astana') — NOT a numeric id. Canonical map in bot/config.py CITY_MAP. 11 slugs; text column, no migration needed to add cities.
