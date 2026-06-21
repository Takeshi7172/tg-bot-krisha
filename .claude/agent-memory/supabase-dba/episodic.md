# episodic.md — supabase-dba / tg-bot-krisha

## 2026-06-21 — Greenfield initial schema

Situation: Wave 1 of a greenfield Telegram bot (tg-bot-krisha). No existing Supabase project, no existing migrations. Brief specified 4 tables with exact column definitions and index requirements.

Actions: Created supabase/migrations/20260621_001_initial_schema.sql with all 4 tables (users, subscriptions, seen_listings, fsm_state), 16 RLS policies (service_role only), 4 indexes (including a partial index on subscriptions.city_id WHERE is_active), updated_at triggers on users and fsm_state, and full rollback SQL. Created docs/schema.md with ER diagram, common query patterns, Railway pooler connection note, and pg_cron cleanup documentation.

Outcome: STATUS DONE. Two files written. No live Supabase project — migration ready for `supabase db push` once project is linked.

Lesson: Telegram bot schemas have two non-obvious constraints — bigint PK for users (not uuid/int4) and no FK from FSM state to users (aiogram writes FSM before user upsert). Both must be caught at schema design time, not after.
