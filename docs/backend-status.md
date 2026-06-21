# Wave 2 Backend Status

STATUS: IN_PROGRESS

## Decisions (recorded before implementation)

### jsdata key mapping (live-confirmed 2026-06-21 — GROUND TRUTH)
- `advert["id"]` → Listing.id (str cast)
- `advert["title"]` → Listing.title (str, always present — no fallback chain)
- `advert["price"]` → Listing.price (int KZT)
- `advert["rooms"]` → Listing.rooms (int)
- `advert["square"]` → Listing.area (float)
- `advert["photos"][0]["src"]` → Listing.image_url (guard empty list → None)
- `adverts[0]["fullAddress"]` → Listing.address (human-readable city+district+street)
- `adverts[0]["createdAt"]` → Listing.published_at (original publish date "YYYY-MM-DD")
- Listing.url constructed as `"https://krisha.kz/a/show/" + id`
- `advert["userType"]` → Listing.is_owner (`== "owner"` → True; "complex"/"agent" → False)

**Keys NOT used (wrong / misleading):**
- `advert["address"]` — structured dict `{microdistrict, city, district, street, house_num}` — do NOT coerce to str
- `advert["addressTitle"]` — shorter alt, superseded by `adverts[0]["fullAddress"]`
- `adverts[0]["url"]` — protocol-relative `//krisha.kz/…` — construct url from id instead
- `adverts[0]["addedAt"]` — resurfaced/refresh date — do NOT use for published_at
- `adverts[0]["isOwner"]` / `adverts[0]["isAgent"]` — viewer-relative flags, BOTH false on genuine owner listing — do NOT use
- `advert["who"]`, `advert["sellerType"]`, `advert["isAgency"]` — non-existent keys (old guesses)

**Poller owner gate (product fix 2026-06-21):**
`das[who]=1` INCLUDES `userType="complex"` (developers/ЖК). The poller now gates on `listing.is_owner is True` (whitelist "owner" only). Non-owner listings are always `mark_seen` but never pushed to subscribers.

### city_id handling
Schema's Wave 1 docs use numeric strings ('1', '4', etc.) but the brief's verified live recon shows PATH slugs ('almaty', 'astana'). The brief states city_id IS TEXT and stores the krisha CITY SLUG. The CITY_MAP in config.py uses slugs as keys. All DB calls use slug strings matching config.CITY_MAP keys. This is the authoritative decision — the schema.md city map table is stale (Wave 1 assumption, corrected by Wave 2 live recon).

### supabase-py async usage
supabase-py v2 async client: `from supabase._async.client import AsyncClient, create_client`. All repository methods are async. The sync `create_client` from `supabase` top-level is NOT used.

### Poller push_callback signature
`push_callback(user_id: int, listing: Listing) -> Awaitable[None]` — Wave 3 (bot handlers) will inject a concrete implementation.

### Filter matching
A subscriber receives a listing if ALL of:
- `price_min` is None OR listing.price is None OR listing.price >= price_min
- `price_max` is None OR listing.price is None OR listing.price <= price_max
- `rooms` is None OR listing.rooms is None OR listing.rooms in rooms[]

### requirements.txt
Uses `aiogram>=3.13,<4` — imported in requirements but NOT imported in any Wave 2 Python file (constraint satisfied). Wave 3 bot layer will import aiogram.

---

## Files to create
1. `bot/__init__.py`
2. `bot/scraper/__init__.py`
3. `bot/scraper/models.py`
4. `bot/scraper/anti_bot.py`
5. `bot/scraper/krisha_scraper.py`
6. `bot/poller/__init__.py`
7. `bot/poller/poller.py`
8. `bot/db/__init__.py`
9. `bot/db/supabase_client.py`
10. `bot/db/repositories.py`
11. `bot/config.py`
12. `requirements.txt`

---

STATUS: DONE (updated after all files written)

## Files Changed
- `bot/__init__.py` — package marker
- `bot/scraper/__init__.py` — package marker
- `bot/scraper/models.py` — Listing Pydantic v2 model
- `bot/scraper/anti_bot.py` — UA pool, headers, delay, retry, challenge detection
- `bot/scraper/krisha_scraper.py` — KrishaScraper: fetch_listing_ids + fetch_listing_detail
- `bot/poller/__init__.py` — package marker
- `bot/poller/poller.py` — KrishaPoller with push_callback injection
- `bot/db/__init__.py` — package marker
- `bot/db/supabase_client.py` — async supabase-py v2 client factory
- `bot/db/repositories.py` — all repository functions
- `bot/config.py` — pydantic-settings Settings + CITY_MAP
- `requirements.txt` — pinned dependencies
