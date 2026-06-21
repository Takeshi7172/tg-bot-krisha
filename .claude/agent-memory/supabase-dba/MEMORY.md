# supabase-dba Memory — tg-bot-krisha

- [Greenfield schema — 2026-06-21](patterns.md) — Initial schema applied; 4 tables, service_role-only RLS, no native ENUMs
- [FSM state key format](patterns.md) — aiogram key: `fsm:{chat_id}:{user_id}`; no FK on users (race condition risk)
