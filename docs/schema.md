# tg-bot-krisha — Database Schema Reference

<!-- STATUS BLOCK (written first per output_path protocol) -->
<!--
STATUS: DONE
Files changed:
  - supabase/migrations/20260621_001_initial_schema.sql — full DDL: 4 tables + RLS + indexes + triggers
  - docs/schema.md — this file; human-readable reference
Tables/policies created:
  - users (4 RLS policies)
  - subscriptions (4 RLS policies)
  - seen_listings (4 RLS policies)
  - fsm_state (4 RLS policies)
Types regenerated: No (no live Supabase project yet — run after `supabase db push`)
PR: N/A
-->

Generated: 2026-06-21
Migration file: `supabase/migrations/20260621_001_initial_schema.sql`

---

## How to apply

### Option A — Supabase CLI (recommended)
```bash
supabase db push
```
Requires `supabase/config.toml` with `project_id` set. Run once a Supabase project is linked:
```bash
supabase link --project-ref <ref>
supabase db push
```

### Option B — Dashboard SQL editor
Paste the full migration file contents into the Supabase dashboard SQL editor and run.

### After applying
Regenerate TypeScript types:
```bash
supabase gen types typescript --project-id <ref> > database.types.ts
```

---

## Railway connection — CRITICAL

The Railway deployment MUST use the **transaction-mode pooler** connection string, NOT the direct connection string.

| Parameter | Value |
|-----------|-------|
| Host | `aws-1-ap-southeast-2.pooler.supabase.com` |
| Port | **6543** (transaction-mode pooler) |
| NOT | port 5432 (direct — IPv6 only, unreachable from Railway) |

Environment variable for Railway:
```
DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres
```

asyncpg additional required setting (transaction-mode pooler disables named prepared statements):
```python
# In SQLAlchemy engine creation:
connect_args={"prepared_statement_cache_size": 0}
```

No `SET` commands that persist across requests. No advisory locks that span transactions.

---

## Tables

### users

Stores every Telegram user who has interacted with the bot.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `bigint` | NOT NULL | — | **Primary key = Telegram user_id.** Bigint is mandatory — modern Telegram IDs exceed `int4` max (2,147,483,647). Never use `int4` or `uuid` here. |
| `username` | `text` | YES | NULL | Telegram @handle, without the @. Can be NULL (Telegram allows no username). |
| `first_name` | `text` | YES | NULL | Telegram display name. |
| `language_code` | `text` | YES | NULL | IETF language tag from Telegram, e.g. `'ru'`, `'kk'`, `'en'`. |
| `is_active` | `boolean` | NOT NULL | `true` | Set to `false` when Telegram returns a "bot was blocked by the user" error (403 Forbidden). Inactive users are excluded from broadcast. |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Row creation time. |
| `updated_at` | `timestamptz` | NOT NULL | `now()` | Auto-updated by trigger on any UPDATE. |

**Indexes:** none beyond primary key (lookups are always by `id`).

**Trigger:** `set_users_updated_at` — sets `updated_at = now()` on every UPDATE.

---

### subscriptions

One row per (user, city) pair. A user may have multiple subscriptions (one per city). Optional price and room filters narrow which listings trigger a notification.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `uuid` | NOT NULL | `gen_random_uuid()` | Surrogate primary key. |
| `user_id` | `bigint` | NOT NULL | — | FK → `users(id)` ON DELETE CASCADE. |
| `city_id` | `text` | NOT NULL | — | Krisha.kz city path slug (e.g. almaty, astana). Matches CITY_MAP in bot/config.py. |
| `city_name` | `text` | NOT NULL | — | Human-readable city name for display in bot messages, e.g. `'Алматы'`. |
| `price_min` | `integer` | YES | NULL | Minimum price filter in KZT. NULL = no lower bound. |
| `price_max` | `integer` | YES | NULL | Maximum price filter in KZT. NULL = no upper bound. |
| `rooms` | `integer[]` | YES | NULL | Postgres integer array. NULL = any number of rooms. Example: `{1,2,3}` = accept 1-, 2-, or 3-room listings. |
| `is_active` | `boolean` | NOT NULL | `true` | `false` = subscription paused by user. |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Row creation time. |

