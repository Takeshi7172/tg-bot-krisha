"""
bot/scraper/krisha_scraper.py — krisha.kz scraper (two-phase, owner-only).

Phase 1 — fetch_listing_ids(city_slug):
  GET /prodazha/kvartiry/{city_slug}/?das[who]=1
  Parse div[data-id] from section.a-search-list.
  Returns list of (listing_id, url) tuples.

Phase 2 — fetch_listing_detail(listing_id, city_slug):
  GET /a/show/{listing_id}
  Parse <script id="jsdata"> JSON blob.
  Returns a fully populated Listing.

The caller (KrishaPoller) performs dedup between phases 1 and 2 — detail
requests are only issued for listing ids NOT already in seen_listings. This
keeps detail fetches rare in steady state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx
from selectolax.parser import HTMLParser

from bot.config import get_settings
from bot.scraper.anti_bot import (
    build_headers,
    is_challenge_response,
    with_retry,
)
from bot.scraper.models import Listing

logger = logging.getLogger(__name__)


class KrishaScraper:
    """
    Async HTTP scraper for krisha.kz apartment-for-sale listings.

    Usage
    -----
    async with KrishaScraper() as scraper:
        ids = await scraper.fetch_listing_ids("almaty")
        listing = await scraper.fetch_listing_detail("1013256936", "almaty")

    The scraper owns a single httpx.AsyncClient for connection reuse. The
    client is created in __aenter__ and closed in __aexit__.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "KrishaScraper":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.REQUEST_TIMEOUT),
            follow_redirects=True,
            # Do NOT pass headers here — we build them per-request so the UA
            # rotates and the Referer is request-specific.
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Phase 1: search page → list of (id, url) pairs
    # ------------------------------------------------------------------

    async def fetch_listing_ids(
        self,
        city_slug: str,
        page: int = 1,
    ) -> list[tuple[str, str]]:
        """
        Fetch one search-results page and return all listing ids + URLs found.

        Parameters
        ----------
        city_slug : str
            Krisha city slug, e.g. 'almaty'. Used as the URL path segment.
        page : int
            Page number (1-based). Default 1.

        Returns
        -------
        list[tuple[str, str]]
            List of (listing_id, full_url) tuples parsed from div[data-id].
            Returns an empty list if:
            - div.a-search-empty is present (no results)
            - The response is a challenge page
            - A network error occurs after all retries
        """
        assert self._client is not None, "Use KrishaScraper as an async context manager"

        base_url = self._settings.KRISHA_BASE_URL
        url = f"{base_url}/prodazha/kvartiry/{city_slug}/"
        params: dict[str, str | int] = {"das[who]": 1}
        if page > 1:
            params["page"] = page

        async def _do_request() -> httpx.Response:
            return await self._client.get(  # type: ignore[union-attr]
                url,
                params=params,
                headers=build_headers(referer=f"{base_url}/"),
            )

        try:
            response: httpx.Response = await with_retry(
                _do_request,
                context=f"fetch_listing_ids city={city_slug} page={page}",
            )
        except Exception as exc:
            logger.error(
                "krisha_scraper: fetch_listing_ids failed after retries "
                "city=%s page=%d: %s",
                city_slug,
                page,
                exc,
            )
            return []

        body = response.text

        # Block / challenge detection
        if is_challenge_response(response.status_code, body):
            logger.warning(
                "krisha_scraper: challenge/block detected for city=%s page=%d "
                "status=%d — skipping city this cycle",
                city_slug,
                page,
                response.status_code,
            )
            return []

        if response.status_code != 200:
            logger.warning(
                "krisha_scraper: unexpected status %d for city=%s page=%d",
                response.status_code,
                city_slug,
                page,
            )
            return []

        return self._parse_listing_ids(body, city_slug, base_url)

    def _parse_listing_ids(
        self,
        html: str,
        city_slug: str,
        base_url: str,
    ) -> list[tuple[str, str]]:
        """
        Parse the search-results HTML and return (listing_id, url) pairs.

        Selectors (andprov spider.py, May 2025 — verified):
          - Empty results guard:  div.a-search-empty
          - Listings container:   section.a-search-list
          - Each card:            div[data-id]  (data-id attribute = listing id)
          - Card link:            a.a-card__title  (href is relative, e.g. /a/show/12345)
        """
        tree = HTMLParser(html)

        # Empty results page
        if tree.css_first("div.a-search-empty") is not None:
            logger.debug("krisha_scraper: empty results page for city=%s", city_slug)
            return []

        container = tree.css_first("section.a-search-list")
        if container is None:
            logger.debug(
                "krisha_scraper: section.a-search-list not found for city=%s "
                "(page may be empty or layout changed)",
                city_slug,
            )
            return []

        results: list[tuple[str, str]] = []

        for card in container.css("div[data-id]"):
            listing_id = card.attributes.get("data-id", "").strip()
            if not listing_id:
                continue

            # Prefer the canonical URL from the title link
            link_node = card.css_first("a.a-card__title")
            if link_node is not None:
                href = link_node.attributes.get("href", "").strip()
                full_url = f"{base_url}{href}" if href.startswith("/") else href
            else:
                # Fallback: construct from listing id
                full_url = f"{base_url}/a/show/{listing_id}"

            results.append((listing_id, full_url))

        logger.debug(
            "krisha_scraper: parsed %d listing ids for city=%s",
            len(results),
            city_slug,
        )
        return results

    # ------------------------------------------------------------------
    # Phase 2: detail page → full Listing object
    # ------------------------------------------------------------------

    async def fetch_listing_detail(
        self,
        listing_id: str,
        city_slug: str,
    ) -> Listing:
        """
        Fetch the detail page for one listing and return a populated Listing.

        Parses the <script id="jsdata"> JSON blob embedded in the page. Falls
        back to a minimal Listing (id + url + city_id) if the JSON cannot be
        extracted or parsed.

        Parameters
        ----------
        listing_id : str
            Krisha listing id (numeric string).
        city_slug : str
            City slug, e.g. 'almaty' — stored in Listing.city_id.

        Returns
        -------
        Listing
            Populated Listing. May have None fields if the detail page is
            unavailable or the JSON structure is unexpected. Never raises.
        """
        assert self._client is not None, "Use KrishaScraper as an async context manager"

        base_url = self._settings.KRISHA_BASE_URL
        detail_url = f"{base_url}/a/show/{listing_id}"
        search_referer = f"{base_url}/prodazha/kvartiry/{city_slug}/?das[who]=1"

        async def _do_request() -> httpx.Response:
            return await self._client.get(  # type: ignore[union-attr]
                detail_url,
                headers=build_headers(referer=search_referer),
            )

        try:
            response: httpx.Response = await with_retry(
                _do_request,
                context=f"fetch_listing_detail id={listing_id} city={city_slug}",
            )
        except Exception as exc:
            logger.error(
                "krisha_scraper: fetch_listing_detail failed after retries "
                "id=%s city=%s: %s — returning partial listing",
                listing_id,
                city_slug,
                exc,
            )
            return Listing(id=listing_id, url=detail_url, city_id=city_slug)

        body = response.text

        if is_challenge_response(response.status_code, body):
            logger.warning(
                "krisha_scraper: challenge/block on detail id=%s city=%s "
                "status=%d — returning partial listing",
                listing_id,
                city_slug,
                response.status_code,
            )
            return Listing(id=listing_id, url=detail_url, city_id=city_slug)

        if response.status_code != 200:
            logger.warning(
                "krisha_scraper: detail page status %d for id=%s city=%s "
                "— returning partial listing",
                response.status_code,
                listing_id,
                city_slug,
            )
            return Listing(id=listing_id, url=detail_url, city_id=city_slug)

        return self._parse_detail(body, listing_id, city_slug, detail_url)

    def _parse_detail(
        self,
        html: str,
        listing_id: str,
        city_slug: str,
        detail_url: str,
    ) -> Listing:
        """
        Parse detail page HTML. Extract the jsdata JSON blob and build a Listing.

        jsdata structure (live-confirmed 2026-06-21):
          data["advert"]          — dict with core fields (singular)
          data["adverts"][0]      — dict with fullAddress, createdAt, addedAt, url
          data["advert"]["id"]    — listing id (int)
          data["advert"]["title"] — listing title string (always present)
          data["advert"]["price"] — price in KZT (int)
          data["advert"]["rooms"] — room count (int)
          data["advert"]["square"]     — area m² (float/int)
          data["advert"]["photos"]     — list of {src, w, h, title, alt}
          data["advert"]["userType"]   — "owner" | "complex" | "agent" / "agency"
          data["adverts"][0]["fullAddress"] — full human-readable address string
          data["adverts"][0]["createdAt"]   — original publish date "YYYY-MM-DD"
          data["adverts"][0]["addedAt"]     — resurfaced/refresh date (do NOT use)
          data["adverts"][0]["url"]         — protocol-relative "//krisha.kz/..."
                                             (do NOT use — construct url from id)
          data["advert"]["address"]         — STRUCTURED dict {microdistrict, city, …}
                                             (do NOT coerce to str)
        """
        tree = HTMLParser(html)
        fallback = Listing(id=listing_id, url=detail_url, city_id=city_slug)

        script_node = tree.css_first("script#jsdata")
        if script_node is None:
            logger.warning(
                "krisha_scraper: script#jsdata not found for id=%s", listing_id
            )
            return fallback

        raw = script_node.text()
        if not raw:
            logger.warning(
                "krisha_scraper: script#jsdata is empty for id=%s", listing_id
            )
            return fallback

        # Extract the outermost JSON object from the script text
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            logger.warning(
                "krisha_scraper: could not locate JSON bounds in jsdata for id=%s",
                listing_id,
            )
            return fallback

        try:
            data: dict = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            logger.warning(
                "krisha_scraper: JSON parse error in jsdata for id=%s: %s",
                listing_id,
                exc,
            )
            return fallback

        # Log top-level keys once at DEBUG so the mapping can be verified in dev
        logger.debug(
            "krisha_scraper: jsdata top-level keys for id=%s: %s",
            listing_id,
            list(data.keys()),
        )

        advert: dict = data.get("advert") or {}
        adverts_list: list = data.get("adverts") or []
        advert0: dict = adverts_list[0] if adverts_list else {}

        # Log advert top-level keys once at DEBUG
        if advert:
            logger.debug(
                "krisha_scraper: advert keys for id=%s: %s",
                listing_id,
                list(advert.keys()),
            )

        # --- Confirmed fields (live-verified 2026-06-21) ---
        price: int | None = self._safe_int(advert.get("price"))
        rooms: int | None = self._safe_int(advert.get("rooms"))
        area: float | None = self._safe_float(advert.get("square"))

        # title lives directly in advert["title"] — no fallback chain needed
        title: str | None = advert.get("title") or None
        if isinstance(title, str):
            title = title.strip() or None

        # address: adverts[0]["fullAddress"] — best human-readable form.
        # advert["address"] is a STRUCTURED dict — do NOT coerce to str.
        address: str | None = advert0.get("fullAddress") or None

        # image: guard empty photos list
        image_url: str | None = None
        photos = advert.get("photos")
        if isinstance(photos, list) and photos:
            first_photo = photos[0]
            if isinstance(first_photo, dict):
                image_url = first_photo.get("src") or None

        # url: construct from id — adverts[0]["url"] is protocol-relative "//krisha.kz/…"
        base_url = self._settings.KRISHA_BASE_URL
        listing_id_confirmed = str(advert.get("id") or listing_id)
        confirmed_url = f"{base_url}/a/show/{listing_id_confirmed}"

        # published_at: adverts[0]["createdAt"] is original publish date "YYYY-MM-DD".
        # adverts[0]["addedAt"] is the resurfaced/refresh date — do NOT use.
        published_at: datetime | None = self._parse_date(advert0.get("createdAt"))

        # is_owner: advert["userType"] is the authoritative owner indicator.
        # Whitelist ONLY "owner" (private person). "complex" = developer/ЖК,
        # which appears in das[who]=1 results but is NOT a private owner.
        # adverts[0]["isOwner"] / adverts[0]["isAgent"] are viewer-relative — ignore.
        user_type: str | None = advert.get("userType")
        is_owner: bool = user_type == "owner"
        logger.debug(
            "krisha_scraper: id=%s userType=%r is_owner=%s",
            listing_id_confirmed,
            user_type,
            is_owner,
        )

        return Listing(
            id=listing_id_confirmed,
            title=title,
            price=price,
            rooms=rooms,
            area=area,
            address=address,
            url=confirmed_url,
            image_url=image_url,
            published_at=published_at,
            city_id=city_slug,
            is_owner=is_owner,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_int(value: object) -> int | None:
        """Convert a value to int, returning None on failure."""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(value: object) -> float | None:
        """Convert a value to float, returning None on failure."""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_date(value: object) -> datetime | None:
        """
        Parse a date value from adverts[0]["createdAt"].

        Handles:
          - "YYYY-MM-DD" plain date string → datetime at midnight UTC
          - Full ISO 8601 string (with time/tz) → datetime
          - Unix timestamp int/float → datetime UTC
          - None / unparseable → None
        """
        if value is None:
            return None

        # Unix timestamp
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                return None

        if isinstance(value, str) and value.strip():
            raw = value.strip()
            # Plain date "YYYY-MM-DD" — fromisoformat handles this in 3.7+
            # Full ISO with Z suffix — replace for 3.9/3.10 compat
            raw = raw.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                logger.debug(
                    "krisha_scraper: could not parse createdAt value=%r", value
                )

        return None
