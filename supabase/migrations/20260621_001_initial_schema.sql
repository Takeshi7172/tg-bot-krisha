-- Migration: 20260621_001_initial_schema.sql
-- Purpose: Initial schema for tg-bot-krisha Telegram bot
--          Tables: users, subscriptions, seen_listings, fsm_state
-- Reversible: YES — rollback at bottom of file
-- Note: No native ENUM types — VARCHAR+CHECK used for SQLAlchemy (asyncpg)
--       compatibility with transaction-mode pooler (port 6543).

-- ============================================================
-- 1. TABLES
-- ============================================================

-- ----------------------------------------------------------
-- users
-- Stores Telegram users who interact with the bot.
-- users.id is Telegram user_id (bigint). Modern Telegram IDs
-- exceed int4 max (2,147,483,647) — bigint is mandatory.
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
  id            bigint      PRIMARY KEY,            -- Telegram user_id
  username      text,                               -- @handle, nullable
  first_name    text,                               -- display name
  language_code text,                               -- e.g. 'ru', 'kk', 'en'
  is_active     boolean     NOT NULL DEFAULT true,  -- false when user blocks bot
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  users IS 'Telegram users registered with the bot.';
COMMENT ON COLUMN users.id IS 'Telegram user_id — bigint required; modern IDs exceed int4 range.';
COMMENT ON COLUMN users.is_active IS 'Set to false when bot receives a "user blocked bot" error from Telegram API.';

-- ----------------------------------------------------------
-- subscriptions
-- One subscription = one (user, city) pair with optional
-- price/room filters. A user may subscribe to multiple cities.
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscriptions (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  city_id     text        NOT NULL,   -- krisha city path slug, e.g. 'almaty', 'astana' (matches CITY_MAP in bot/config.py)
  city_name   text        NOT NULL,   -- human-readable, e.g. 'Алматы'
  price_min   integer,                -- KZT, nullable = no lower bound
  price_max   integer,                -- KZT, nullable = no upper bound
  rooms       integer[],              -- e.g. {1,2,3}, nullable = any rooms
  is_active   boolean     NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT subscriptions_user_city_unique UNIQUE (user_id, city_id)
);

COMMENT ON TABLE  subscriptions IS 'User alert subscriptions — one per (user, city) pair.';
COMMENT ON COLUMN subscriptions.city_id IS 'Krisha.kz city path slug, e.g. almaty, astana. Matches CITY_MAP keys in bot/config.py.';
COMMENT ON COLUMN subscriptions.rooms IS 'Postgres integer array. NULL = no room filter. Example: {1,2,3} = 1, 2, or 3-room listings.';

-- ----------------------------------------------------------
-- seen_listings
-- Deduplication table. Tracks which listing IDs have already
-- been sent to subscribers for a given city.
--
-- GROWTH NOTE: At 12 cities polling every 5 min with ~60
-- new listings/poll, this table grows ~864 k rows/day.
-- Use pg_cron to delete rows older than SEEN_LISTINGS_TTL_DAYS
-- (recommended default: 30 days). pg_cron NOT implemented here
-- — document only. Cleanup query:
--   DELETE FROM seen_listings
--     WHERE first_seen_at < now() - interval '30 days';
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS seen_listings (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  listing_id    text        NOT NULL,   -- krisha listing id (string)
  city_id       text        NOT NULL,   -- krisha city path slug (matches CITY_MAP in bot/config.py)
  first_seen_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT seen_listings_listing_city_unique UNIQUE (listing_id, city_id)
);

COMMENT ON TABLE  seen_listings IS 'Deduplication store. Prevents resending already-notified listings. Grows ~864k rows/day across 12 cities — schedule pg_cron DELETE for rows older than TTL (recommend 30 days).';
COMMENT ON COLUMN seen_listings.listing_id IS 'Krisha.kz listing identifier as returned by their API/scraper.';
COMMENT ON COLUMN seen_listings.city_id IS 'Scoped per city — same listing_id can appear in different cities (edge case).';

