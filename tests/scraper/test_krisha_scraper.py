"""
tests/scraper/test_krisha_scraper.py — Hermetic tests for KrishaScraper.

All HTTP calls are intercepted by respx — no real network traffic.
Tests cover:
  - Phase 1: parse listing IDs from search HTML fixtures
  - Phase 2: parse detail page jsdata → Listing (owner vs complex)
  - Empty results guard (div.a-search-empty)
  - Challenge / block response handling
  - Fallback Listing on network failure / bad JSON
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import respx
import httpx

from bot.scraper.krisha_scraper import KrishaScraper
from bot.scraper.models import Listing

FIXTURES = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _make_scraper() -> KrishaScraper:
    """Return a KrishaScraper with a real (but intercepted) httpx client."""
    scraper = KrishaScraper()
    scraper._client = httpx.AsyncClient(
        timeout=httpx.Timeout(5),
        follow_redirects=True,
    )
    return scraper


# ---------------------------------------------------------------------------
# Phase 1: fetch_listing_ids / _parse_listing_ids
# ---------------------------------------------------------------------------


class TestParseListingIds:
    """Unit tests for _parse_listing_ids (no HTTP, call directly)."""

    def setup_method(self):
        self.scraper = KrishaScraper()
        # _parse_listing_ids doesn't need _client
        self.base_url = "https://krisha.kz"

    def test_parses_three_ids(self):
        html = _load_fixture("search_results.html")
        result = self.scraper._parse_listing_ids(html, "almaty", self.base_url)
        ids = [r[0] for r in result]
        assert ids == ["1011171116", "2022334455", "3033445566"]

    def test_urls_are_full_https(self):
        html = _load_fixture("search_results.html")
        result = self.scraper._parse_listing_ids(html, "almaty", self.base_url)
        for _id, url in result:
            assert url.startswith("https://krisha.kz/a/show/")

    def test_empty_page_returns_empty_list(self):
        html = _load_fixture("search_empty.html")
        result = self.scraper._parse_listing_ids(html, "almaty", self.base_url)
        assert result == []

    def test_missing_section_returns_empty_list(self):
        html = "<html><body><p>no section here</p></body></html>"
        result = self.scraper._parse_listing_ids(html, "almaty", self.base_url)
        assert result == []

    def test_card_without_link_uses_fallback_url(self):
        html = """
        <html><body>
        <section class="a-search-list">
          <div data-id="9876543210"><!-- no a.a-card__title --></div>
        </section>
        </body></html>
        """
        result = self.scraper._parse_listing_ids(html, "almaty", self.base_url)
        assert len(result) == 1
        assert result[0][0] == "9876543210"
        assert result[0][1] == "https://krisha.kz/a/show/9876543210"


# ---------------------------------------------------------------------------
# Phase 2: _parse_detail
# ---------------------------------------------------------------------------


class TestParseDetail:
    """Unit tests for _parse_detail — parse the jsdata JSON blob."""

    def setup_method(self):
        self.scraper = KrishaScraper()
        self.base_url = "https://krisha.kz"
        self.detail_url = "https://krisha.kz/a/show/1011171116"

    def test_owner_listing_full_fields(self):
        html = _load_fixture("detail_owner.html")
        listing = self.scraper._parse_detail(
            html, "1011171116", "almaty", self.detail_url
        )

        assert listing.id == "1011171116"
        assert listing.title == "1-комнатная квартира · 46 м² · 8/9 этаж"
        assert listing.price == 29_000_000
        assert listing.rooms == 1
        assert listing.area == 46.0
        # address from adverts[0]["fullAddress"]
        assert listing.address == "Алматы, Турксибский р-н, мкр Кайрат 153/59"
        # image_url from photos[0]["src"]
        assert listing.image_url == "https://krisha-photos.kcdn.online/webp/4c/4c1d5fd7/71-full.jpg"
        # url constructed from id, NOT from adverts[0]["url"] (protocol-relative)
        assert listing.url == "https://krisha.kz/a/show/1011171116"
        # published_at from adverts[0]["createdAt"] — NOT addedAt
        assert listing.published_at is not None
        assert listing.published_at.year == 2026
        assert listing.published_at.month == 4
        assert listing.published_at.day == 9
        assert listing.city_id == "almaty"
        assert listing.is_owner is True

    def test_complex_listing_is_owner_false(self):
        html = _load_fixture("detail_complex.html")
        listing = self.scraper._parse_detail(
            html, "1009994655", "almaty", "https://krisha.kz/a/show/1009994655"
        )
        assert listing.is_owner is False
        assert listing.id == "1009994655"

    def test_viewer_relative_isOwner_field_is_ignored(self):
        """
        adverts[0]["isOwner"] is viewer-relative and ALWAYS false on real data
        even for genuine owner listings. The scraper must use advert["userType"]
        exclusively.
        """
        html = _load_fixture("detail_owner.html")
        listing = self.scraper._parse_detail(
            html, "1011171116", "almaty", self.detail_url
        )
        # isOwner in adverts[0] is false in the fixture, yet is_owner must be True
        # because advert["userType"] == "owner"
        assert listing.is_owner is True

    def test_no_jsdata_script_returns_fallback(self):
        html = "<html><body><p>no jsdata here</p></body></html>"
        fallback_url = "https://krisha.kz/a/show/99"
        listing = self.scraper._parse_detail(html, "99", "almaty", fallback_url)
        assert listing.id == "99"
        assert listing.url == fallback_url
        assert listing.title is None
        assert listing.is_owner is False

    def test_empty_jsdata_returns_fallback(self):
        html = "<html><body><script id='jsdata'></script></body></html>"
        fallback_url = "https://krisha.kz/a/show/99"
        listing = self.scraper._parse_detail(html, "99", "almaty", fallback_url)
        assert listing.id == "99"
        assert listing.is_owner is False

    def test_invalid_json_returns_fallback(self):
        html = "<html><body><script id='jsdata'>NOT JSON {{</script></body></html>"
        fallback_url = "https://krisha.kz/a/show/99"
        listing = self.scraper._parse_detail(html, "99", "almaty", fallback_url)
        assert listing.id == "99"
        assert listing.is_owner is False

    def test_empty_photos_gives_none_image_url(self):
        data = {
            "advert": {
                "id": 111,
                "title": "test",
                "price": 1000000,
                "rooms": 1,
                "square": 40,
                "userType": "owner",
                "photos": [],
            },
            "adverts": [
                {
                    "id": 111,
                    "fullAddress": "Алматы",
                    "createdAt": "2026-01-01",
                    "addedAt": "2026-06-01",
                    "url": "//krisha.kz/a/show/111",
                }
            ],
        }
        html = f"<script id='jsdata'>{json.dumps(data)}</script>"
        listing = self.scraper._parse_detail(
            html, "111", "almaty", "https://krisha.kz/a/show/111"
        )
        assert listing.image_url is None
        assert listing.is_owner is True

    def test_url_constructed_from_id_not_protocol_relative(self):
        """
        adverts[0]["url"] is "//krisha.kz/..." (protocol-relative).
        The scraper must NOT use it; it must construct https://krisha.kz/a/show/{id}.
        """
        html = _load_fixture("detail_owner.html")
        listing = self.scraper._parse_detail(
            html, "1011171116", "almaty", self.detail_url
        )
        assert listing.url.startswith("https://")
        assert "//krisha.kz" not in listing.url.replace("https://krisha.kz", "")

    def test_address_from_fullAddress_not_structured_dict(self):
        """
        advert["address"] is a STRUCTURED dict — must NOT be coerced to str.
        Address must come from adverts[0]["fullAddress"].
        """
        html = _load_fixture("detail_owner.html")
        listing = self.scraper._parse_detail(
            html, "1011171116", "almaty", self.detail_url
        )
        # The structured dict str representation would look like "{...}"
        assert not listing.address.startswith("{")
        assert listing.address == "Алматы, Турксибский р-н, мкр Кайрат 153/59"

    def test_published_at_from_createdAt_not_addedAt(self):
        """
        createdAt="2026-04-09", addedAt="2026-06-21".
        published_at must be 2026-04-09, NOT 2026-06-21.
        """
        html = _load_fixture("detail_owner.html")
        listing = self.scraper._parse_detail(
            html, "1011171116", "almaty", self.detail_url
        )
        assert listing.published_at is not None
        assert listing.published_at.month == 4  # April, not June
        assert listing.published_at.day == 9


# ---------------------------------------------------------------------------
# _parse_date helper
# ---------------------------------------------------------------------------


class TestParseDate:
    def setup_method(self):
        self.scraper = KrishaScraper()

    def test_plain_date_string(self):
        result = self.scraper._parse_date("2026-04-09")
        assert result is not None
        assert result.year == 2026
        assert result.month == 4
        assert result.day == 9

    def test_none_returns_none(self):
        assert self.scraper._parse_date(None) is None

    def test_empty_string_returns_none(self):
        assert self.scraper._parse_date("") is None

    def test_unix_timestamp_int(self):
        result = self.scraper._parse_date(0)
        assert result is not None
        assert result.year == 1970

    def test_unparseable_string_returns_none(self):
        assert self.scraper._parse_date("not-a-date") is None

    def test_iso_with_z_suffix(self):
        result = self.scraper._parse_date("2026-04-09T12:00:00Z")
        assert result is not None
        assert result.year == 2026


# ---------------------------------------------------------------------------
# fetch_listing_ids — integration with respx HTTP mock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFetchListingIds:
    async def test_returns_ids_from_html(self):
        html = _load_fixture("search_results.html")
        scraper = _make_scraper()
        try:
            with respx.mock:
                respx.get("https://krisha.kz/prodazha/kvartiry/almaty/").mock(
                    return_value=httpx.Response(200, text=html)
                )
                result = await scraper.fetch_listing_ids("almaty", page=1)
        finally:
            await scraper._client.aclose()

        assert len(result) == 3
        assert result[0][0] == "1011171116"

    async def test_challenge_403_returns_empty(self):
        scraper = _make_scraper()
        try:
            with respx.mock:
                respx.get("https://krisha.kz/prodazha/kvartiry/almaty/").mock(
                    return_value=httpx.Response(403, text="Forbidden")
                )
                result = await scraper.fetch_listing_ids("almaty")
        finally:
            await scraper._client.aclose()

        assert result == []

    async def test_challenge_429_returns_empty(self):
        scraper = _make_scraper()
        try:
            with respx.mock:
                respx.get("https://krisha.kz/prodazha/kvartiry/almaty/").mock(
                    return_value=httpx.Response(429, text="Too Many Requests")
                )
                result = await scraper.fetch_listing_ids("almaty")
        finally:
            await scraper._client.aclose()

        assert result == []

    async def test_challenge_body_marker_returns_empty(self):
        scraper = _make_scraper()
        try:
            with respx.mock:
                respx.get("https://krisha.kz/prodazha/kvartiry/almaty/").mock(
                    return_value=httpx.Response(
                        200, text="<html>Just a moment...</html>"
                    )
                )
                result = await scraper.fetch_listing_ids("almaty")
        finally:
            await scraper._client.aclose()

        assert result == []

    async def test_network_error_returns_empty(self):
        """
        When the httpx client raises ConnectError on every attempt,
        with_retry exhausts all retries and fetch_listing_ids returns [].

        asyncio.sleep is patched so the test does not wait for real backoff delays.
        """
        scraper = _make_scraper()

        async def _raise_connect_error(*_args, **_kwargs):
            raise httpx.ConnectError("network down")

        try:
            with patch.object(scraper._client, "get", side_effect=_raise_connect_error), \
                 patch("bot.scraper.anti_bot.asyncio.sleep", new_callable=AsyncMock):
                result = await scraper.fetch_listing_ids("almaty")
        finally:
            await scraper._client.aclose()

        assert result == []


# ---------------------------------------------------------------------------
# fetch_listing_detail — integration with respx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFetchListingDetail:
    async def test_owner_detail_returns_full_listing(self):
        html = _load_fixture("detail_owner.html")
        scraper = _make_scraper()
        try:
            with respx.mock:
                respx.get("https://krisha.kz/a/show/1011171116").mock(
                    return_value=httpx.Response(200, text=html)
                )
                listing = await scraper.fetch_listing_detail("1011171116", "almaty")
        finally:
            await scraper._client.aclose()

        assert listing.id == "1011171116"
        assert listing.is_owner is True
        assert listing.price == 29_000_000
        assert listing.address == "Алматы, Турксибский р-н, мкр Кайрат 153/59"

    async def test_complex_detail_is_owner_false(self):
        html = _load_fixture("detail_complex.html")
        scraper = _make_scraper()
        try:
            with respx.mock:
                respx.get("https://krisha.kz/a/show/1009994655").mock(
                    return_value=httpx.Response(200, text=html)
                )
                listing = await scraper.fetch_listing_detail("1009994655", "almaty")
        finally:
            await scraper._client.aclose()

        assert listing.is_owner is False

    async def test_challenge_returns_partial_listing(self):
        scraper = _make_scraper()
        try:
            with respx.mock:
                respx.get("https://krisha.kz/a/show/999").mock(
                    return_value=httpx.Response(403, text="Forbidden")
                )
                listing = await scraper.fetch_listing_detail("999", "almaty")
        finally:
            await scraper._client.aclose()

        assert listing.id == "999"
        assert listing.is_owner is False
        assert listing.title is None
