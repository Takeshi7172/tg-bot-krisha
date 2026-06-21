"""
tests/push/test_formatter.py — Tests for format_listing().

Tests:
  - Full caption contains title, price, area, rooms, address, url
  - None price omitted from caption
  - None rooms omitted
  - None address omitted
  - Thousands separator (spaces, not commas)
  - Caption >1024 chars is truncated with "..."
  - Returns (caption, image_url) tuple; image_url matches listing.image_url
"""

from __future__ import annotations

import pytest

from bot.push.formatter import format_listing
from bot.scraper.models import Listing


def _listing(**kwargs) -> Listing:
    defaults = dict(
        id="1011171116",
        url="https://krisha.kz/a/show/1011171116",
        city_id="almaty",
    )
    defaults.update(kwargs)
    return Listing(**defaults)


class TestFormatListing:
    def test_full_caption_contains_all_fields(self):
        listing = _listing(
            title="1-комнатная квартира · 46 м² · 8/9 этаж",
            price=29_000_000,
            rooms=1,
            area=46.0,
            address="Алматы, Турксибский р-н, мкр Кайрат 153/59",
            image_url="https://example.com/photo.jpg",
        )
        caption, image_url = format_listing(listing)

        assert "1-комнатная квартира" in caption
        # Formatter uses narrow no-break space   as thousands separator
        assert "29 000 000" in caption
        assert "46" in caption
        assert "1-комн." in caption
        assert "Алматы" in caption
        assert "https://krisha.kz/a/show/1011171116" in caption
        assert image_url == "https://example.com/photo.jpg"

    def test_none_price_omitted(self):
        listing = _listing(price=None, title="Test")
        caption, _ = format_listing(listing)
        assert "₸" not in caption
        assert "💰" not in caption

    def test_none_rooms_omitted(self):
        listing = _listing(rooms=None, area=50.0, title="Test")
        caption, _ = format_listing(listing)
        assert "комн." not in caption

    def test_none_area_omitted(self):
        listing = _listing(area=None, rooms=2, title="Test")
        caption, _ = format_listing(listing)
        assert "м²" not in caption

    def test_none_address_omitted(self):
        listing = _listing(address=None, title="Test")
        caption, _ = format_listing(listing)
        assert "📍" not in caption

    def test_thousands_separator_is_not_comma(self):
        listing = _listing(price=29_000_000)
        caption, _ = format_listing(listing)
        # Formatter replaces commas with narrow no-break space ( )
        assert "29,000,000" not in caption
        # Price digits are present
        assert "29" in caption and "000" in caption

    def test_url_always_present(self):
        listing = _listing()
        caption, _ = format_listing(listing)
        assert "https://krisha.kz/a/show/1011171116" in caption

    def test_caption_truncated_at_1024(self):
        # Build a title so long that the caption exceeds 1024 chars
        very_long_title = "Квартира " + "мкр " * 300
        listing = _listing(
            title=very_long_title,
            price=1_000_000,
            address="Алматы",
        )
        caption, _ = format_listing(listing)
        assert len(caption) <= 1024
        assert caption.endswith("...")

    def test_caption_under_1024_not_truncated(self):
        listing = _listing(title="Short", price=1_000_000, address="Алматы")
        caption, _ = format_listing(listing)
        assert not caption.endswith("...")
        assert len(caption) < 1024

    def test_none_image_url_returned_when_no_photo(self):
        listing = _listing(image_url=None)
        _, image_url = format_listing(listing)
        assert image_url is None

    def test_short_title_fallback_when_no_title(self):
        """If title is None, short_title() returns rooms/area fallback."""
        listing = _listing(title=None, rooms=2, area=55.0)
        caption, _ = format_listing(listing)
        # short_title() produces "2-комн. кв., 55 м²"
        assert "2-комн." in caption

    def test_price_format_contains_tenge_sign(self):
        listing = _listing(price=29_000_000)
        caption, _ = format_listing(listing)
        # Price formatted with tenge sign
        assert "₸" in caption
        assert "29" in caption

    def test_returns_two_tuple(self):
        listing = _listing()
        result = format_listing(listing)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_area_with_integer_value_formats_without_decimal(self):
        listing = _listing(area=46.0, rooms=1)
        caption, _ = format_listing(listing)
        # :g format strips trailing zeros: 46.0 → "46"
        assert "46 м²" in caption

    def test_both_area_and_rooms_in_same_line(self):
        listing = _listing(area=46.0, rooms=1)
        caption, _ = format_listing(listing)
        # They appear on the same line separated by " · "
        lines = caption.split("\n")
        area_rooms_line = next((l for l in lines if "м²" in l and "комн." in l), None)
        assert area_rooms_line is not None, "Expected area and rooms on the same line"