**Constraints:**
- `UNIQUE(user_id, city_id)` — one subscription per (user, city). Use `INSERT ... ON CONFLICT DO UPDATE` (upsert) to update filters on an existing subscription.

**Indexes:**
- `idx_subscriptions_user_id` on `(user_id)` — fetch all subs for a user in the bot handler.
- `idx_subscriptions_active_city_id` on `(city_id) WHERE is_active = true` — partial index. The poller uses `SELECT DISTINCT city_id FROM subscriptions WHERE is_active = true` to determine which cities to scrape. This index makes that query a fast index-only scan.

---

### seen_listings

Deduplication store. Prevents the bot from resending a listing that was already notified. The poller checks this table before dispatching notifications.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `id` | `uuid` | NOT NULL | `gen_random_uuid()` | Surrogate primary key. |
| `listing_id` | `text` | NOT NULL | — | Krisha.kz listing identifier as returned by their API/scraper. |
| `city_id` | `text` | NOT NULL | — | Scoped per city. The same `listing_id` can appear in different cities (rare edge case with cross-listed properties). |
| `first_seen_at` | `timestamptz` | NOT NULL | `now()` | When the listing was first observed by the poller. |

**Constraints:**
- `UNIQUE(listing_id, city_id)` — the primary dedup lookup. The unique constraint creates a B-tree index automatically; no separate index needed for this query pattern.

**Indexes:**
- `idx_seen_listings_city_id` on `(city_id)` — supports bulk city-scoped queries (e.g. "how many listings seen for city X today").
- The `UNIQUE(listing_id, city_id)` constraint index covers the hot path: `SELECT 1 FROM seen_listings WHERE listing_id = $1 AND city_id = $2`.

**Growth and cleanup:**

This table grows unbounded. Estimated growth at steady state:

| Assumption | Value |
|------------|-------|
| Cities monitored | 12 |
| Polls per city per day | 288 (every 5 min) |
| New listings per poll | ~3–5 average |
| Rows added per day | ~10k–17k |
| Rows after 30 days | ~300k–500k |

At higher polling frequency or more cities the numbers scale proportionally.

**Recommended cleanup (NOT implemented — document only):**

Use `pg_cron` (available on Supabase Pro) to schedule a nightly DELETE:

```sql
-- pg_cron job (run after enabling pg_cron extension):
SELECT cron.schedule(
  'cleanup-seen-listings',
  '0 3 * * *',   -- 03:00 UTC daily
  $$
    DELETE FROM seen_listings
    WHERE first_seen_at < now() - (current_setting('app.seen_listings_ttl_days', true)::int || ' days')::interval;
  $$
);
```

Set `app.seen_listings_ttl_days` via `ALTER DATABASE postgres SET app.seen_listings_ttl_days = '30';` or pass as an env-driven migration. Default: 30 days.

Alternatively, implement in Python poller (batch DELETE at end of each poll cycle for entries older than TTL).

---

### fsm_state

Persistent FSM storage for aiogram 3. Required because Railway containers are stateless — aiogram's default in-memory storage is wiped on every redeploy or restart.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `key` | `text` | NOT NULL | — | **Primary key.** Format: `fsm:{chat_id}:{user_id}`. Matches aiogram `StorageKey` serialization. |
| `state` | `text` | YES | NULL | aiogram state name (e.g. `'SubscriptionForm:city'`). NULL = no active FSM state (user is idle). |
| `data` | `jsonb` | NOT NULL | `'{}'` | Arbitrary FSM context data stored by aiogram handlers. |
| `updated_at` | `timestamptz` | NOT NULL | `now()` | Auto-updated by trigger. |

**Trigger:** `set_fsm_state_updated_at` — sets `updated_at = now()` on every UPDATE.

**Integration note:** Wire aiogram's storage to this table using a custom `BaseStorage` implementation that does `INSERT ... ON CONFLICT (key) DO UPDATE` for writes and `SELECT` for reads. The `updated_at` column lets you identify and clean up stale FSM states (e.g. users who abandoned mid-flow).

---

## Row Level Security

RLS is **enabled on all 4 tables**. All 16 policies (SELECT/INSERT/UPDATE/DELETE × 4 tables) are granted to the `service_role` only.

