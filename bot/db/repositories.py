"""
bot/db/repositories.py — All DB access functions for tg-bot-krisha.

Each function accepts an AsyncClient (from supabase_client.get_supabase_client())
rather than creating its own connection — callers control the client lifecycle.

Table / column names must stay in sync with supabase/migrations/20260621_001_initial_schema.sql.

city_id NOTE: city_id is TEXT and stores the krisha CITY SLUG (e.g. 'almaty'),
NOT a numeric id. This overrides the legacy numeric map in docs/schema.md
(Wave 1 assumption, corrected by Wave 2 live recon confirming path-slug URLs).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from supabase._async.client import AsyncClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data transfer types returned by repository functions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubscriptionRow:
    """
    Minimal subscription data needed by the poller to match listings.

    user_id    : Telegram user id (bigint)
    price_min  : Optional lower price bound in KZT
    price_max  : Optional upper price bound in KZT
    rooms      : Optional list of accepted room counts, e.g. [1, 2, 3]
    """

    user_id: int
    price_min: int | None
    price_max: int | None
    rooms: list[int] | None


@dataclass(frozen=True)
class FullSubscriptionRow:
    """
    Full subscription row for display in the bot UI.

    id        : Subscription UUID (string)
    user_id   : Telegram user id
    city_id   : Krisha city slug
    city_name : Human-readable city name
    price_min : Optional lower price bound
    price_max : Optional upper price bound
    rooms     : Optional room filter list
    is_active : Whether the subscription is active
    """

    id: str
    user_id: int
    city_id: str
    city_name: str
    price_min: int | None
    price_max: int | None
    rooms: list[int] | None
    is_active: bool


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def upsert_user(
    client: AsyncClient,
    *,
    user_id: int,
    username: str | None,
    first_name: str | None,
    language_code: str | None,
) -> None:
    """
    Insert a user row or update username/first_name/language_code on conflict.

    Equivalent SQL (from schema.md):
      INSERT INTO users (id, username, first_name, language_code)
      VALUES ($1, $2, $3, $4)
      ON CONFLICT (id) DO UPDATE SET
        username = EXCLUDED.username,
        first_name = EXCLUDED.first_name,
        language_code = EXCLUDED.language_code,
        updated_at = now();

    supabase-py upsert with on_conflict='id' achieves the same effect.
    """
    data = {
        "id": user_id,
        "username": username,
        "first_name": first_name,
        "language_code": language_code,
        "is_active": True,
    }
    try:
        await client.table("users").upsert(data, on_conflict="id").execute()
        logger.debug("repositories: upsert_user user_id=%d", user_id)
    except Exception as exc:
        logger.error("repositories: upsert_user failed user_id=%d: %s", user_id, exc)
        raise


async def set_user_inactive(client: AsyncClient, user_id: int) -> None:
    """
    Mark a user as inactive (bot was blocked by user — Telegram 403).
    Inactive users are excluded from broadcast queries.
    """
    try:
        await (
            client.table("users")
            .update({"is_active": False})
            .eq("id", user_id)
            .execute()
        )
        logger.info("repositories: set_user_inactive user_id=%d", user_id)
    except Exception as exc:
        logger.error(
            "repositories: set_user_inactive failed user_id=%d: %s", user_id, exc
        )
        raise


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


async def get_active_city_slugs(client: AsyncClient) -> list[str]:
    """
    Return the list of distinct city slugs that have at least one active subscription.

    Equivalent SQL:
      SELECT DISTINCT city_id FROM subscriptions WHERE is_active = true;

    The poller calls this at the start of each poll cycle to determine which
    cities to scrape.
    """
    try:
        response = await (
            client.table("subscriptions")
            .select("city_id")
            .eq("is_active", True)
            .execute()
        )
        rows: list[dict] = response.data or []
        # Deduplicate in Python (PostgREST .select doesn't support DISTINCT directly)
        seen: set[str] = set()
        result: list[str] = []
        for row in rows:
            slug = row.get("city_id", "")
            if slug and slug not in seen:
                seen.add(slug)
                result.append(slug)
        logger.debug(
            "repositories: get_active_city_slugs found %d cities: %s",
            len(result),
            result,
        )
        return result
    except Exception as exc:
        logger.error("repositories: get_active_city_slugs failed: %s", exc)
        raise


async def get_subscribers_for_city(
    client: AsyncClient,
    city_slug: str,
) -> list[SubscriptionRow]:
    """
    Return all active subscribers for a given city slug, with their filters.

    Equivalent SQL:
      SELECT user_id, price_min, price_max, rooms
      FROM subscriptions
      WHERE city_id = $1 AND is_active = true;

    The poller uses this to determine who receives a notification for a given
    listing, then applies the price/rooms filters before calling push_callback.
    """
    try:
        response = await (
            client.table("subscriptions")
            .select("user_id, price_min, price_max, rooms")
            .eq("city_id", city_slug)
            .eq("is_active", True)
            .execute()
        )
        rows: list[dict] = response.data or []
        result: list[SubscriptionRow] = []
        for row in rows:
            rooms_raw = row.get("rooms")
            rooms_list: list[int] | None = None
            if isinstance(rooms_raw, list):
                rooms_list = [int(r) for r in rooms_raw if r is not None]

            result.append(
                SubscriptionRow(
                    user_id=int(row["user_id"]),
                    price_min=row.get("price_min"),
                    price_max=row.get("price_max"),
                    rooms=rooms_list if rooms_list else None,
                )
            )
        logger.debug(
            "repositories: get_subscribers_for_city city=%s found %d subscribers",
            city_slug,
            len(result),
        )
        return result
    except Exception as exc:
        logger.error(
            "repositories: get_subscribers_for_city failed city=%s: %s",
            city_slug,
            exc,
        )
        raise


async def get_user_subscriptions(
    client: AsyncClient,
    user_id: int,
) -> list[FullSubscriptionRow]:
    """
    Return all subscriptions (active and inactive) for a given user.

    Equivalent SQL:
      SELECT id, city_id, city_name, price_min, price_max, rooms, is_active
      FROM subscriptions
      WHERE user_id = $1;
    """
    try:
        response = await (
            client.table("subscriptions")
            .select("id, city_id, city_name, price_min, price_max, rooms, is_active")
            .eq("user_id", user_id)
            .execute()
        )
        rows: list[dict] = response.data or []
        result: list[FullSubscriptionRow] = []
        for row in rows:
            rooms_raw = row.get("rooms")
            rooms_list: list[int] | None = None
            if isinstance(rooms_raw, list):
                rooms_list = [int(r) for r in rooms_raw if r is not None]

            result.append(
                FullSubscriptionRow(
                    id=str(row["id"]),
                    user_id=user_id,
                    city_id=row["city_id"],
                    city_name=row["city_name"],
                    price_min=row.get("price_min"),
                    price_max=row.get("price_max"),
                    rooms=rooms_list if rooms_list else None,
                    is_active=bool(row.get("is_active", True)),
                )
            )
        return result
    except Exception as exc:
        logger.error(
            "repositories: get_user_subscriptions failed user_id=%d: %s",
            user_id,
            exc,
        )
        raise


async def add_subscription(
    client: AsyncClient,
    *,
    user_id: int,
    city_id: str,
    city_name: str,
    price_min: int | None = None,
    price_max: int | None = None,
    rooms: list[int] | None = None,
) -> FullSubscriptionRow:
    """
    Insert a new subscription or update filters on an existing (user_id, city_id) pair.

    Uses INSERT ... ON CONFLICT (user_id, city_id) DO UPDATE to implement upsert.
    The UNIQUE(user_id, city_id) constraint is defined in the migration.

    Returns the resulting FullSubscriptionRow.
    """
    data: dict = {
        "user_id": user_id,
        "city_id": city_id,
        "city_name": city_name,
        "is_active": True,
        "price_min": price_min,
        "price_max": price_max,
        "rooms": rooms,
    }
    try:
        response = await (
            client.table("subscriptions")
            .upsert(data, on_conflict="user_id,city_id")
            .execute()
        )
        row: dict = (response.data or [{}])[0]
        rooms_raw = row.get("rooms")
        rooms_list: list[int] | None = None
        if isinstance(rooms_raw, list):
            rooms_list = [int(r) for r in rooms_raw if r is not None]

        logger.info(
            "repositories: add_subscription user_id=%d city=%s", user_id, city_id
        )
        return FullSubscriptionRow(
            id=str(row["id"]),
            user_id=user_id,
            city_id=city_id,
            city_name=city_name,
            price_min=price_min,
            price_max=price_max,
            rooms=rooms_list if rooms_list else None,
            is_active=True,
        )
    except Exception as exc:
        logger.error(
            "repositories: add_subscription failed user_id=%d city=%s: %s",
            user_id,
            city_id,
            exc,
        )
        raise


async def remove_subscription(
    client: AsyncClient,
    *,
    user_id: int,
    city_id: str,
) -> None:
    """
    Deactivate a subscription (set is_active = false).

    We soft-delete rather than hard-delete so that seen_listings dedup
    remains effective if the user resubscribes later — they won't be
    re-notified about already-seen listings.
    """
    try:
        await (
            client.table("subscriptions")
            .update({"is_active": False})
            .eq("user_id", user_id)
            .eq("city_id", city_id)
            .execute()
        )
        logger.info(
            "repositories: remove_subscription user_id=%d city=%s", user_id, city_id
        )
    except Exception as exc:
        logger.error(
            "repositories: remove_subscription failed user_id=%d city=%s: %s",
            user_id,
            city_id,
            exc,
        )
        raise


# ---------------------------------------------------------------------------
# Seen listings (dedup)
# ---------------------------------------------------------------------------


async def is_seen(
    client: AsyncClient,
    listing_id: str,
    city_id: str,
) -> bool:
    """
    Return True if the listing has already been seen (and notified) for this city.

    Equivalent SQL:
      SELECT 1 FROM seen_listings WHERE listing_id = $1 AND city_id = $2 LIMIT 1;

    This is the fast-path pre-check. Dedup is double-locked:
      1. This function (Python-layer check)
      2. UNIQUE(listing_id, city_id) constraint in DB (prevents duplicate rows)
    """
    try:
        response = await (
            client.table("seen_listings")
            .select("listing_id")
            .eq("listing_id", listing_id)
            .eq("city_id", city_id)
            .limit(1)
            .execute()
        )
        found = bool(response.data)
        logger.debug(
            "repositories: is_seen listing_id=%s city=%s → %s",
            listing_id,
            city_id,
            found,
        )
        return found
    except Exception as exc:
        logger.error(
            "repositories: is_seen failed listing_id=%s city=%s: %s",
            listing_id,
            city_id,
            exc,
        )
        raise


async def mark_seen(
    client: AsyncClient,
    listing_id: str,
    city_id: str,
) -> None:
    """
    Record that a listing has been seen for this city.

    Uses INSERT ... ON CONFLICT DO NOTHING to handle the race condition where
    two concurrent poller instances (unlikely but possible) both try to insert
    the same row.

    Equivalent SQL:
      INSERT INTO seen_listings (listing_id, city_id)
      VALUES ($1, $2)
      ON CONFLICT (listing_id, city_id) DO NOTHING;
    """
    try:
        await (
            client.table("seen_listings")
            .upsert(
                {"listing_id": listing_id, "city_id": city_id},
                on_conflict="listing_id,city_id",
                ignore_duplicates=True,
            )
            .execute()
        )
        logger.debug(
            "repositories: mark_seen listing_id=%s city=%s", listing_id, city_id
        )
    except Exception as exc:
        logger.error(
            "repositories: mark_seen failed listing_id=%s city=%s: %s",
            listing_id,
            city_id,
            exc,
        )
        raise
