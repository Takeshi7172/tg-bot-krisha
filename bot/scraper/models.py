"""
bot/scraper/models.py — Pydantic v2 model for a normalised krisha.kz listing.

This is the canonical data transfer object between the scraper and the
notification layer (poller → push_callback). No ORM, no DB columns — it is a
pure in-memory model that travels across the asyncio event loop.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class Listing(BaseModel):
    """
    Normalised representation of a single krisha.kz apartment-for-sale listing.

    Field notes
    -----------
    id          : Krisha internal listing id (numeric string, e.g. "1013256936").
                  Stored as str to avoid int overflow on unusual IDs and to
                  match the DB seen_listings.listing_id column (text).
    title       : Constructed from jsdata if not exposed directly. May be None
                  when the scraper hits a partial-parse fallback.
    price       : Asking price in KZT (tenge). None if not parseable.
    rooms       : Room count (1, 2, 3 …). None if not parseable.
    area        : Total area in m². None if not parseable.
    address     : Full address string from adverts[0]["fullAddress"]. None on fallback.
    url         : Full canonical URL — https://krisha.kz/a/show/{id}.
    image_url   : URL of the first photo thumbnail. None if listing has no photos.
    published_at: ISO 8601 datetime when the listing was published. None if unknown.
    city_id     : Krisha city SLUG (e.g. "almaty") — matches CITY_MAP keys in config.py.
                  NOT a numeric id. Stored as text in DB.
    is_owner    : True if the listing was posted by a private property owner.
                  Determined from advert["userType"] == "owner" (live-verified 2026-06-21).
                  "complex" (developer/ЖК) and "agent"/"agency" values yield False.
                  Defaults to False — the scraper always passes the computed value;
                  partial fallback Listings (detail fetch failed) are treated as non-owner
                  so they are never pushed to subscribers.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    title: str | None = None
    price: int | None = None
    rooms: int | None = None
    area: float | None = None
    address: str | None = None
    url: str
    image_url: str | None = None
    published_at: datetime | None = None
    city_id: str
    is_owner: bool = False

    @field_validator("id")
    @classmethod
    def id_must_be_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Listing id must be a non-empty string")
        return v.strip()

    @field_validator("url")
    @classmethod
    def url_must_be_https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError(f"Listing url must start with https://, got: {v!r}")
        return v

    @field_validator("city_id")
    @classmethod
    def city_id_must_be_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("city_id must be a non-empty string")
        return v.strip()

    # -----------------------------------------------------------------------
    # Convenience helpers (used by Wave 3 notification formatter)
    # -----------------------------------------------------------------------

    def format_price(self) -> str:
        """Return price as a human-readable KZT string, e.g. '45 000 000 ₸'."""
        if self.price is None:
            return "Цена не указана"
        # Thousands separator with Kazakh tenge sign
        return f"{self.price:,}".replace(",", " ") + " ₸"

    def format_area(self) -> str:
        """Return area as a string, e.g. '52.0 м²'."""
        if self.area is None:
            return ""
        return f"{self.area:g} м²"

    def short_title(self) -> str:
        """
        Derive a short title from available fields if title is None.
        Example: '2-комн. кв., 52 м²'
        """
        if self.title:
            return self.title
        parts: list[str] = []
        if self.rooms:
            parts.append(f"{self.rooms}-комн. кв.")
        if self.area:
            parts.append(f"{self.area:g} м²")
        return ", ".join(parts) if parts else "Квартира"
