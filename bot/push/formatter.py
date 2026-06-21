"""
bot/push/formatter.py — Formats a Listing into a Telegram caption.

format_listing(listing) → (caption: str, image_url: str | None)

Caption format (Russian, emoji lines omitted if field is None):
  🏠 {title}
  💰 {price} ₸
  📐 {area} м² · {rooms}-комн.
  📍 {address}
  🔗 {url}

Telegram caption limit: 1024 chars for photos (vs 4096 for messages).
Caption is truncated with "..." if it would exceed that.
"""

from __future__ import annotations

from bot.scraper.models import Listing

_CAPTION_LIMIT = 1024


def format_listing(listing: Listing) -> tuple[str, str | None]:
    """
    Build a Telegram push message from a Listing.

    Returns
    -------
    (caption, image_url)
        caption   — formatted text, ≤1024 chars (Telegram photo caption limit)
        image_url — first photo URL, or None if the listing has no images
    """
    lines: list[str] = []

    # Title line — use Listing.short_title() which falls back gracefully
    title = listing.short_title()
    lines.append(f"🏠 {title}")

    # Price line
    if listing.price is not None:
        price_formatted = f"{listing.price:,}".replace(
            ",", " "
        )  # narrow no-break space
        lines.append(f"💰 {price_formatted} ₸")

    # Area + rooms line (combined to save vertical space)
    area_rooms_parts: list[str] = []
    if listing.area is not None:
        area_rooms_parts.append(f"{listing.area:g} м²")
    if listing.rooms is not None:
        area_rooms_parts.append(f"{listing.rooms}-комн.")
    if area_rooms_parts:
        lines.append("📐 " + " · ".join(area_rooms_parts))

    # Address line
    if listing.address:
        lines.append(f"📍 {listing.address}")

    # URL line — always present (url is required in Listing model)
    lines.append(f"🔗 {listing.url}")

    caption = "\n".join(lines)

    # Truncate to Telegram caption limit
    if len(caption) > _CAPTION_LIMIT:
        caption = caption[: _CAPTION_LIMIT - 3] + "..."

    return caption, listing.image_url