| Role | Access | Rationale |
|------|--------|-----------|
| `service_role` | Full (all ops, all rows) | Railway backend authenticates with service key server-to-server. RLS is bypassed by service_role by default in Supabase, but explicit policies are created for clarity and to support future dashboard tools. |
| `authenticated` | None (no policy) | No end-user facing Supabase client at this stage. Add policies here when an admin dashboard or miniapp is introduced. |
| `anon` | None (no policy) | Public access blocked. |

**Security note:** The `service_role` key must NEVER be exposed client-side. It belongs only in Railway environment variables (`SUPABASE_SERVICE_KEY`). The bot has no Telegram Web App / miniapp at this stage, so no anon-key client usage exists.

---

## City Map

`subscriptions.city_id` stores the Krisha.kz **city path slug** directly — the same string that appears in Krisha.kz URLs (e.g. `krisha.kz/almaty/`). The canonical map is defined in `bot/config.py` as `CITY_MAP` and is the single source of truth.

| city_id (slug) | City name (display) |
|----------------|---------------------|
| `almaty` | Алматы |
| `astana` | Астана |
| `shymkent` | Шымкент |
| `karaganda` | Қарағанды |
| `aktobe` | Актобе |
| `atyrau` | Атырау |
| `pavlodar` | Павлодар |
| `ust-kamenogorsk` | Усть-Каменогорск |
| `semej` | Семей |
| `taraz` | Тараз |
| `kostanay` | Қостанай |

Store as `text` in `subscriptions.city_id`. Slugs match Krisha.kz URL path segments exactly, so no mapping or conversion is needed between the bot and the scraper.

---

## Entity Relationship

```
users (bigint PK)
  |
  | 1:N
  v
subscriptions (uuid PK)
  city_id ─────────── [no FK — references the city map above, enforced in app layer]

seen_listings (uuid PK)
  listing_id + city_id ─── [no FK — independent of subscriptions; dedup store]

fsm_state (text PK)
  key = 'fsm:{chat_id}:{user_id}' ─── [no FK on users — FSM key may outlive user row]
```

`seen_listings` and `fsm_state` have no foreign keys by design:
- `seen_listings` is a pure dedup store — it does not need referential integrity with subscriptions or users.
- `fsm_state` keys are created by aiogram before a `users` row necessarily exists (first message handling); FK would create a race condition.

---

## Common Query Patterns

### Poller: which cities to fetch?
```sql
SELECT DISTINCT city_id
FROM subscriptions
WHERE is_active = true;
-- Uses: idx_subscriptions_active_city_id (index-only scan)
```

### Poller: check if listing already seen
```sql
SELECT 1 FROM seen_listings
WHERE listing_id = $1 AND city_id = $2
LIMIT 1;
-- Uses: unique constraint index on (listing_id, city_id)
```

### Poller: mark listing as seen (upsert)
```sql
INSERT INTO seen_listings (listing_id, city_id)
VALUES ($1, $2)
ON CONFLICT (listing_id, city_id) DO NOTHING;
```

### Poller: fetch active subscriptions for a city
```sql
SELECT user_id, price_min, price_max, rooms
FROM subscriptions
WHERE city_id = $1
  AND is_active = true;
-- Uses: idx_subscriptions_active_city_id
```

### Bot: fetch all subscriptions for a user
```sql
SELECT id, city_id, city_name, price_min, price_max, rooms, is_active
FROM subscriptions
WHERE user_id = $1;
-- Uses: idx_subscriptions_user_id
```

### Bot: upsert user on first contact
```sql
INSERT INTO users (id, username, first_name, language_code)
VALUES ($1, $2, $3, $4)
ON CONFLICT (id) DO UPDATE SET
  username      = EXCLUDED.username,
  first_name    = EXCLUDED.first_name,
  language_code = EXCLUDED.language_code,
  updated_at    = now();
```

### FSM: read state
```sql
SELECT state, data FROM fsm_state WHERE key = $1;
```

### FSM: write state (upsert)
```sql
INSERT INTO fsm_state (key, state, data)
VALUES ($1, $2, $3)
ON CONFLICT (key) DO UPDATE SET
  state      = EXCLUDED.state,
  data       = EXCLUDED.data,
  updated_at = now();
```