-- ----------------------------------------------------------
-- fsm_state
-- Persistent FSM storage for aiogram 3 on Railway.
-- Railway deploys are stateless (ephemeral containers), so
-- aiogram FSM state must live in Postgres, not in memory.
-- Key format: 'fsm:{chat_id}:{user_id}'
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS fsm_state (
  key        text    PRIMARY KEY,              -- 'fsm:{chat_id}:{user_id}'
  state      text,                             -- aiogram state name, nullable (no active state)
  data       jsonb   NOT NULL DEFAULT '{}',    -- FSM context data
  updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  fsm_state IS 'Persistent aiogram FSM storage. Required because Railway containers are stateless — in-memory FSM is reset on every redeploy/restart.';
COMMENT ON COLUMN fsm_state.key IS 'Composite key: fsm:{chat_id}:{user_id}. Matches aiogram StorageKey serialization.';
COMMENT ON COLUMN fsm_state.state IS 'NULL means no active FSM state (user is in default/idle state).';

-- ============================================================
-- 2. UPDATED_AT TRIGGERS
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- users
DROP TRIGGER IF EXISTS set_users_updated_at ON users;
CREATE TRIGGER set_users_updated_at
  BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- fsm_state
DROP TRIGGER IF EXISTS set_fsm_state_updated_at ON fsm_state;
CREATE TRIGGER set_fsm_state_updated_at
  BEFORE UPDATE ON fsm_state
  FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================
-- 3. INDEXES
-- ============================================================

-- seen_listings: city_id for filtering by city during poller dedup scan
CREATE INDEX IF NOT EXISTS idx_seen_listings_city_id
  ON seen_listings (city_id);

-- seen_listings: the UNIQUE constraint on (listing_id, city_id) already
-- creates a unique B-tree index — covers the primary dedup lookup pattern:
--   SELECT 1 FROM seen_listings WHERE listing_id = $1 AND city_id = $2

-- subscriptions: user_id for fetching all subscriptions for a user
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id
  ON subscriptions (user_id);

-- subscriptions: partial index for active cities — used by poller to
-- determine which city_ids need fetching (only cities with active subs)
CREATE INDEX IF NOT EXISTS idx_subscriptions_active_city_id
  ON subscriptions (city_id)
  WHERE is_active = true;

-- ============================================================
-- 4. ROW LEVEL SECURITY
-- ============================================================
-- The bot backend runs with service_role key (server-to-server).
-- service_role bypasses RLS by default in Supabase.
-- RLS is enabled on all tables so that anon/authenticated Supabase
-- JS clients (if ever added — e.g. an admin dashboard) cannot
-- directly access data without explicit policies.
--
-- Policy: service_role can do everything (USING (true) covers all ops).
-- No policies granted to anon or authenticated roles at this stage —
-- add only when a client-facing UI is introduced.

-- users
ALTER TABLE users ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role can select users"
  ON users FOR SELECT
  TO service_role
  USING (true);

CREATE POLICY "service_role can insert users"
  ON users FOR INSERT
  TO service_role
  WITH CHECK (true);

CREATE POLICY "service_role can update users"
  ON users FOR UPDATE
  TO service_role
  USING (true)
  WITH CHECK (true);

CREATE POLICY "service_role can delete users"
  ON users FOR DELETE
  TO service_role
  USING (true);

-- subscriptions
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role can select subscriptions"
  ON subscriptions FOR SELECT
  TO service_role
  USING (true);

CREATE POLICY "service_role can insert subscriptions"
  ON subscriptions FOR INSERT
  TO service_role
  WITH CHECK (true);

CREATE POLICY "service_role can update subscriptions"
  ON subscriptions FOR UPDATE
  TO service_role
  USING (true)
  WITH CHECK (true);

CREATE POLICY "service_role can delete subscriptions"
  ON subscriptions FOR DELETE
  TO service_role
  USING (true);

-- seen_listings
ALTER TABLE seen_listings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role can select seen_listings"
  ON seen_listings FOR SELECT
  TO service_role
  USING (true);

CREATE POLICY "service_role can insert seen_listings"
  ON seen_listings FOR INSERT
  TO service_role
  WITH CHECK (true);

CREATE POLICY "service_role can update seen_listings"
  ON seen_listings FOR UPDATE
  TO service_role
  USING (true)
  WITH CHECK (true);

CREATE POLICY "service_role can delete seen_listings"
  ON seen_listings FOR DELETE
  TO service_role
  USING (true);

-- fsm_state
ALTER TABLE fsm_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role can select fsm_state"
  ON fsm_state FOR SELECT
  TO service_role
  USING (true);

CREATE POLICY "service_role can insert fsm_state"
  ON fsm_state FOR INSERT
  TO service_role
  WITH CHECK (true);

CREATE POLICY "service_role can update fsm_state"
  ON fsm_state FOR UPDATE
  TO service_role
  USING (true)
  WITH CHECK (true);

CREATE POLICY "service_role can delete fsm_state"
  ON fsm_state FOR DELETE
  TO service_role
  USING (true);

-- ============================================================
-- ROLLBACK (reverse migration — run manually if needed)
-- ============================================================
-- DROP TRIGGER IF EXISTS set_fsm_state_updated_at ON fsm_state;
-- DROP TRIGGER IF EXISTS set_users_updated_at ON users;
-- DROP FUNCTION IF EXISTS update_updated_at_column();
-- DROP TABLE IF EXISTS fsm_state;
-- DROP TABLE IF EXISTS seen_listings;
-- DROP TABLE IF EXISTS subscriptions;
-- DROP TABLE IF EXISTS users;
