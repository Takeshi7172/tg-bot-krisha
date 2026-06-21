# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Telegram bot (aiogram 3) that scrapes krisha.kz and auto-pushes FRESH apartment-for-SALE listings FROM PRIVATE OWNERS (userType == "owner", excludes developers/agencies) to subscribers by city.

## Stack

- **Bot framework:** aiogram 3 (async, Python 3.12)
- **HTTP client:** httpx (async) + selectolax (fast C-backed HTML parser)
- **Data validation:** pydantic v2, pydantic-settings v2
- **Database:** supabase-py v2 (Postgres via Supabase) — stores subscriptions, seen_listings, fsm_state
- **FSM storage:** SupabaseFSMStorage (default) or RedisStorage (if REDIS_URL set)
- **Deploy:** Railway — polling mode for dev, webhook mode (aiohttp on $PORT) for prod

## Architecture

```
bot/
  config.py          — Settings (pydantic-settings) + CITY_MAP (single source of truth for city slugs)
  main.py            — Entry point: FSM storage selection, Bot+Dispatcher, KrishaPoller task, webhook/polling mode
  scraper/
    models.py        — Listing (pydantic v2, frozen) — DTO between scraper and push layer
    krisha_scraper.py — Two-phase scraper: Phase 1 fetch_listing_ids (search page, div[data-id])
                        Phase 2 fetch_listing_detail (detail page, script#jsdata JSON)
    anti_bot.py      — UA rotation, retry logic, challenge detection, random delays
  poller/
    poller.py        — KrishaPoller: loops every POLL_INTERVAL_SECONDS;
                        dedup via seen_listings BEFORE detail fetch; mark_seen always;
                        push only if is_owner; injected push_callback (no telegram imports here)
  db/
    supabase_client.py — Async Supabase client singleton
    repositories.py    — DB queries: get_active_city_slugs, get_subscribers_for_city, is_seen, mark_seen
  fsm/
    states.py        — FSMStates (subscribe flow)
    storage.py       — SupabaseFSMStorage (persists FSM in fsm_state table)
  handlers/
    start.py         — /start handler
    subscribe.py     — subscription creation flow (FSM)
    subscriptions.py — /subscriptions list + cancel
  keyboards/
    cities.py        — City selection inline keyboard (from CITY_MAP)
    subscriptions.py — Subscription management keyboard
  push/
    formatter.py     — Format Listing → Telegram HTML message
    sender.py        — send_listing_to_user with Telegram rate limiting (_last_sent at module level)
```

### Data flow

```
KrishaScraper.fetch_listing_ids(city_slug)
  → list[(listing_id, url)]           # Phase 1: search page, div[data-id]

KrishaPoller: for each id NOT in seen_listings:
  KrishaScraper.fetch_listing_detail(listing_id, city_slug)
    → Listing                         # Phase 2: detail page, script#jsdata JSON

  mark_seen(listing_id, city_slug)   # always, before push decision

  if listing.is_owner:
    for sub in get_subscribers_for_city(city_slug):
      if matches_filters(listing, sub):
        push_callback(sub.user_id, listing)   # injected from bot layer
```

**Decoupling rule:** `bot/scraper/` and `bot/poller/` have zero aiogram/telegram imports. The push_callback is injected at startup as `functools.partial(send_listing_to_user, bot)`.

## Commands

Use `py` on Windows, `python` on Linux/Docker.

```bash
# Run locally (polling mode) — requires .env with BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY
py -m bot.main

# Run all tests
py -m pytest -q

# Run a single test
py -m pytest tests/poller/test_poller.py::test_name

# Lint
ruff check bot/
```

## Database

Apply the initial schema before first run:

```bash
# Via Supabase CLI
supabase db push

# Or paste supabase/migrations/20260621_001_initial_schema.sql into the Supabase dashboard SQL editor
```

Tables:
- `subscriptions` — user subscriptions (user_id, city_id, price_min, price_max, rooms[])
- `seen_listings` — dedup log (listing_id, city_id, seen_at). Rows older than SEEN_LISTINGS_TTL_DAYS are eligible for cleanup via pg_cron (see docs/schema.md).
- `fsm_state` — persisted FSM state for SupabaseFSMStorage

`city_id` in all tables stores the krisha SLUG (e.g. `almaty`, `astana`) — not a numeric id.

## Environment Variables

See `.env.example` for the full annotated list. Summary:

| Variable | Required | Default | Notes |
|---|---|---|---|
| `BOT_TOKEN` | yes | — | From @BotFather |
| `SUPABASE_URL` | yes | — | `https://<ref>.supabase.co` |
| `SUPABASE_SERVICE_KEY` | yes | — | service_role key, server-side only |
| `POLL_INTERVAL_SECONDS` | no | 300 | Seconds between scrape cycles |
| `REQUEST_TIMEOUT` | no | 20 | httpx timeout in seconds |
| `KRISHA_BASE_URL` | no | https://krisha.kz | Override for testing |
| `SEEN_LISTINGS_TTL_DAYS` | no | 30 | TTL for seen_listings cleanup |
| `REDIS_URL` | no | None | If set, uses RedisStorage for FSM |
| `WEBHOOK_URL` | no | None | If set, runs in webhook mode (prod) |
| `PORT` | no | 8080 | aiohttp webhook server port |

**Railway note:** `SUPABASE_SERVICE_KEY` value must be the Supabase **transaction-mode pooler** connection string, not the direct connection. Pooler port is `:6543`. The direct database endpoint is IPv6-only and unreachable from Railway's IPv4 network.

## krisha.kz Specifics

- Owner-only search URL: `https://krisha.kz/prodazha/kvartiry/{city_slug}/?das[who]=1`
- `das[who]=1` includes developers (`userType == "complex"`), so the poller post-filters on `userType == "owner"` from the detail page JSON. This is the single reliable owner indicator.
- Default sort is newest-first (no sort param needed).
- **CITY_MAP in `bot/config.py`** is the single source of truth for valid city slugs and display names. Do not add cities anywhere else.
- Detail page data lives in `<script id="jsdata">` as a JSON blob. Key paths verified 2026-06-21:
  - `data["advert"]["userType"]` — `"owner"` | `"complex"` | `"agent"` / `"agency"`
  - `data["advert"]["price"]` — price in KZT
  - `data["advert"]["rooms"]`, `data["advert"]["square"]`, `data["advert"]["title"]`
  - `data["adverts"][0]["fullAddress"]` — human-readable address
  - `data["adverts"][0]["createdAt"]` — original publish date `"YYYY-MM-DD"` (use this, NOT `addedAt`)
  - `data["advert"]["address"]` — structured dict, do NOT coerce to str
- The scraper logs jsdata top-level keys at DEBUG on every parse — useful for detecting layout changes.

## Known Limitations / First-Run Checks

- jsdata key paths verified 2026-06-21 but krisha HTML can change without notice. If listings stop arriving, enable DEBUG logging and check the logged jsdata keys.
- No Cloudflare WAF observed on listing pages as of 2026-06-21, but reCAPTCHA is present on forms (not scraped).
- `seen_listings` grows approximately unbounded in steady state. The `SEEN_LISTINGS_TTL_DAYS` setting marks the cutoff for cleanup; the actual pg_cron cleanup job is documented in `docs/schema.md`.
- In webhook mode, the health probe endpoint is `GET /healthz` (returns `200 ok`). Polling mode has no HTTP server — Railway health checks require webhook mode or must be disabled.
